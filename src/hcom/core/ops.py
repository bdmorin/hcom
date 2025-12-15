"""Core operations for HCOM.

Clean operational layer used by both CLI commands and Python API.
Raises HcomError on failure, returns meaningful data on success.
"""
from __future__ import annotations

from ..shared import HcomError, SenderIdentity


def op_send(
    identity: SenderIdentity,
    message: str,
    envelope: dict[str, str] | None = None
) -> list[str]:
    """Send message.

    Args:
        identity: Sender identity (from resolve_identity or constructed)
        message: Message text (can include @mentions)
        envelope: Optional envelope fields {intent, reply_to, thread}

    Returns:
        List of instance names message was delivered to

    Raises:
        HcomError: If validation fails or delivery fails
    """
    from .messages import send_message
    return send_message(identity, message, envelope=envelope)


def op_stop(instance_name: str, initiated_by: str | None = None, reason: str = 'api') -> bool:
    """Stop (disable) an instance.

    Args:
        instance_name: Instance to stop
        initiated_by: Who initiated the stop (for logging)
        reason: Reason for stop (for logging)

    Returns:
        True if stopped, False if already stopped

    Raises:
        HcomError: If instance not found
    """
    from .instances import load_instance_position
    from ..hooks.utils import disable_instance

    position = load_instance_position(instance_name)
    if not position:
        raise HcomError(f"Instance '{instance_name}' not found")

    if position.get('origin_device_id'):
        raise HcomError(f"Cannot stop remote instance '{instance_name}' via ops - use relay")

    if not position.get('enabled', False):
        return False  # Already stopped

    disable_instance(instance_name, initiated_by=initiated_by, reason=reason)
    return True


def op_start(instance_name: str, initiated_by: str | None = None, reason: str = 'api') -> bool:
    """Start (enable) an instance.

    Args:
        instance_name: Instance to start
        initiated_by: Who initiated the start (for logging)
        reason: Reason for start (for logging)

    Returns:
        True if started, False if already started

    Raises:
        HcomError: If instance not found or cannot be started
    """
    from .instances import load_instance_position, enable_instance

    position = load_instance_position(instance_name)
    if not position:
        raise HcomError(f"Instance '{instance_name}' not found")

    if position.get('origin_device_id'):
        raise HcomError(f"Cannot start remote instance '{instance_name}' via ops - use relay")

    if position.get('enabled', False):
        return False  # Already started

    # Check if background instance has exited permanently
    if position.get('session_ended') and position.get('background'):
        raise HcomError(f"Cannot start '{instance_name}': headless instance has exited permanently")

    enable_instance(instance_name, initiated_by=initiated_by, reason=reason)
    return True


def op_launch(
    count: int,
    claude_args: list[str],
    *,
    launcher: str,
    tag: str | None = None,
    background: bool = False,
    cwd: str | None = None,
) -> dict:
    """Launch Claude instances.

    Args:
        count: Number of instances to launch (1-100)
        claude_args: Claude CLI arguments (already parsed/merged)
        launcher: Name of launching instance (for logging)
        tag: HCOM_TAG value
        background: Headless mode
        cwd: Working directory for instances

    Returns:
        {
            "batch_id": str,
            "launched": int,
            "failed": int,
            "background": bool,
            "log_files": list[str],
        }

    Raises:
        HcomError: If validation fails or no instances launched
    """
    import os
    import time
    import random
    import uuid
    from .config import get_config
    from .runtime import build_claude_env
    from .db import init_db, get_last_event_id, log_event
    from ..terminal import build_claude_command, launch_terminal

    # Validate
    if count <= 0:
        raise HcomError("Count must be positive")
    if count > 100:
        raise HcomError("Too many instances requested (max 100)")
    if tag and '|' in tag:
        raise HcomError('Tag cannot contain "|" characters')

    terminal_mode = get_config().terminal
    if terminal_mode == 'here' and count > 1:
        raise HcomError(f"'here' mode cannot launch {count} instances (it's one terminal window)")

    # Initialize
    init_db()
    working_dir = cwd or os.getcwd()

    # Build environment
    base_env = build_claude_env()
    if tag:
        base_env['HCOM_TAG'] = tag

    # Generate batch ID
    batch_id = str(uuid.uuid4()).split('-')[0]

    # Build claude command
    claude_cmd = build_claude_command(claude_args)

    # Launch instances
    launched = 0
    log_files = []

    for _ in range(count):
        instance_env = base_env.copy()

        # Generate unique launch token
        instance_env['HCOM_LAUNCH_TOKEN'] = str(uuid.uuid4())
        instance_env['HCOM_LAUNCHED'] = '1'
        instance_env['HCOM_LAUNCH_EVENT_ID'] = str(get_last_event_id())
        instance_env['HCOM_LAUNCHED_BY'] = launcher
        instance_env['HCOM_LAUNCH_BATCH_ID'] = batch_id

        if background:
            log_filename = f'background_{int(time.time())}_{random.randint(1000, 9999)}.log'
            instance_env['HCOM_BACKGROUND'] = log_filename

        try:
            if background:
                log_file = launch_terminal(claude_cmd, instance_env, cwd=working_dir, background=True)
                if log_file:
                    log_files.append(log_file)
                    launched += 1
            else:
                if launch_terminal(claude_cmd, instance_env, cwd=working_dir):
                    launched += 1
        except Exception:
            pass  # Continue launching remaining instances

    failed = count - launched

    if launched == 0:
        raise HcomError(f"No instances launched (0/{count})")

    # Log launch event
    try:
        log_event('life', launcher, {
            'action': 'launched',
            'by': launcher,
            'batch_id': batch_id,
            'count_requested': count,
            'launched': launched,
            'failed': failed,
            'background': background,
            'tag': tag or ''
        })
    except Exception:
        pass  # Don't fail if logging fails

    return {
        "batch_id": batch_id,
        "launched": launched,
        "failed": failed,
        "background": background,
        "log_files": log_files,
    }


__all__ = ['op_send', 'op_stop', 'op_start', 'op_launch']
