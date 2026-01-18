"""Codex CLI integration.

This module provides the PTY integration for Codex using the listen-push delivery engine.

Bootstrap delivery:
- Full bootstrap injected at launch via `-c developer_instructions=...` flag
- Built via `get_bootstrap()` in `_add_codex_developer_instructions()`
- User's system_prompt (if any) appended after separator
- Codex knows hcom immediately without running commands first

Message delivery (listen-push):
- Don't inject full message text into Codex.
- Instead, when Codex is safe for input, inject a short instruction that causes Codex
  to run `hcom listen` in its own tool environment.
- `hcom listen` then blocks until messages arrive, prints them, advances cursor, and exits.

Transcript parsing:
- Periodically parses Codex transcript for file edits (apply_patch)
- Logs status events with original timestamps for collision detection
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import signal
import shlex
import random
import typing
from pathlib import Path

from .pty_wrapper import PTYWrapper
from .push_delivery import (
    PTYLike,
    DeliveryGate,
    TwoPhaseRetryPolicy,
    run_notify_delivery_loop,
)
from .pty_common import (
    inject_message as _pty_inject,
    inject_enter as _inject_enter,
    get_instance_cursor,
    NotifyServer,
    register_notify_port,
    update_heartbeat,
    GateWrapperView,
    HeartbeatNotifier,
    create_sighup_handler,
    build_listen_instruction,
    set_terminal_title,
    termux_shebang_bypass,
    CLAUDE_CODEX_READY_PATTERN,
    STATUS_CONTEXT_EXIT_KILLED,
    STATUS_CONTEXT_EXIT_CLOSED,
)
from ..core.log import log_warn, log_error

# Regex to extract file paths from apply_patch input
APPLY_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+?)(?:\n|$)")


# ==================== Transcript Parsing ====================


class TranscriptWatcher:
    """Watches Codex transcript for tool calls and user prompts.

    Parses transcript incrementally (seeks to last position). Logs:
    - apply_patch → tool:apply_patch (file edits for collision detection)
    - shell/shell_command → tool:shell (commands for cmd: subscriptions)
    - user prompts → status_context='prompt' (for glue/user_input tracking)

    Uses original transcript timestamps for accurate event ordering.
    """

    def __init__(self, instance_name: str, transcript_path: str | None = None):
        self.instance_name = instance_name
        self.transcript_path = transcript_path
        self._file_pos = 0
        self._logged_call_ids: set[str] = set()

    def set_transcript_path(self, path: str) -> None:
        """Set/update transcript path (may not be known at init)."""
        if path != self.transcript_path:
            self.transcript_path = path
            self._file_pos = 0  # Reset position for new file

    def sync(self) -> int:
        """Parse new transcript entries, log tool calls and prompts to events DB.

        Returns number of file edits logged (apply_patch only).
        """
        if not self.transcript_path:
            return 0

        path = Path(self.transcript_path)
        if not path.exists():
            return 0

        edits_logged = 0
        try:
            # Reset position if file was truncated/replaced
            file_size = path.stat().st_size
            if file_size < self._file_pos:
                self._file_pos = 0

            with open(path, "r") as f:
                f.seek(self._file_pos)
                new_lines = f.readlines()
                self._file_pos = f.tell()

            for line in new_lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    edits_logged += self._process_entry(entry)
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=self.instance_name, tool="codex")

        return edits_logged

    def _process_entry(self, entry: dict) -> int:
        """Process a single transcript entry. Returns number of edits logged."""
        if entry.get("type") != "response_item":
            return 0

        payload = entry.get("payload", {})
        payload_type = payload.get("type", "")
        timestamp = entry.get("timestamp", "")

        # Handle user messages -> log active:prompt status (but filter hcom injections)
        if payload_type == "message" and payload.get("role") == "user":
            # Extract message text to check for hcom injection
            content = payload.get("content", [])
            text = ""
            for part in content:
                if isinstance(part, dict):
                    text += part.get("text", "")
                elif isinstance(part, str):
                    text += part
            text = text.strip()

            # Skip hcom-injected messages, only log real user prompts
            if not text.startswith("[hcom]"):
                self._log_user_prompt(timestamp)
            return 0

        # Handle both function_call and custom_tool_call formats
        if payload_type not in ("function_call", "custom_tool_call"):
            return 0

        tool_name = payload.get("name", "")
        call_id = payload.get("call_id", "")

        # Skip if already processed
        if call_id and call_id in self._logged_call_ids:
            return 0

        edits = 0

        if tool_name == "apply_patch":
            # Extract file paths from apply_patch input
            input_text = payload.get("input", "") or payload.get("arguments", "")
            files = APPLY_PATCH_FILE_RE.findall(input_text)
            for filepath in files:
                self._log_file_edit(filepath.strip(), timestamp)
                edits += 1

        elif tool_name in ("shell", "shell_command", "exec_command"):
            # Log shell commands for command subscriptions
            # Formats vary by Codex version:
            #   shell: {"command": ["bash", "-lc", "cmd"], "workdir": "..."}
            #   shell_command: {"command": "cmd string", "workdir": "..."}
            #   exec_command: {"cmd": "cmd string", "workdir": "..."}
            args_str = payload.get("arguments", "") or payload.get("input", "")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                cmd = args.get("command", "") or args.get("cmd", "")
                # Handle both array and string formats
                if isinstance(cmd, list):
                    # Array format: ["bash", "-lc", "actual command"]
                    if len(cmd) >= 3 and cmd[0] == "bash" and cmd[1] == "-lc":
                        actual_cmd = cmd[2]
                    else:
                        actual_cmd = " ".join(cmd)
                else:
                    # String format: "actual command"
                    actual_cmd = str(cmd)
                self._log_shell_command(actual_cmd, timestamp)
            except (json.JSONDecodeError, TypeError, AttributeError):
                # Fallback: log raw arguments
                self._log_shell_command(str(args_str)[:500], timestamp)

        if call_id:
            # Sliding window: keep recent call_ids to prevent duplicates while bounding memory
            if len(self._logged_call_ids) > 10000:
                # Keep last 5000 instead of clearing all
                self._logged_call_ids = set(list(self._logged_call_ids)[-5000:])
            self._logged_call_ids.add(call_id)

        return edits

    def _log_status_retroactive(self, status: str, context: str, detail: str, timestamp: str) -> None:
        """Log status event and update instance cache if timestamp is newest.

        Events are always logged with original timestamp (for subscriptions/audit).
        Instance cache is only updated if event timestamp >= current status_time,
        preventing retroactive events from overwriting newer state.
        """
        from ..core.db import log_event, get_instance
        from ..core.instances import update_instance_position
        from ..shared import parse_iso_timestamp

        # Always log event with original timestamp
        log_event(
            event_type="status",
            instance=self.instance_name,
            data={"status": status, "context": context, "detail": detail}
            if detail
            else {"status": status, "context": context},
            timestamp=timestamp or None,
        )

        # Only update instance if this event is newer than current status
        if timestamp:
            try:
                event_dt = parse_iso_timestamp(timestamp)
                if event_dt:
                    event_time = int(event_dt.timestamp())
                    instance = get_instance(self.instance_name)
                    current_time = instance.get("status_time", 0) if instance else 0

                    if event_time >= current_time:
                        updates: dict[str, object] = {
                            "status": status,
                            "status_time": event_time,
                            "status_context": context,
                        }
                        if detail:
                            updates["status_detail"] = detail
                        update_instance_position(self.instance_name, updates)
            except Exception:
                pass  # Don't fail on timestamp parse errors

    def _log_file_edit(self, filepath: str, timestamp: str) -> None:
        """Log a file edit status event for collision detection."""
        try:
            self._log_status_retroactive("active", "tool:apply_patch", filepath, timestamp)
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=self.instance_name, tool="codex")

    def _log_shell_command(self, command: str, timestamp: str) -> None:
        """Log a shell command status event for command subscriptions."""
        try:
            self._log_status_retroactive("active", "tool:shell", command, timestamp)
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=self.instance_name, tool="codex")

    def _log_user_prompt(self, timestamp: str) -> None:
        """Log user prompt status event (active:prompt)."""
        try:
            self._log_status_retroactive("active", "prompt", "", timestamp)
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=self.instance_name, tool="codex")


# ==================== Thread Helpers ====================


def _run_transcript_watcher_thread(
    *,
    instance_name: str,
    process_id: str | None,
    watcher: TranscriptWatcher,
    running_flag: list[bool],
    poll_interval: float = 5.0,
) -> None:
    """Background thread that periodically syncs transcript."""
    while running_flag[0]:
        if process_id:
            try:
                from ..core.db import get_process_binding

                binding = get_process_binding(process_id)
                bound_name = binding.get("instance_name") if binding else None
                if bound_name and bound_name != instance_name:
                    instance_name = bound_name
                    watcher.instance_name = bound_name
            except Exception:
                pass

        # Get transcript path from instance DB (may be set by notify hook)
        try:
            from ..core.db import get_instance

            inst = get_instance(instance_name)
            if inst and inst.get("transcript_path"):
                watcher.set_transcript_path(inst["transcript_path"])
        except Exception:
            pass

        # Sync any new edits
        try:
            watcher.sync()
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=instance_name, tool="codex")

        # Sleep in small increments to check running flag
        sleep_until = time.monotonic() + poll_interval
        while running_flag[0] and time.monotonic() < sleep_until:
            time.sleep(0.5)


def _inject_message(port: int, message: str) -> bool:
    """Inject text to PTY via TCP (same primitive used by other integrations)."""
    return _pty_inject(port, message)


def _run_receiver_thread(
    *,
    process_id: str | None,
    instance_name: str,
    wrapper: PTYWrapper,
    gate_wrapper: GateWrapperView,
    running_flag: list[bool],
) -> None:
    from ..core.db import get_instance
    from ..core.messages import get_unread_messages

    current = {"name": instance_name}

    notify = NotifyServer()
    if not notify.start():
        log_warn("pty", "notify.fail", instance=instance_name, tool="codex")

    register_notify_port(current["name"], notify.port, notify.server is not None)

    def running() -> bool:
        return bool(running_flag[0])

    def _refresh_binding() -> bool:
        """Refresh binding from DB. Returns True if binding is valid."""
        if not process_id:
            return True  # No process_id means ad-hoc mode, always valid
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
        register_notify_port(current["name"], notify.port, notify.server is not None)
        return True

    def get_cursor() -> int:
        """Get current cursor position for delivery verification."""
        _refresh_binding()
        return get_instance_cursor(current["name"])

    def has_pending() -> bool:
        """Return True when there are unread messages to deliver."""
        if not _refresh_binding():
            return False  # Binding gone - don't claim messages for stolen identity
        inst = get_instance(current["name"])
        if not inst:  # Row exists = participating
            return False
        messages, _ = get_unread_messages(current["name"], update_position=False)
        return bool(messages)

    def try_deliver() -> bool:
        """Inject instruction to run hcom listen."""
        if not _refresh_binding():
            return False  # Binding gone - don't deliver
        if not gate_wrapper.actual_port:
            return False
        text = build_listen_instruction(current["name"])
        return _inject_message(gate_wrapper.actual_port, text)

    def try_enter() -> bool:
        """Inject just Enter key (for retry when text already in buffer)."""
        if not _refresh_binding():
            return False  # Binding gone - don't deliver
        if not gate_wrapper.actual_port:
            return False
        return _inject_enter(gate_wrapper.actual_port)

    gate = DeliveryGate(
        require_idle=True,  # Codex: transcript parsing tracks status
        require_ready_prompt=True,
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
            notifier=HeartbeatNotifier(notify, lambda: current["name"]),
            wrapper=typing.cast(PTYLike, gate_wrapper),
            has_pending=has_pending,
            try_deliver=try_deliver,
            try_enter=try_enter,
            gate=gate,
            retry=retry,
            idle_wait=30.0,
            start_pending=False,
            instance_name=instance_name,
            get_cursor=get_cursor,
            # Longer timeout for Codex: agent runs `hcom listen` (not instant hook)
            verify_timeout=10.0,
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
            tool="codex",
            traceback=tb,
        )
        set_status(current["name"], "error", context="pty:crash", detail=str(e)[:200])
    finally:
        notify.close()


def _add_codex_developer_instructions(codex_args: list[str], instance_name: str) -> list[str]:
    """Add hcom bootstrap to codex developer_instructions.

    Builds full bootstrap and adds via -c developer_instructions=... flag.
    If user also provided developer_instructions (via system_prompt in launcher),
    bootstrap comes first, then separator, then user content below.

    Skip for resume/review subcommands (not interactive launch).
    """
    from ..tools.codex.args import resolve_codex_args
    from ..core.bootstrap import get_bootstrap

    spec = resolve_codex_args(codex_args, None)

    # Skip non-interactive modes and resume/fork (already has bootstrap from original session)
    if spec.subcommand in ("exec", "e", "resume", "fork", "review"):
        return list(codex_args)

    # Build bootstrap for this instance
    bootstrap = get_bootstrap(instance_name, tool="codex")

    # Check if developer_instructions already exists in -c flags
    existing_dev_instructions: str | None = None
    tokens = list(spec.clean_tokens)

    # Scan for -c developer_instructions=... pattern
    i = 0
    dev_instr_idx: int | None = None
    while i < len(tokens):
        token = tokens[i]
        # Handle -c developer_instructions=value (equals syntax)
        if token.startswith("-c=developer_instructions=") or token.startswith("--config=developer_instructions="):
            existing_dev_instructions = token.split("=", 2)[2] if token.count("=") >= 2 else ""
            dev_instr_idx = i
            break
        # Handle -c developer_instructions=value (space syntax)
        if token in ("-c", "--config") and i + 1 < len(tokens):
            next_token = tokens[i + 1]
            if next_token.startswith("developer_instructions="):
                existing_dev_instructions = next_token.split("=", 1)[1]
                dev_instr_idx = i
                break
        i += 1

    # Build combined developer instructions
    if existing_dev_instructions:
        # Bootstrap first, then user content below separator
        combined = f"{bootstrap}\n---\n{existing_dev_instructions}"
        # Remove existing developer_instructions from tokens
        if dev_instr_idx is not None:
            if tokens[dev_instr_idx] in ("-c", "--config"):
                # Remove both -c and the value
                tokens = tokens[:dev_instr_idx] + tokens[dev_instr_idx + 2 :]
            else:
                # Remove single equals-style token
                tokens = tokens[:dev_instr_idx] + tokens[dev_instr_idx + 1 :]
    else:
        # No existing - just use bootstrap
        combined = bootstrap

    # Prepend -c developer_instructions=... to tokens
    result_tokens = ["-c", f"developer_instructions={combined}"] + tokens

    # Prepend subcommand if present
    if spec.subcommand:
        result_tokens = [spec.subcommand] + result_tokens

    return result_tokens


def _get_sandbox_flags(mode: str) -> list[str]:
    """Get sandbox flags for the given mode.

    Modes:
    - workspace: Normal codex - edits auto-approved in workspace (default)
    - untrusted: Read-only - all edits need Y/n approval, but hcom works
    - danger-full-access: Full access (no sandbox restrictions)
    - none: No flags - raw codex folder trust (hcom may not work)
    """
    return {
        "workspace": ["--sandbox", "workspace-write"],
        "untrusted": ["--sandbox", "workspace-write", "-a", "untrusted"],
        "danger-full-access": ["--sandbox", "danger-full-access"],
        "none": [],
    }.get(mode, ["--sandbox", "workspace-write"])  # default to workspace


def _ensure_hcom_writable(tokens: list[str]) -> list[str]:
    """Ensure --add-dir ~/.hcom is present so hcom can write to its DB.

    Codex's --add-dir flag is IGNORED in read-only sandbox mode, but required
    for workspace-write mode to allow hcom DB writes.

    If no sandbox flags are present (mode="none"), skip adding --add-dir
    since user is using codex's own folder settings and takes responsibility
    for hcom DB access.
    """
    from ..core.paths import hcom_path
    from ..tools.codex.args import resolve_codex_args

    spec = resolve_codex_args(tokens, None)

    # If no sandbox flags, assume mode="none" - skip --add-dir
    # User takes responsibility for hcom DB access
    has_sandbox = spec.has_flag(
        ["--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox"],
        ["--sandbox=", "-s="],
    )
    if not has_sandbox:
        return tokens  # Mode is "none", user's own settings

    hcom_dir = str(hcom_path())

    # Check if --add-dir with hcom path already exists
    for i, token in enumerate(spec.clean_tokens):
        if token == "--add-dir" and i + 1 < len(spec.clean_tokens):
            if spec.clean_tokens[i + 1] == hcom_dir:
                return tokens  # Already present

    # Add --add-dir at the beginning
    return ["--add-dir", hcom_dir] + tokens


# ==================== Main Runner ====================


def run_codex_with_hcom(
    instance_name: str,
    codex_args: list[str] | None = None,
    resume_thread_id: str | None = None,
) -> int:
    """Run Codex with hcom listen-push integration. Blocks until Codex exits.

    Reads HCOM_PROCESS_ID from env (set by launcher).
    """
    process_id = os.environ.get("HCOM_PROCESS_ID")

    # Resolve instance name from process binding if available
    if process_id:
        try:
            from ..core.db import get_process_binding

            binding = get_process_binding(process_id)
            bound_name = binding.get("instance_name") if binding else None
            if bound_name:
                instance_name = bound_name
        except Exception:
            pass

    # Inject sandbox flags based on HCOM_CODEX_SANDBOX_MODE config
    # This happens BEFORE any other processing so merge_codex_args handles precedence
    sandbox_mode = os.environ.get("HCOM_CODEX_SANDBOX_MODE", "workspace")
    sandbox_flags = _get_sandbox_flags(sandbox_mode)
    if sandbox_flags:
        codex_args = sandbox_flags + list(codex_args or [])
    else:
        codex_args = list(codex_args or [])

    # Warn if mode is "none" (no sandbox flags = hcom likely broken)
    if sandbox_mode == "none":
        print(
            "[hcom] Warning: Sandbox mode is 'none' - --add-dir ~/.hcom disabled.",
            file=sys.stderr,
        )
        print(
            "[hcom] hcom commands may fail unless HCOM_DIR is within workspace.",
            file=sys.stderr,
        )

    # Ensure --add-dir ~/.hcom is present for hcom DB writes (skips if mode="none")
    codex_args = _ensure_hcom_writable(codex_args)

    # Add bootstrap to developer_instructions (must happen after instance_name resolved)
    codex_args = _add_codex_developer_instructions(codex_args, instance_name)

    # Hook setup moved to launcher.launch() - single source of truth
    # (launcher sets up hooks before creating PTY script that calls this function)

    # Register instance (pre-registered by launcher).
    # For resume: use bind_session_to_process to handle canonical instance lookup
    try:
        from ..core.instances import bind_session_to_process

        if resume_thread_id:
            # Pre-bind known session_id - handles canonical lookup and merging
            canonical = bind_session_to_process(resume_thread_id, process_id)
            if canonical and canonical != instance_name:
                instance_name = canonical
    except Exception as e:
        log_error("pty", "status.change", e, instance=instance_name, tool="codex")

    # Required for Codex notify hook handler to activate (it no-ops otherwise).
    os.environ["HCOM_LAUNCHED"] = "1"

    running_flag: list[bool] = [True]

    # Apply Termux shebang bypass (explicit node invocation)
    command = termux_shebang_bypass(["codex", *codex_args], "codex")

    wrapper = PTYWrapper(
        command=command,
        instance_name=instance_name,
        port=0,
        ready_pattern=CLAUDE_CODEX_READY_PATTERN,
    )
    gate_wrapper = GateWrapperView(wrapper)

    original_wait = wrapper.wait_for_ready
    transcript_watcher = TranscriptWatcher(instance_name)

    def patched_wait(pattern: bytes | None = None, timeout: float = 30.0) -> bool:
        if pattern is None:
            pattern = CLAUDE_CODEX_READY_PATTERN
        ok = original_wait(pattern, timeout)
        if ok and wrapper.actual_port:
            # PTY is ready - log ready event (status_context='new' triggers ready event log)
            from ..core.instances import set_status

            set_status(instance_name, "listening", "start")

            # Bootstrap delivered via developer_instructions at launch.
            # Start receiver thread for ongoing message delivery.
            threading.Thread(
                target=_run_receiver_thread,
                kwargs={
                    "process_id": process_id,
                    "instance_name": instance_name,
                    "wrapper": wrapper,
                    "gate_wrapper": gate_wrapper,
                    "running_flag": running_flag,
                },
                daemon=True,
            ).start()
            # Start transcript watcher for file edit status tracking
            threading.Thread(
                target=_run_transcript_watcher_thread,
                kwargs={
                    "instance_name": instance_name,
                    "process_id": process_id,
                    "watcher": transcript_watcher,
                    "running_flag": running_flag,
                    "poll_interval": 5.0,
                },
                daemon=True,
            ).start()
        return ok

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

    try:
        exit_code = wrapper.run()
        return exit_code
    finally:
        running_flag[0] = False
        resolved_name = instance_name
        # Mark instance as exited
        try:
            from ..core.db import get_process_binding, delete_process_binding
            from ..core.instances import set_status

            if process_id:
                binding = get_process_binding(process_id)
                bound_name = binding.get("instance_name") if binding else None
                if bound_name:
                    resolved_name = bound_name
            set_status(resolved_name, "inactive", STATUS_CONTEXT_EXIT_CLOSED)
            # Stop instance (delete row, log life event)
            from ..core.tool_utils import stop_instance

            stop_instance(resolved_name, initiated_by="pty", reason="closed")
            # Clean up process binding (prevents stale entries)
            if process_id:
                delete_process_binding(process_id)
        except Exception as e:
            log_error("pty", "pty.exit", e, instance=instance_name, tool="codex")
        try:
            update_heartbeat(resolved_name)
        except Exception:
            pass


def create_codex_runner_script(
    instance_name: str,
    process_id: str,
    cwd: str,
    codex_args: list[str] | None = None,
    resume_thread_id: str | None = None,
    run_here: bool = False,
    sandbox_mode: str = "workspace",
) -> str:
    """Create a bash script that runs Codex with hcom integration."""
    from ..core.paths import hcom_path, LAUNCH_DIR

    script_file = str(hcom_path(LAUNCH_DIR, f"codex_{instance_name}_{random.randint(1000, 9999)}.sh"))
    python_path = sys.executable
    module_dir = Path(__file__).parent.parent

    codex_args_str = repr(codex_args or [])
    resume_str = repr(resume_thread_id) if resume_thread_id else "None"

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
    hcom_dir_export = f'export HCOM_DIR="{hcom_dir}"' if hcom_dir else "# HCOM_DIR not set"

    # sandbox_mode is passed as parameter from launch_codex_pty (from env dict)

    script_content = f'''#!/bin/bash
# Codex hcom runner (listen-push) for {instance_name}
cd {shlex.quote(cwd)}

unset CLAUDECODE
export HCOM_LAUNCHED=1
export HCOM_PROCESS_ID="{process_id}"
export HCOM_CODEX_SANDBOX_MODE="{sandbox_mode}"
{hcom_dir_export}
{"export HCOM_VIA_SHIM=1" if os.environ.get("HCOM_VIA_SHIM") else "# no shim"}

export PYTHONPATH="{module_dir.parent}:$PYTHONPATH"
{shlex.quote(python_path)} -c "
import sys
sys.path.insert(0, '{module_dir.parent}')
from hcom.pty.codex import run_codex_with_hcom
sys.exit(run_codex_with_hcom('{instance_name}', codex_args={codex_args_str}, resume_thread_id={resume_str}))
"{exec_line}
'''
    with open(script_file, "w") as f:
        f.write(script_content)
    os.chmod(script_file, 0o755)
    return script_file


def launch_codex_pty(
    cwd: str,
    env: dict,
    instance_name: str,
    codex_args: list[str] | None = None,
    resume_thread_id: str | None = None,
    run_here: bool = False,
) -> str | None:
    """Launch Codex in a terminal via PTY wrapper. Returns instance name."""
    from ..terminal import launch_terminal

    process_id = env.get("HCOM_PROCESS_ID")
    if not process_id:
        log_error(
            "pty",
            "pty.exit",
            "HCOM_PROCESS_ID not set in env",
            instance=instance_name,
            tool="codex",
        )
        return None
    # Get sandbox mode from env (set by launcher from config)
    sandbox_mode = env.get("HCOM_CODEX_SANDBOX_MODE", "workspace")
    script_file = create_codex_runner_script(
        instance_name,
        process_id,
        cwd,
        codex_args,
        resume_thread_id,
        run_here=run_here,
        sandbox_mode=sandbox_mode,
    )
    success = launch_terminal(f"bash {shlex.quote(script_file)}", env, cwd=cwd, run_here=run_here)
    return instance_name if success else None


__all__ = [
    "launch_codex_pty",
    "run_codex_with_hcom",
]
