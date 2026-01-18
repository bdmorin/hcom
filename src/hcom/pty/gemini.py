"""Gemini CLI integration.

This module provides the PTY integration for Gemini using the notify-driven delivery engine.

Key behavior:
- PTY wrapper injects "[hcom]" only.
- Actual message content is delivered by Gemini's BeforeAgent hook.
"""

from __future__ import annotations

import os
import sys
import threading
import random
import shlex
import signal
import typing
from pathlib import Path

from .pty_wrapper import PTYWrapper
from .push_delivery import (
    PTYLike,
    DeliveryGate,
    NotifyServerAdapter,
    TwoPhaseRetryPolicy,
    run_notify_delivery_loop,
)
from .pty_common import (
    inject_message as _inject,
    inject_enter as _inject_enter,
    get_instance_cursor,
    wait_for_process_registration,
    register_notify_port,
    GateWrapperView,
    HeartbeatNotifier,
    DebouncedIdleChecker,
    create_sighup_handler,
    build_message_preview,
    set_terminal_title,
    termux_shebang_bypass,
    GEMINI_READY_PATTERN,
)
from ..core.log import log_info, log_warn, log_error
from ..tools.gemini.args import (
    GeminiArgsSpec,
    resolve_gemini_args,
    merge_gemini_args,
)


# ==================== Args Resolution ====================


def get_resolved_gemini_args(cli_args: list[str] | None = None) -> GeminiArgsSpec:
    """Resolve Gemini args with config precedence: CLI > env > config file.

    Merges HCOM_GEMINI_ARGS from config.env with CLI args.
    Validates for conflicts and returns parsed spec.
    """
    from ..core.config import get_config

    config = get_config()
    env_value = config.gemini_args if config.gemini_args else None

    # Parse env and CLI separately
    env_spec = resolve_gemini_args(None, env_value)
    cli_spec = resolve_gemini_args(cli_args, None)

    # Merge: CLI takes precedence
    if cli_args:
        return merge_gemini_args(env_spec, cli_spec)
    return env_spec


# ==================== PTY Implementation ====================


def inject_trigger(port: int, instance_name: str) -> bool:
    """Inject message trigger with preview to PTY."""
    preview = build_message_preview(instance_name)
    ok = _inject(port, preview)
    if not ok:
        log_warn("pty", "inject.fail", instance=instance_name, tool="gemini")
    return ok


def _run_receiver_thread(
    *,
    process_id: str,
    instance_name: str,
    gate_wrapper: GateWrapperView,
    running_flag: list[bool],
) -> None:
    """Receiver thread using push_delivery loop.

    Delivery verification is handled by push_delivery loop via get_cursor callback:
    - Loop snapshots cursor before inject
    - If cursor advances after inject, delivery confirmed
    - If cursor stuck after 2s timeout, loop retries injection
    """
    from ..core.db import get_instance
    from ..core.messages import get_unread_messages
    from .pty_common import get_instance_status

    current = {"name": instance_name}

    # Don't require session_id for hcom-launched instances - process binding is sufficient.
    # Session_id comes from SessionStart hook which may be delayed; if we wait for it,
    # the receiver thread may timeout and exit without registering notify port.
    # The instance row exists (created by launcher) so message delivery will work.
    bound_name, instance = wait_for_process_registration(
        process_id,
        timeout=30,
        require_session_id=False,  # Process binding is enough for hcom-launched
    )
    if not instance or not bound_name:
        log_error(
            "pty",
            "pty.exit",
            "process binding not found",
            instance=current["name"],
            tool="gemini",
        )
        return

    current["name"] = bound_name

    # SessionStart hook is authoritative for ready detection (sets status to listening).
    # PTY wrapper just provides the injection mechanism - don't redundantly set status here.

    notifier = NotifyServerAdapter()
    if not notifier.start():
        log_warn("pty", "notify.fail", instance=current["name"], tool="gemini")

    register_notify_port(current["name"], notifier.port, notifier.port is not None)

    def _refresh_binding() -> bool:
        """Refresh binding from DB. Returns True if binding is valid."""
        from ..core.db import get_process_binding, migrate_notify_endpoints

        binding = get_process_binding(process_id)
        if not binding:
            # Binding removed (reset/stolen) - keep loop alive for rebind
            # Return False so callers skip delivery for this stolen identity
            return False
        new_name = binding.get("instance_name")
        if new_name and new_name != current["name"]:
            migrate_notify_endpoints(current["name"], new_name)
            current["name"] = new_name
            set_terminal_title(current["name"])
        # Always ensure notify port is registered (handles rebind after reset)
        register_notify_port(current["name"], notifier.port, notifier.port is not None)
        return True

    idle_checker = DebouncedIdleChecker(debounce_seconds=0.4)

    def running() -> bool:
        return bool(running_flag[0])

    def is_idle() -> bool:
        # Use DebouncedIdleChecker: Gemini fires AfterAgent multiple times per turn.
        # Wait 400ms for idle to stabilize before considering it final.
        # See dev/gemini-afteragent-detection-findings.md for details.
        if not _refresh_binding():
            return False
        status, _detail = get_instance_status(current["name"])
        return idle_checker.is_stable_idle(status)

    def get_cursor() -> int:
        """Get current cursor position for delivery verification."""
        _refresh_binding()
        return get_instance_cursor(current["name"])

    def has_pending() -> bool:
        """Check for unread messages."""
        if not _refresh_binding():
            return False  # Binding gone - don't claim messages for stolen identity
        inst = get_instance(current["name"])
        if not inst:  # Row exists = participating
            return False
        messages, _ = get_unread_messages(current["name"], update_position=False)
        return bool(messages)

    def try_deliver() -> bool:
        """Inject trigger."""
        if not _refresh_binding():
            return False  # Binding gone - don't deliver
        if not gate_wrapper.actual_port:
            return False

        inst = get_instance(current["name"])
        if not inst:  # Row exists = participating
            return False

        return inject_trigger(gate_wrapper.actual_port, current["name"])

    def try_enter() -> bool:
        """Inject just Enter key (for retry when text already in buffer)."""
        if not _refresh_binding():
            return False  # Binding gone - don't deliver
        if not gate_wrapper.actual_port:
            return False
        return _inject_enter(gate_wrapper.actual_port)

    gate = DeliveryGate(
        require_idle=True,  # Gemini: AfterAgent is our authoritative "turn ended"
        require_ready_prompt=True,
        require_output_stable_seconds=1.0,
        block_on_user_activity=True,
        block_on_approval=True,
    )
    # Retry cap stays at 2s for the first minute of pending delivery, then allows up to 5s.
    retry = TwoPhaseRetryPolicy(
        initial=0.25,
        multiplier=2.0,
        warm_maximum=2.0,
        warm_seconds=60.0,
        cold_maximum=5.0,
    )

    try:
        run_notify_delivery_loop(
            running=running,
            notifier=HeartbeatNotifier(notifier, lambda: current["name"]),
            wrapper=typing.cast(PTYLike, gate_wrapper),
            has_pending=has_pending,
            try_deliver=try_deliver,
            try_enter=try_enter,
            is_idle=is_idle,
            gate=gate,
            retry=retry,
            idle_wait=30.0,
            instance_name=current["name"],
            get_cursor=get_cursor,
            verify_timeout=2.0,
        )
    except Exception as e:
        import traceback
        from ..core.instances import set_status

        tb = traceback.format_exc()
        log_error(
            "pty",
            "receiver.crash",
            str(e),
            instance=current["name"],
            tool="gemini",
            traceback=tb,
        )
        set_status(current["name"], "error", context="pty:crash", detail=str(e)[:200])


def run_gemini_with_hcom(gemini_args: list[str] | None = None) -> int:
    """Run Gemini with hcom integration. Blocks until Gemini exits.

    Requires:
    - HCOM_PROCESS_ID set (provided by runner script / launcher)
    - Gemini hooks installed (BeforeAgent/AfterAgent) for content delivery + idle detection
    """
    gemini_args = list(gemini_args or [])

    # PTY mode: no auto-prompt needed (contactable via inject when idle)
    # User-provided prompts flow through unchanged

    process_id = os.environ.get("HCOM_PROCESS_ID")
    if not process_id:
        print("[gemini-hcom] ERROR: HCOM_PROCESS_ID not set", file=sys.stderr)
        return 1
    from ..core.db import get_process_binding

    binding = get_process_binding(process_id)
    instance_name = binding.get("instance_name") if binding else None
    if not instance_name:
        print("[gemini-hcom] ERROR: Process binding missing", file=sys.stderr)
        return 1

    # Apply Termux shebang bypass (explicit node invocation)
    command = termux_shebang_bypass(["gemini", *gemini_args], "gemini")

    wrapper = PTYWrapper(
        command=command,
        instance_name=instance_name,
        port=0,
        ready_pattern=GEMINI_READY_PATTERN,
    )
    gate_wrapper = GateWrapperView(wrapper)

    running_flag: list[bool] = [True]

    original_wait = wrapper.wait_for_ready

    def patched_wait(pattern: bytes | None = None, timeout: float = 30.0) -> bool:
        if pattern is None:
            pattern = GEMINI_READY_PATTERN
        ok = original_wait(pattern, timeout)
        if ok and wrapper.actual_port:
            log_info(
                "pty",
                "pty.ready",
                port=wrapper.actual_port,
                instance=instance_name,
                tool="gemini",
            )
            threading.Thread(
                target=_run_receiver_thread,
                kwargs={
                    "process_id": process_id,
                    "instance_name": instance_name,
                    "gate_wrapper": gate_wrapper,
                    "running_flag": running_flag,
                },
                daemon=True,
            ).start()
        return ok

    wrapper.wait_for_ready = patched_wait

    # Setup SIGHUP handler for terminal close (Gemini doesn't set status - hooks handle it)
    signal.signal(
        signal.SIGHUP,
        create_sighup_handler(
            instance_name,
            running_flag,
            process_id,
        ),
    )

    try:
        return wrapper.run()
    finally:
        running_flag[0] = False
        # Update DB to mark instance as dead (complements SIGHUP handler for normal exit)
        try:
            from ..core.db import get_process_binding, delete_process_binding
            from ..core.instances import set_status

            resolved_name = instance_name
            binding = get_process_binding(process_id)
            bound_name = binding.get("instance_name") if binding else None
            if bound_name:
                resolved_name = bound_name
            set_status(resolved_name, "inactive", "exit:closed")
            # Stop instance (delete row, log life event)
            from ..core.tool_utils import stop_instance

            stop_instance(resolved_name, initiated_by="pty", reason="closed")
            # Clean up process binding (prevents stale entries)
            delete_process_binding(process_id)
        except Exception as db_err:
            log_error("pty", "pty.exit", db_err, instance=instance_name, tool="gemini")


def create_gemini_runner_script(
    cwd: str,
    instance_name: str,
    process_id: str,
    tag: str,
    gemini_args: list[str] | None = None,
    run_here: bool = False,
) -> str:
    """Create a bash script that runs Gemini with hcom integration."""
    from ..core.paths import hcom_path, LAUNCH_DIR

    gemini_args = gemini_args or []
    script_file = str(
        hcom_path(LAUNCH_DIR, f"gemini_{instance_name}_{random.randint(1000, 9999)}.sh")
    )

    python_path = sys.executable
    module_dir = Path(__file__).parent.parent

    import json as _json
    import base64 as _base64

    gemini_args_b64 = _base64.b64encode(_json.dumps(gemini_args).encode()).decode()

    # For new terminal launches, keep terminal open after exit
    # For run_here launches, just exit to return to original shell
    exec_line = (
        ""
        if run_here
        else """
# Clear identity env vars to prevent reuse after exit
unset HCOM_PROCESS_ID HCOM_LAUNCHED HCOM_TAG
exec bash -l"""
    )

    # Export HCOM_DIR if set (for test isolation and custom hcom directories)
    hcom_dir = os.environ.get("HCOM_DIR", "")
    hcom_dir_export = (
        f'export HCOM_DIR="{hcom_dir}"' if hcom_dir else "# HCOM_DIR not set"
    )

    script_content = f"""#!/bin/bash
# Gemini hcom runner ({instance_name})
cd {shlex.quote(cwd)}

export HCOM_PROCESS_ID="{process_id}"
export HCOM_TAG="{tag}"
export HCOM_LAUNCHED=1
{hcom_dir_export}
{"export HCOM_VIA_SHIM=1" if os.environ.get("HCOM_VIA_SHIM") else "# no shim"}

export PYTHONPATH="{module_dir.parent}:$PYTHONPATH"
{shlex.quote(python_path)} -c "
import sys, json, base64
sys.path.insert(0, '{module_dir.parent}')
from hcom.pty.gemini import run_gemini_with_hcom
gemini_args = json.loads(base64.b64decode('{gemini_args_b64}').decode())
sys.exit(run_gemini_with_hcom(gemini_args=gemini_args))
"{exec_line}
"""

    with open(script_file, "w") as f:
        f.write(script_content)
    os.chmod(script_file, 0o755)
    return script_file


def launch_gemini_pty(
    cwd: str,
    env: dict,
    instance_name: str,
    tag: str = "",
    gemini_args: list[str] | None = None,
    run_here: bool = False,
) -> str | None:
    """Launch Gemini in PTY mode with hcom integration.

    Args are expected to be already resolved/validated by caller (lifecycle.py or launcher.py).
    """
    from ..terminal import launch_terminal

    process_id = env.get("HCOM_PROCESS_ID")
    if not process_id:
        log_error(
            "pty",
            "pty.exit",
            "HCOM_PROCESS_ID not set in env",
            instance=instance_name,
            tool="gemini",
        )
        return None

    script_file = create_gemini_runner_script(
        cwd, instance_name, process_id, tag, gemini_args, run_here=run_here
    )
    success = launch_terminal(
        f"bash {shlex.quote(script_file)}", env, cwd=cwd, run_here=run_here
    )
    return instance_name if success else None


__all__ = [
    "launch_gemini_pty",
    "run_gemini_with_hcom",
    "get_resolved_gemini_args",
]
