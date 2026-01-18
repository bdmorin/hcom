"""Notify-driven delivery engine for PTY-based tool integrations.

This module is intentionally self-contained so Gemini/Codex/Claude-PTY can share:
- a single definition of "safe to inject"
- a notify-driven loop (no periodic DB polling when idle)
- bounded retry behavior when delivery is pending but unsafe

Used by: gemini.py, codex.py
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Protocol


class Notifier(Protocol):
    """Wake-up primitive (usually a local TCP notify server)."""

    def wait(self, *, timeout: float) -> bool:
        """Block until notified or timeout. Returns True if notified."""
        ...

    def close(self) -> None: ...


class RetryPolicyProtocol(Protocol):
    """Protocol for retry delay calculation."""

    def delay(self, attempt: int, *, pending_for: float | None = None) -> float:
        """Calculate delay for retry attempt."""
        ...


class PTYLike(Protocol):
    """Subset of PTYWrapper used for safe injection gating."""

    @property
    def actual_port(self) -> int | None: ...

    def is_waiting_approval(self) -> bool: ...

    def is_user_active(self) -> bool: ...

    def is_ready(self) -> bool: ...

    def is_output_stable(self, seconds: float) -> bool: ...

    def is_prompt_empty(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class GateResult:
    safe: bool
    reason: str


@dataclass
class DeliveryLoopState:
    """State tracking for delivery loop debouncing."""

    last_block_reason: str | None = None
    last_block_log: float = 0.0


def _log_gate_block(
    state: DeliveryLoopState, reason: str, instance_name: str = ""
) -> None:
    """Log gate block with 5-second debounce for same reason."""
    from ..core.log import log_info

    now = time.monotonic()
    # Only log if reason changed or 5+ seconds since last log
    if reason != state.last_block_reason or (now - state.last_block_log) >= 5.0:
        log_info("pty", "gate.blocked", instance=instance_name, reason=reason)
        state.last_block_reason = reason
        state.last_block_log = now


@dataclass(frozen=True, slots=True)
class DeliveryGate:
    """Conservative 'safe to inject' gate.

    This gate answers one question:
    "If we inject a single line + Enter right now, will it land as a fresh user turn
    without clobbering an approval prompt, a running command, or the user's typing?"

    Gate checks (in order):
    - require_idle: DB status must be "listening" (set by hooks after turn completes).
        Claude/Gemini hooks also set status="blocked" on approval which fails this check.
    - block_on_approval: No pending approval prompt (OSC9 detection in PTY)
    - block_on_user_activity: No keystrokes within cooldown (default 0.5s)
    - require_ready_prompt: Ready pattern visible on screen (e.g., "? for shortcuts").
        Pattern hidden when user has uncommitted text or is in a submenu (slash menu).
        Note: Claude hides this in accept-edits mode, so Claude disables this check.
    - require_prompt_empty: Check if prompt has no user text (Claude-specific).
        Detects static placeholders (Try "...") and LLM suggestions (↵ send).
    - require_output_stable_seconds: Screen unchanged for N seconds (default 1.0s)
    """

    require_idle: bool = False
    require_ready_prompt: bool = True
    require_prompt_empty: bool = False  # Claude enables this
    require_output_stable_seconds: float = 1.0
    block_on_user_activity: bool = True
    block_on_approval: bool = True

    def evaluate(
        self, *, wrapper: PTYLike, is_idle: bool | None = None, instance_name: str = ""
    ) -> GateResult:
        """Evaluate gate conditions. Returns GateResult with safe=True/False and reason.

        Note: This method does NOT log. Logging is handled by the delivery loop
        via _log_gate_block() with debounce to reduce log spam.
        """
        if self.require_idle and not is_idle:
            return GateResult(False, "not_idle")
        if self.block_on_approval and wrapper.is_waiting_approval():
            return GateResult(False, "approval")
        if self.block_on_user_activity and wrapper.is_user_active():
            return GateResult(False, "user_active")
        if self.require_ready_prompt and not wrapper.is_ready():
            return GateResult(False, "not_ready")
        if self.require_prompt_empty and not wrapper.is_prompt_empty():
            return GateResult(False, "prompt_has_text")
        if self.require_output_stable_seconds > 0 and not wrapper.is_output_stable(
            self.require_output_stable_seconds
        ):
            return GateResult(False, "output_unstable")
        return GateResult(True, "ok")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry schedule when delivery is pending but unsafe."""

    initial: float = 0.25
    maximum: float = 2.0
    multiplier: float = 2.0

    def delay(self, attempt: int, *, pending_for: float | None = None) -> float:
        if attempt <= 0:
            return 0.0
        d = self.initial * (self.multiplier ** (attempt - 1))
        return min(self.maximum, d)


@dataclass(frozen=True, slots=True)
class TwoPhaseRetryPolicy:
    """Retry policy with a warm phase and a slower phase.

    Keeps retry maximum low for the first N seconds after messages become pending,
    then allows a higher maximum (lower overhead) if the tool stays unsafe.
    """

    initial: float = 0.25
    multiplier: float = 2.0
    warm_maximum: float = 2.0
    warm_seconds: float = 60.0
    cold_maximum: float = 5.0

    def delay(self, attempt: int, *, pending_for: float | None = None) -> float:
        if attempt <= 0:
            return 0.0
        d = self.initial * (self.multiplier ** (attempt - 1))
        max_delay = self.warm_maximum
        if pending_for is not None and pending_for >= self.warm_seconds:
            max_delay = self.cold_maximum
        return min(max_delay, d)


def _update_gate_block_status(
    instance_name: str,
    reason: str,
    block_since: float | None,
    current_status: str | None,
    current_context: str | None,
) -> float:
    """Update status when gate blocks delivery for 2+ seconds.

    Only updates if current status is "listening" - don't overwrite active/blocked.
    Returns updated block_since timestamp.

    Uses set_gate_status() for tui:* contexts (no event logged) to avoid event bloat.
    Uses set_status() for pty:approval (blocked) which is a significant state change.
    """
    now = time.monotonic()
    if block_since is None:
        return now

    # Only update status if instance is currently listening
    # Don't overwrite active/blocked status
    if current_status != "listening":
        return block_since

    # After 2 seconds of blocking, log the reason
    if (now - block_since) >= 2.0:
        # Use hyphens in context to match existing tui:not-ready format
        reason_formatted = reason.replace("_", "-")

        # Approval waiting = blocked status (consistent with Claude hooks)
        # This uses set_status() because blocked is a significant state change
        if reason_formatted == "approval":
            if current_context != "pty:approval":
                from ..core.instances import set_status

                set_status(
                    instance_name,
                    "blocked",
                    context="pty:approval",
                    detail="waiting for user approval",
                )
        else:
            # tui:* contexts use set_gate_status() - no event logged
            context = f"tui:{reason_formatted}"
            if current_context != context:
                from ..core.instances import set_gate_status

                detail_map = {
                    "not-idle": "waiting for idle status",
                    "user-active": "user is typing",
                    "not-ready": "prompt not visible",
                    "output-unstable": "output still streaming",
                    "prompt-has-text": "uncommitted text in prompt",
                }
                set_gate_status(
                    instance_name,
                    context=context,
                    detail=detail_map.get(reason_formatted, reason),
                )
    return block_since


def _clear_gate_block_status(
    instance_name: str, current_status: str | None, current_context: str | None
) -> None:
    """Clear gate block status after successful delivery.

    Uses set_gate_status() for tui:* contexts (no event logged).
    Uses set_status() for pty:approval since transitioning from blocked is significant.
    """
    # Clear listening with tui: context (no event logged)
    if (
        current_status == "listening"
        and current_context
        and current_context.startswith("tui:")
    ):
        from ..core.instances import set_gate_status

        set_gate_status(instance_name, context="", detail="")
    # Clear blocked with pty:approval context (Codex approval cleared) - logs event
    elif current_status == "blocked" and current_context == "pty:approval":
        from ..core.instances import set_status

        set_status(instance_name, "listening", context="ready")


def run_notify_delivery_loop(
    *,
    running: Callable[[], bool],
    notifier: Notifier,
    wrapper: PTYLike,
    has_pending: Callable[[], bool],
    try_deliver: Callable[[], bool],
    try_enter: Callable[[], bool] | None = None,
    is_idle: Callable[[], bool] | None = None,
    gate: DeliveryGate,
    retry: RetryPolicyProtocol = RetryPolicy(),
    idle_wait: float = 30.0,
    start_pending: bool = False,
    instance_name: str = "",
    get_cursor: Callable[[], int] | None = None,
    verify_timeout: float = 2.0,
    max_verify_retries: int = 5,
) -> None:
    """Run a notify-driven delivery loop with delivery verification.

    Design goals:
    - Zero periodic DB polling when there are no pending messages.
    - Delivery attempts happen only after a wake event or bounded retry tick.
    - When unsafe (not at prompt, user typing, approval), retry backs off.
    - Verify delivery via cursor advance (hook reads messages, advances cursor).

    States:
    - idle: no pending messages, sleeping on notifier
    - pending: messages exist, waiting for safe gate to inject
    - verifying: injected, waiting for cursor advance to confirm delivery

    Args:
        get_cursor: Callback returning current cursor position (last_event_id).
            If provided, enables delivery verification. If None, assumes
            try_deliver success = delivery success (legacy behavior).
        verify_timeout: Max seconds to wait for cursor advance before retry.
        try_enter: Callback to inject just Enter key (for retry when text injected but Enter failed).
            If None, retries use try_deliver (full injection).
        max_verify_retries: Max retries before giving up on delivery (default 5).
    """

    # State: 'idle' | 'pending' | 'verifying'
    state = "pending" if start_pending else "idle"
    attempt = 0
    pending_since: float | None = time.monotonic() if start_pending else None
    block_since: float | None = None

    # Verification state (only used when get_cursor provided)
    cursor_before: int = 0
    injected_at: float = 0.0
    verify_retries: int = 0  # Track retries in verifying state

    # Log debounce state
    log_state = DeliveryLoopState()

    def _is_idle() -> bool:
        return is_idle() if is_idle is not None else True

    def _get_current_status() -> tuple[str | None, str | None]:
        """Get (status, context) from DB."""
        if not instance_name:
            return None, None
        try:
            from ..core.db import get_db

            row = (
                get_db()
                .execute(
                    "SELECT status, status_context FROM instances WHERE name = ?",
                    (instance_name,),
                )
                .fetchone()
            )
            if row:
                return row["status"], row["status_context"]
            return None, None
        except Exception:
            return None, None

    def _log_verify_timeout() -> None:
        """Log delivery timeout for debugging."""
        from ..core.log import log_warn

        log_warn(
            "pty", "delivery.timeout", instance=instance_name, tool="push_delivery"
        )

    def _log_state(new_state: str, reason: str = "") -> None:
        """Log state transition for debugging."""
        from ..core.log import log_info

        log_info(
            "pty",
            "delivery.state",
            state=new_state,
            reason=reason,
            instance=instance_name,
        )

    try:
        while running():
            # === IDLE STATE ===
            if state == "idle":
                notifier.wait(timeout=idle_wait)
                if not running():
                    break
                if has_pending():
                    state = "pending"
                    pending_since = time.monotonic()
                    _log_state("pending", "messages_arrived")
                continue

            # === VERIFYING STATE ===
            if state == "verifying":
                # Check if cursor advanced (hook read the messages)
                if get_cursor is not None:
                    current_cursor = get_cursor()
                    if current_cursor > cursor_before:
                        # Delivery confirmed! Check for more messages.
                        if instance_name:
                            status, context = _get_current_status()
                            _clear_gate_block_status(instance_name, status, context)
                        if has_pending():
                            state = "pending"
                            _log_state("pending", "cursor_advanced_more_messages")
                        else:
                            state = "idle"
                            pending_since = None
                            _log_state("idle", "cursor_advanced_delivered")
                        attempt = 0
                        block_since = None
                        verify_retries = 0
                        continue

                    # Check verification timeout
                    elapsed = time.monotonic() - injected_at
                    if elapsed > verify_timeout:
                        # Timeout - Enter key likely failed, retry injection
                        # Partial gate check: skip is_ready/is_output_stable (our failed injection)
                        # but still respect user_active, approval, idle
                        _log_verify_timeout()

                        # Check max retries
                        if verify_retries >= max_verify_retries:
                            from ..core.log import log_error

                            log_error(
                                "pty",
                                "delivery.max_retries",
                                f"max retries ({verify_retries}) exceeded",
                                instance=instance_name,
                                tool="push_delivery",
                            )
                            # Give up - go back to pending, will try fresh injection
                            state = "pending"
                            verify_retries = 0
                            attempt += 1
                            _log_state("pending", "max_retries_exceeded")
                            continue

                        # Check critical gates only (not ready/output_stable)
                        if gate.block_on_approval and wrapper.is_waiting_approval():
                            # Can't retry during approval - stay in verifying, wait
                            notifier.wait(timeout=0.5)
                            continue
                        if gate.block_on_user_activity and wrapper.is_user_active():
                            # User started typing - wait for them
                            notifier.wait(timeout=0.5)
                            continue
                        if gate.require_idle and not _is_idle():
                            # Agent busy - wait
                            notifier.wait(timeout=0.5)
                            continue

                        # Retry: first attempt = Enter only, subsequent = full injection
                        cursor_before = get_cursor()
                        if verify_retries == 0 and try_enter is not None:
                            # First retry: just send Enter (text already in buffer)
                            ok = try_enter()
                            _log_state("verifying", "retry_enter_only")
                        else:
                            # Subsequent retries: full injection (clobber and retry)
                            ok = try_deliver()
                            _log_state("verifying", "retry_full_inject")

                        verify_retries += 1
                        if ok:
                            injected_at = time.monotonic()
                            # Stay in verifying state
                        else:
                            state = "pending"
                            _log_state("pending", "retry_failed")
                            attempt += 1
                        continue

                    # Still waiting for cursor advance - short sleep
                    notifier.wait(timeout=0.25)
                    if not running():
                        break
                    continue
                else:
                    # No get_cursor - can't verify, assume delivered
                    if has_pending():
                        state = "pending"
                    else:
                        state = "idle"
                        pending_since = None
                    attempt = 0
                    block_since = None
                    continue

            # === PENDING STATE ===
            result = gate.evaluate(
                wrapper=wrapper, is_idle=_is_idle(), instance_name=instance_name
            )
            if result.safe:
                # Snapshot cursor before injection (if verification enabled)
                if get_cursor is not None:
                    cursor_before = get_cursor()

                ok = try_deliver()
                if ok:
                    if get_cursor is not None:
                        # Enter verifying state to confirm delivery
                        injected_at = time.monotonic()
                        verify_retries = 0
                        state = "verifying"
                        _log_state("verifying", "injected")
                    else:
                        # Legacy mode: assume delivery succeeded
                        if has_pending():
                            state = "pending"
                        else:
                            state = "idle"
                            pending_since = None
                        attempt = 0
                        block_since = None
                        if instance_name:
                            status, context = _get_current_status()
                            _clear_gate_block_status(instance_name, status, context)
                    continue
                attempt += 1
            else:
                # Gate blocked - log with debounce and update TUI after 2 seconds
                _log_gate_block(log_state, result.reason, instance_name)
                if instance_name:
                    status, context = _get_current_status()
                    block_since = _update_gate_block_status(
                        instance_name, result.reason, block_since, status, context
                    )
                    # Stability-based recovery: if status stuck "active" but output stable 10s,
                    # assume ESC cancelled or similar - flip to listening
                    if (
                        result.reason == "not_idle"
                        and status == "active"
                        and wrapper.is_output_stable(10.0)
                    ):
                        from ..core.instances import set_status
                        from ..core.log import log_info as _log_info

                        set_status(instance_name, "listening", context="pty:recovered")
                        _log_info(
                            "pty",
                            "status.recovered",
                            instance=instance_name,
                            reason="stable_10s",
                        )
                        attempt = 0  # Reset attempts, re-evaluate immediately
                        continue
                attempt += 1

            # Pending but couldn't deliver: wait for retry
            pending_for = (
                (time.monotonic() - pending_since)
                if pending_since is not None
                else None
            )
            delay = retry.delay(attempt, pending_for=pending_for)
            if delay <= 0:
                continue
            notified = notifier.wait(timeout=delay)
            # If notified, snap back to fast retries
            if notified:
                attempt = 0
            if not running():
                break

            # Re-check if still pending
            if not has_pending():
                state = "idle"
                attempt = 0
                pending_since = None
                block_since = None
    finally:
        try:
            notifier.close()
        except Exception:
            pass


class NotifyServerAdapter:
    """Adapter for pty.pty_common.NotifyServer to satisfy Notifier Protocol."""

    def __init__(self) -> None:
        from .pty_common import NotifyServer

        self._notify = NotifyServer()
        self.port: int | None = None

    def start(self) -> bool:
        ok = self._notify.start()
        self.port = self._notify.port
        return ok

    def wait(self, *, timeout: float) -> bool:
        return self._notify.wait(timeout=timeout)

    def close(self) -> None:
        self._notify.close()


__all__ = [
    "DeliveryGate",
    "GateResult",
    "Notifier",
    "NotifyServerAdapter",
    "PTYLike",
    "RetryPolicy",
    "RetryPolicyProtocol",
    "TwoPhaseRetryPolicy",
    "run_notify_delivery_loop",
]
