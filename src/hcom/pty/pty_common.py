"""Shared PTY utilities for Gemini, Codex, and Claude PTY modes.

Extracts common infrastructure to reduce duplication across pty/*.py files.
"""

from __future__ import annotations

import socket
import select
import sys
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .pty_wrapper import PTYWrapper

# ==================== Termux Shebang Bypass ====================

# Default Termux node path
TERMUX_NODE_PATH = "/data/data/com.termux/files/usr/bin/node"


def termux_shebang_bypass(command: list[str], tool: str) -> list[str]:
    """Apply Termux shebang bypass for npm-installed CLI tools.

    On Termux, npm global CLIs have shebangs like #!/usr/bin/env node
    which fail because /usr/bin/env doesn't exist. This function
    rewrites the command to explicitly call node with the tool path.

    Args:
        command: Original command list, e.g. ["gemini", "--arg"]
        tool: Tool name ("gemini" or "codex")

    Returns:
        Modified command list for Termux, or original if not on Termux
        or tool not found.
    """
    import shutil
    from ..shared import is_termux

    if not is_termux():
        return command

    if not command or command[0] != tool:
        return command

    # Find tool path
    tool_path = shutil.which(tool)
    if not tool_path:
        return command  # Let it fail naturally

    # Find node path
    node_path = shutil.which("node") or TERMUX_NODE_PATH

    # Rewrite: ["gemini", ...args] -> ["node", "/path/to/gemini", ...args]
    return [node_path, tool_path] + command[1:]


# ==================== Magic Strings ====================

# Ready patterns for PTY detection (visible when idle, hidden when user types)
GEMINI_READY_PATTERN = b"Type your message"
CLAUDE_CODEX_READY_PATTERN = b"? for shortcuts"  # Both Claude and Codex use this

# Status contexts
STATUS_CONTEXT_EXIT_STOPPED = "exit:stopped"
STATUS_CONTEXT_EXIT_KILLED = "exit:killed"
STATUS_CONTEXT_EXIT_CLOSED = "exit:closed"


# ==================== Terminal Title ====================


def set_terminal_title(instance_name: str) -> None:
    """Set terminal window and tab title for hcom instance."""
    try:
        title = f"hcom: {instance_name}"
        with open("/dev/tty", "w") as tty_fd:
            tty_fd.write(f"\033]1;{title}\007\033]2;{title}\007")
    except (OSError, IOError):
        pass


# ==================== TCP Injection ====================


def inject_message(port: int, message: str) -> bool:
    """Inject message to PTY via TCP.

    Returns True on success, False on failure.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", port))
            sock.sendall(message.encode("utf-8"))
        return True
    except Exception:
        return False


def inject_enter(port: int) -> bool:
    """Inject just Enter key to PTY via TCP (no text).

    Used for retry when text was injected but Enter failed.
    Sends newline which gets stripped, leaving only the Enter key.
    """
    return inject_message(port, "\n")


# ==================== Instance Status ====================


def get_instance_status(instance_name: str) -> tuple[str, str]:
    """Get instance status from DB.

    Returns (status, detail) tuple. Status is one of:
    - 'listening': Safe to inject (heartbeat-proven current)
    - 'active': Processing, do not inject
    - 'blocked': Approval prompt, do not inject
    - 'unknown': Instance not found
    """
    try:
        from ..core.db import get_db

        conn = get_db()
        cursor = conn.execute(
            "SELECT status, status_detail FROM instances WHERE name = ?",
            (instance_name,),
        )
        row = cursor.fetchone()
        if row:
            return row["status"] or "unknown", row["status_detail"] or ""
        return "unknown", ""
    except Exception:
        return "unknown", ""


# ==================== TCP Notification Server ====================


class NotifyServer:
    """TCP notification server for instant wake on message arrival.

    Used by poll threads to block efficiently instead of busy-polling.
    notify_all_instances() connects to this port to wake the poll thread.
    """

    def __init__(self):
        self.server: socket.socket | None = None
        self.port: int | None = None

    def start(self) -> bool:
        """Start the notification server. Returns True on success."""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(("127.0.0.1", 0))
            self.server.listen(128)
            self.server.setblocking(False)
            self.port = self.server.getsockname()[1]
            return True
        except Exception:
            return False

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait for notification or timeout. Returns True if notified."""
        if not self.server:
            time.sleep(0.1)
            return False

        try:
            readable, _, _ = select.select([self.server], [], [], timeout)
            if readable:
                # Drain all pending notifications
                while True:
                    try:
                        conn, _ = self.server.accept()
                        conn.close()
                    except BlockingIOError:
                        break
                return True
            return False
        except Exception:
            time.sleep(0.1)
            return False

    def close(self) -> None:
        """Close the notification server."""
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
            self.port = None


# ==================== Poll Thread Helpers ====================


def wait_for_process_registration(
    process_id: str,
    timeout: int = 30,
    log_fn: Callable[..., None] | None = None,
    require_session_id: bool = True,
) -> tuple[str | None, dict | None]:
    """Wait for process binding to resolve to a session-bound instance.

    Args:
        process_id: HCOM_PROCESS_ID (pre-generated by launcher)
        timeout: Max seconds to wait
        log_fn: Optional logging function
        require_session_id: If True, wait until session_id is set (default).
            If False, return as soon as instance_name is available.

    Returns:
        (instance_name, instance) or (None, None) on timeout
    """
    from ..core.db import get_instance, get_process_binding

    last_status = "no_binding"
    last_instance_name = None

    for i in range(timeout):
        binding = get_process_binding(process_id)
        instance_name = binding.get("instance_name") if binding else None
        if instance_name:
            last_instance_name = instance_name
            instance = get_instance(instance_name)
            if instance:
                if instance.get("session_id") or not require_session_id:
                    return instance_name, instance
                last_status = "waiting_session_id"
                if log_fn and i % 5 == 0:  # Log every 5 seconds
                    log_fn(
                        f"Instance {instance_name} pre-registered, waiting for session_id..."
                    )
            else:
                last_status = "instance_not_found"
        else:
            if log_fn and i % 5 == 0:  # Log every 5 seconds
                log_fn("Waiting for process binding registration...")
        time.sleep(1.0)

    # Timeout - provide diagnostic info
    if log_fn:
        if last_status == "waiting_session_id":
            log_fn(
                f"TIMEOUT: Instance {last_instance_name} found but session_id never set. "
                "SessionStart hook may have failed."
            )
        elif last_status == "instance_not_found":
            log_fn(
                f"TIMEOUT: Process binding found instance {last_instance_name} but "
                "instance not in DB. Launcher may have failed."
            )
        else:
            log_fn(
                f"TIMEOUT: Process binding {process_id[:8]}... never registered. "
                "Launcher or SessionStart hook may have failed."
            )

    return None, None


def register_notify_port(
    instance_name: str, notify_port: int | None, tcp_mode: bool
) -> None:
    """Register notify port for PTY-based integrations.

    Uses notify_endpoints table so multiple waiters can coexist (e.g., PTY + `hcom listen`)
    without clobbering a single instances.notify_port.
    """
    from ..core.instances import update_instance_position
    from ..core.db import upsert_notify_endpoint
    from ..core.log import log_info, log_error

    if notify_port:
        try:
            upsert_notify_endpoint(instance_name, "pty", int(notify_port))
            log_info(
                "pty",
                "notify.port.registered",
                instance=instance_name,
                port=notify_port,
            )
        except Exception as e:
            log_error(
                "pty", "notify.port.fail", e, instance=instance_name, port=notify_port
            )
    # Keep tcp_mode as a UI hint; notify port is stored in notify_endpoints only.
    update_instance_position(instance_name, {"tcp_mode": tcp_mode})


def update_heartbeat(instance_name: str) -> None:
    """Update last_stop timestamp to prove instance is alive."""
    from ..core.instances import update_instance_position

    update_instance_position(instance_name, {"last_stop": int(time.time())})


# ==================== Gate Wrapper View ====================


class GateWrapperView:
    """Thin adapter for PTYWrapper to satisfy PTYLike protocol."""

    def __init__(self, wrapper: PTYWrapper) -> None:
        self._wrapper = wrapper

    @property
    def actual_port(self) -> int | None:
        return self._wrapper.actual_port

    def is_waiting_approval(self) -> bool:
        return self._wrapper.is_waiting_approval()

    def is_user_active(self) -> bool:
        return self._wrapper.is_user_active()

    def is_ready(self) -> bool:
        return self._wrapper.is_ready()

    def is_output_stable(self, seconds: float) -> bool:
        return self._wrapper.is_output_stable(seconds)

    def is_prompt_empty(self) -> bool:
        """Check if Claude's input box is empty (no user text)."""
        return self._wrapper.is_prompt_empty()


# ==================== Heartbeat Notifier ====================


class HeartbeatNotifier:
    """Wrapper that adds heartbeat updates to a Notifier.

    Wraps any notifier to add heartbeat updates after each wait,
    proving the instance is alive even when idle.

    Args:
        inner: The underlying notifier to wrap
        instance_name: Either a string (static name) or a callable returning
            the current instance name (for dynamic resolution during rebinding)
    """

    def __init__(self, inner, instance_name: str | Callable[[], str]) -> None:
        self._inner = inner
        self._name_resolver = (
            instance_name if callable(instance_name) else lambda: instance_name
        )

    def wait(self, *, timeout: float) -> bool:
        result = self._inner.wait(timeout=timeout)
        try:
            update_heartbeat(self._name_resolver())
        except Exception:
            pass
        return result

    def close(self) -> None:
        self._inner.close()


# ==================== Debounced Idle Checker ====================


class DebouncedIdleChecker:
    """Debounced idle detection for Gemini.

    Gemini fires AfterAgent multiple times per turn. Wait for idle to
    stabilize before considering it final.
    """

    def __init__(self, debounce_seconds: float = 0.4) -> None:
        self._debounce_seconds = debounce_seconds
        self._idle_since: float | None = None

    def is_stable_idle(self, status: str) -> bool:
        """Check if instance has been listening for debounce period."""
        if status != "listening":
            self._idle_since = None
            return False
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
            return False
        return (now - self._idle_since) >= self._debounce_seconds


# ==================== Signal Handler ====================


def create_sighup_handler(
    instance_name: str,
    running_flag: list[bool],
    process_id: str | None = None,
    log_fn: Callable[[str], None] | None = None,
    exit_context: str = STATUS_CONTEXT_EXIT_KILLED,
) -> Callable:
    """Create a SIGHUP handler for terminal close.

    Consistently sets status to inactive before exit.
    """

    def handle_sighup(signum, frame):
        if log_fn:
            log_fn("SIGHUP received - terminal closed")
        running_flag[0] = False
        try:
            from ..core.instances import set_status

            resolved_name = instance_name
            if process_id:
                try:
                    from ..core.db import get_process_binding

                    binding = get_process_binding(process_id)
                    bound_name = binding.get("instance_name") if binding else None
                    if bound_name:
                        resolved_name = bound_name
                except Exception:
                    pass
            set_status(resolved_name, "inactive", exit_context)
            # Stop instance (delete row, log life event)
            from ..core.tool_utils import stop_instance

            reason = (
                exit_context.split(":")[-1] if ":" in exit_context else exit_context
            )
            stop_instance(resolved_name, initiated_by="pty", reason=reason)
            if log_fn:
                log_fn(f"Stopped instance {resolved_name}")
        except Exception as e:
            if log_fn:
                log_fn(f"SIGHUP cleanup error: {e}")
        sys.exit(128 + signum)

    return handle_sighup


# ==================== Cursor Tracking ====================


def get_instance_cursor(instance_name: str) -> int:
    """Get current cursor position (last_event_id) for instance.

    Used by PTY modules to track delivery confirmation:
    - Snapshot cursor before inject
    - If cursor advances after inject, message was delivered/read

    Returns:
        Current last_event_id, or 0 if not found
    """
    from ..core.instances import load_instance_position

    data = load_instance_position(instance_name)
    return data.get("last_event_id", 0) if data else 0


# ==================== Message Preview ====================

# Max length for message preview in PTY trigger
PREVIEW_MAX_LEN = 60


def build_message_preview(instance_name: str, max_len: int = PREVIEW_MAX_LEN) -> str:
    """Build truncated message preview for PTY injection.

    Used by Gemini and Claude PTY modes to show a preview hint in the
    injected trigger while avoiding problematic characters.

    Reuses format_hook_messages but truncates before user message content.
    User content may contain @ chars that trigger autocomplete in some CLIs.
    Full messages delivered via hook's additionalContext.
    """
    from ..core.messages import get_unread_messages, format_hook_messages

    messages, _ = get_unread_messages(instance_name, update_position=False)
    if not messages:
        return "<hcom></hcom>"

    wrapper_open = "<hcom>"
    wrapper_close = "</hcom>"
    wrapper_len = len(wrapper_open) + len(wrapper_close)
    content_max = max_len - wrapper_len
    if content_max <= 0:
        return f"{wrapper_open}{wrapper_close}"

    formatted = format_hook_messages(messages, instance_name)

    # Truncate before user content (after first ": ") to avoid special chars
    colon_pos = formatted.find(": ")
    if colon_pos != -1:
        envelope = formatted[:colon_pos]  # Stop before the colon
        if len(envelope) > content_max:
            if content_max <= 3:
                return f"{wrapper_open}{wrapper_close}"
            return f"{wrapper_open}{envelope[: content_max - 3]}...{wrapper_close}"
        return f"{wrapper_open}{envelope}{wrapper_close}"

    # No colon found, just truncate normally
    if len(formatted) > content_max:
        if content_max <= 3:
            return f"{wrapper_open}{wrapper_close}"
        return f"{wrapper_open}{formatted[: content_max - 3]}...{wrapper_close}"
    return f"{wrapper_open}{formatted}{wrapper_close}"


def build_listen_instruction(instance_name: str) -> str:
    """Build preview + hcom listen instruction for message notification.

    Used by PTY codex and command-line message delivery (adhoc/codex modes)
    to notify instances of pending messages without marking them as read.

    Example: <hcom>luna → you</hcom> | run: hcom listen 1
    """
    from ..core.tool_utils import build_hcom_command

    preview = build_message_preview(instance_name)
    hcom_cmd = build_hcom_command()
    return f"{preview}"
    # return f"{preview} | run: {hcom_cmd} listen 1"


__all__ = [
    # Termux shebang bypass
    "termux_shebang_bypass",
    "TERMUX_NODE_PATH",
    # Core utilities
    "set_terminal_title",
    "inject_message",
    "get_instance_status",
    "NotifyServer",
    "wait_for_process_registration",
    "register_notify_port",
    "update_heartbeat",
    # Magic strings
    "GEMINI_READY_PATTERN",
    "CLAUDE_CODEX_READY_PATTERN",
    "STATUS_CONTEXT_EXIT_STOPPED",
    "STATUS_CONTEXT_EXIT_KILLED",
    "STATUS_CONTEXT_EXIT_CLOSED",
    # Gate wrapper
    "GateWrapperView",
    # Heartbeat notifier
    "HeartbeatNotifier",
    # Debounced idle
    "DebouncedIdleChecker",
    # Signal handler
    "create_sighup_handler",
    # Cursor tracking
    "get_instance_cursor",
    # Message preview
    "PREVIEW_MAX_LEN",
    "build_message_preview",
]
