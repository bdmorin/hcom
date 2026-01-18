"""Claude Code PTY integration for hcom.

Launches Claude in PTY wrapper with message polling.
Hooks track status, poll thread injects via PTY when idle.

Key env var: HCOM_PTY_MODE=1 tells Stop hook to just signal idle (not inject).
"""

from __future__ import annotations

import os
import sys
import threading
import json
import shlex
import random
import signal
import base64
from pathlib import Path

from .pty_wrapper import PTYWrapper
from .pty_common import (
    inject_message as _inject,
    inject_enter as _inject_enter,
    get_instance_status,
    get_instance_cursor,
    wait_for_process_registration,
    register_notify_port,
    set_terminal_title,
    GateWrapperView,
    HeartbeatNotifier,
    create_sighup_handler,
    CLAUDE_CODEX_READY_PATTERN,
    STATUS_CONTEXT_EXIT_KILLED,
    STATUS_CONTEXT_EXIT_CLOSED,
)
from .push_delivery import (
    DeliveryGate,
    NotifyServerAdapter,
    TwoPhaseRetryPolicy,
    run_notify_delivery_loop,
)
from ..core.log import log_info, log_warn, log_error

# Ready pattern - Claude shows "? for shortcuts" in status bar when idle
READY_PATTERN = CLAUDE_CODEX_READY_PATTERN


def inject_message(port: int, message: str, instance: str = "?") -> bool:
    """Inject message to PTY via TCP."""
    result = _inject(port, message)
    if not result:
        log_warn("pty", "inject.fail", instance=instance, tool="claude")
    return result


def _run_receiver_thread(
    *,
    process_id: str,
    instance_name: str,
    gate_wrapper: GateWrapperView,
    running_flag: list[bool],
) -> None:
    """Receiver thread using push_delivery loop (same pattern as gemini.py).

    Injects "<hcom>" trigger - UserPromptSubmit hook delivers actual messages.

    Delivery verification is handled by push_delivery loop via get_cursor callback:
    - Loop snapshots cursor before inject
    - If cursor advances after inject, delivery confirmed
    - If cursor stuck after 2s timeout, loop retries injection
    """
    from ..core.db import get_instance
    from ..core.messages import get_unread_messages
    from ..core.instances import set_status

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
            tool="claude",
        )
        return

    current["name"] = bound_name

    # SessionStart hook is authoritative for ready detection (sets status to listening).
    # PTY wrapper just provides the injection mechanism - don't redundantly set status here.

    # Set terminal window and tab title
    set_terminal_title(current["name"])

    # Setup notify server
    notifier = NotifyServerAdapter()
    if not notifier.start():
        log_warn("pty", "notify.fail", instance=current["name"], tool="claude")

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

    def running() -> bool:
        return bool(running_flag[0])

    def is_listening() -> bool:
        """Check if Claude is listening (via hook-reported status)."""
        if not _refresh_binding():
            return False
        status, _detail = get_instance_status(current["name"])
        return status == "listening"

    def get_cursor() -> int:
        """Get current cursor position for delivery verification."""
        _refresh_binding()
        return get_instance_cursor(current["name"])

    def has_pending() -> bool:
        """Check for unread messages."""
        if not _refresh_binding():
            return False  # Binding gone - don't claim messages for stolen identity
        messages, _ = get_unread_messages(current["name"], update_position=False)
        return bool(messages)

    def try_deliver() -> bool:
        """Inject <hcom> trigger."""
        if not _refresh_binding():
            return False  # Binding gone - don't deliver
        if not gate_wrapper.actual_port:
            return False

        inst = get_instance(current["name"])
        if not inst:  # Row exists = participating
            return False

        return inject_message(gate_wrapper.actual_port, "<hcom>", current["name"])

    def try_enter() -> bool:
        """Inject just Enter key (for retry when text already in buffer)."""
        _refresh_binding()
        if not gate_wrapper.actual_port:
            return False
        return _inject_enter(gate_wrapper.actual_port)

    gate = DeliveryGate(
        require_idle=True,  # Claude: Stop hook sets "listening" after each turn
        require_ready_prompt=False,  # Claude hides "? for shortcuts" in accept-edits mode
        require_prompt_empty=True,  # Block if user has uncommitted text (Try " or ↵ send = safe)
        require_output_stable_seconds=1.0,
        block_on_user_activity=True,
        block_on_approval=True,
    )

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
            wrapper=gate_wrapper,
            has_pending=has_pending,
            try_deliver=try_deliver,
            try_enter=try_enter,
            is_idle=is_listening,
            gate=gate,
            retry=retry,
            idle_wait=30.0,
            instance_name=current["name"],
            get_cursor=get_cursor,
            verify_timeout=2.0,
        )
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        log_error(
            "pty",
            "receiver.crash",
            str(e),
            instance=current["name"],
            tool="claude",
            traceback=tb,
        )
        set_status(current["name"], "error", context="pty:crash", detail=str(e)[:200])


def run_claude_with_hcom(claude_args: list[str] | None = None) -> int:
    """Run Claude with hcom PTY integration. Blocks until Claude exits.

    Reads HCOM_PROCESS_ID from env (set by launcher).

    Args:
        claude_args: Arguments to pass to claude command
    """
    claude_args = claude_args or []

    process_id = os.environ.get("HCOM_PROCESS_ID")
    if not process_id:
        print("[claude-pty] ERROR: HCOM_PROCESS_ID not set", file=sys.stderr)
        return 1
    from ..core.db import get_process_binding

    binding = get_process_binding(process_id)
    instance_name = binding.get("instance_name") if binding else None
    if not instance_name:
        print("[claude-pty] ERROR: Process binding missing", file=sys.stderr)
        return 1

    # Create wrapper with claude command + args
    claude_command = ["claude"] + claude_args
    wrapper = PTYWrapper(
        command=claude_command,
        instance_name=instance_name,
        port=0,  # Auto-assign
        ready_pattern=READY_PATTERN,
    )
    gate_wrapper = GateWrapperView(wrapper)

    # Shared flag for stopping receiver thread
    running_flag: list[bool] = [True]

    # Patch wait_for_ready to start receiver thread when ready
    original_wait = wrapper.wait_for_ready

    def patched_wait(pattern: bytes | None = None, timeout: float = 30.0) -> bool:
        if pattern is None:
            pattern = READY_PATTERN
        result = original_wait(pattern, timeout)

        if result and wrapper.actual_port:
            log_info(
                "pty",
                "pty.ready",
                port=wrapper.actual_port,
                instance=instance_name,
                tool="claude",
            )

            # Start receiver thread (uses push_delivery loop)
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

        return result

    wrapper.wait_for_ready = patched_wait

    # Setup SIGHUP handler for terminal close
    signal.signal(
        signal.SIGHUP,
        create_sighup_handler(
            instance_name,
            running_flag,
            process_id,
            exit_context=STATUS_CONTEXT_EXIT_KILLED,
        ),
    )

    # Run - blocks until claude exits
    try:
        exit_code = wrapper.run()
        log_info(
            "pty",
            "pty.exit",
            exit_code=exit_code,
            instance=instance_name,
            tool="claude",
        )
    finally:
        running_flag[0] = False
        # Update DB to mark instance as dead
        try:
            from ..core.db import get_process_binding, delete_process_binding
            from ..core.instances import set_status

            resolved_name = instance_name
            binding = get_process_binding(process_id)
            bound_name = binding.get("instance_name") if binding else None
            if bound_name:
                resolved_name = bound_name
            set_status(resolved_name, "inactive", context=STATUS_CONTEXT_EXIT_CLOSED)
            # Stop instance (delete row, log life event)
            from ..core.tool_utils import stop_instance

            stop_instance(resolved_name, initiated_by="pty", reason="closed")
            # Clean up process binding (prevents stale entries)
            delete_process_binding(process_id)
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=instance_name, tool="claude")

    return exit_code


def create_claude_runner_script(
    cwd: str,
    instance_name: str,
    process_id: str,
    tag: str,
    claude_args: list[str] | None = None,
    run_here: bool = False,
) -> str:
    """Create a bash script that runs Claude with hcom PTY integration.

    Args:
        cwd: Working directory
        instance_name: HCOM instance name
        tag: Instance tag prefix
        claude_args: Arguments to pass to claude command
        run_here: If True, script is for current terminal (no exec bash at end)
    """
    from ..core.paths import hcom_path, LAUNCH_DIR

    claude_args = claude_args or []
    script_file = str(
        hcom_path(LAUNCH_DIR, f"claude_{instance_name}_{random.randint(1000, 9999)}.sh")
    )

    python_path = sys.executable
    module_dir = Path(__file__).parent.parent

    # Serialize args via base64
    claude_args_b64 = base64.b64encode(json.dumps(claude_args).encode()).decode()

    # For new terminal launches, keep terminal open after exit
    # For run_here launches, just exit to return to original shell
    # Clear identity env vars to prevent reuse if user runs claude directly after
    exec_line = (
        ""
        if run_here
        else """
unset HCOM_PROCESS_ID HCOM_LAUNCHED HCOM_PTY_MODE HCOM_TAG
exec bash -l"""
    )

    # Export HCOM_DIR if set (for test isolation and custom hcom directories)
    hcom_dir = os.environ.get("HCOM_DIR", "")
    hcom_dir_export = (
        f'export HCOM_DIR="{hcom_dir}"' if hcom_dir else "# HCOM_DIR not set"
    )

    script_content = f'''#!/bin/bash
# Claude hcom PTY runner ({instance_name})
cd {shlex.quote(cwd)}

# HCOM identity
export HCOM_PROCESS_ID="{process_id}"
export HCOM_TAG="{tag}"
export HCOM_LAUNCHED=1
export HCOM_PTY_MODE=1
{hcom_dir_export}
{"export HCOM_VIA_SHIM=1" if os.environ.get("HCOM_VIA_SHIM") else "# no shim"}

export PYTHONPATH="{module_dir.parent}:$PYTHONPATH"
{shlex.quote(python_path)} -c "
import sys, json, base64
sys.path.insert(0, '{module_dir.parent}')
from hcom.pty.claude import run_claude_with_hcom
claude_args = json.loads(base64.b64decode('{claude_args_b64}').decode())
sys.exit(run_claude_with_hcom(claude_args=claude_args))
"{exec_line}
'''

    with open(script_file, "w") as f:
        f.write(script_content)
    os.chmod(script_file, 0o755)

    return script_file


def launch_claude_pty(
    cwd: str,
    env: dict,
    instance_name: str,
    tag: str = "",
    claude_args: list[str] | None = None,
    run_here: bool = False,
) -> str | None:
    """Launch Claude in a terminal via PTY wrapper.

    Args:
        cwd: Working directory
        env: Environment variables
        tag: Instance tag prefix (optional)
        claude_args: Arguments to pass to claude command
        run_here: If True, run in current terminal (blocking). Used for count=1 launches.

    Returns:
        instance_name on success, None on failure
    """
    from ..terminal import launch_terminal

    claude_args = claude_args or []

    process_id = env.get("HCOM_PROCESS_ID")
    if not process_id:
        log_error(
            "pty",
            "pty.exit",
            "HCOM_PROCESS_ID not set in env",
            instance=instance_name,
            tool="claude",
        )
        return None

    # Create runner script (pass run_here to skip exec bash for current terminal)
    script_file = create_claude_runner_script(
        cwd, instance_name, process_id, tag, claude_args, run_here=run_here
    )

    # Launch (run_here=True for count=1, else new terminal)
    success = launch_terminal(
        f"bash {shlex.quote(script_file)}", env, cwd=cwd, run_here=run_here
    )
    return instance_name if success else None


__all__ = ["launch_claude_pty", "run_claude_with_hcom", "READY_PATTERN"]
