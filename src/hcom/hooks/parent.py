"""Parent instance hook implementations"""
from __future__ import annotations
from typing import Any
import sys
import os
import time
import json
from pathlib import Path

from ..shared import HCOM_INVOCATION_PATTERN
from ..core.instances import (
    load_instance_position, update_instance_position, set_status, parse_running_tasks
)
from ..core.config import get_config

from .family import check_external_stop_notification
from ..core.db import get_db, get_events_since

from .utils import (
    build_hcom_bootstrap_text, build_hcom_command,
    disable_instance, log_hook_error, notify_instance
)


def _extract_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Extract status detail from tool input based on tool type.

    Only handles tools in PreToolUse matcher (Bash|Task|Write|Edit).
    """
    match tool_name:
        case 'Bash':
            return tool_input.get('command', '')
        case 'Write' | 'Edit':
            return tool_input.get('file_path', '')
        case 'Task':
            return tool_input.get('prompt', '')
        case _:
            return ''


def get_real_session_id(hook_data: dict[str, Any], env_file: str | None) -> str:
    """Extract real session_id from CLAUDE_ENV_FILE path, fallback to hook_data.

    Claude Code has a bug where hook_data.session_id is wrong for fork scenarios
    (--resume X --fork-session). The CLAUDE_ENV_FILE path contains the correct
    session_id since CC creates the directory with Q0() (current WQ.sessionId).

    Note: hook_data.transcript_path also has the wrong session_id in fork scenarios
    (both use the same buggy OLD value). Only CLAUDE_ENV_FILE path is reliable.

    Path structure: ~/.claude/session-env/{session_id}/hook-N.sh
    """
    from .utils import log_hook_error

    hook_session_id = hook_data.get('session_id', '')

    if env_file:
        try:
            parts = Path(env_file).parts
            if 'session-env' in parts:
                idx = parts.index('session-env')
                if idx + 1 < len(parts):
                    candidate = parts[idx + 1]
                    # Sanity check: looks like UUID (36 chars, 4 hyphens)
                    if len(candidate) == 36 and candidate.count('-') == 4:
                        return candidate
        except Exception as e:
            log_hook_error('get_real_session_id:parse_error', e)

    return hook_session_id


def handle_stop_pending(hook_type: str, hook_data: dict[str, Any], instance_name: str, instance_data: dict[str, Any]) -> None:
    """Handle status-only updates for stop pending instances.

    Called by dispatcher for disabled instances with stop_pending=True.
    Allows tracking instance activity between stop and actual session end,
    and ensures cleanup hooks can set final inactive status.
    """
    tool_name = hook_data.get('tool_name', '')

    match hook_type:
        case 'pre':
            detail = _extract_tool_detail(tool_name, hook_data.get('tool_input', {}))
            set_status(instance_name, 'active', f'tool:{tool_name}', detail=detail)
        case 'post':
            # External stop notification (when disabled, hooks still run via stop_pending path)
            if output := check_external_stop_notification(instance_name, instance_data):
                print(json.dumps(output, ensure_ascii=False))
            if instance_data.get('status') == 'blocked':
                set_status(instance_name, 'active', f'approved:{tool_name}')
        case 'notify':
            message = hook_data.get('message', '')
            # Filter out generic "waiting for input" - not a meaningful status change
            if message != "Claude is waiting for your input":
                set_status(instance_name, 'blocked', message)
        case 'poll':
            stop(instance_name, instance_data)
        case 'sessionend':
            set_status(instance_name, 'inactive', f'exit:{hook_data.get("reason", "unknown")}')
            update_instance_position(instance_name, {'session_ended': True, 'stop_pending': False, 'stop_notified': False})
            notify_instance(instance_name)


def sessionstart(hook_data: dict[str, Any]) -> None:
    """Parent SessionStart: write session ID to env file, create instance for HCOM-launched, show initial msg"""
    # Write session ID to CLAUDE_ENV_FILE for automatic identity resolution
    # NOTE: CLAUDE_ENV_FILE only works on Unix (Claude Code doesn't source it on Windows).
    # Windows vanilla instances must use MAPID fallback for identity resolution.
    # Windows HCOM-launched instances get HCOM_LAUNCH_TOKEN via launch env.
    #
    # Note: session_id is already corrected by dispatcher via get_real_session_id()
    # to work around CC fork bug where hook_data.session_id is wrong
    session_id = hook_data.get('session_id')
    env_file = os.environ.get('CLAUDE_ENV_FILE')
    source = hook_data.get('source', '')

    # Handle compaction: reset name_announced so bootstrap is re-injected
    # After compaction, Claude loses the bootstrap context - re-inject on next PostToolUse
    if source == 'compact' and session_id:
        try:
            from ..core.db import find_instance_by_session
            instance_name = find_instance_by_session(session_id)
            if instance_name:
                update_instance_position(instance_name, {'name_announced': False})
        except Exception as e:
            log_hook_error('sessionstart:compact_reset', e)

    if session_id and env_file:
        try:
            from ..core.instances import resolve_instance_name
            instance_name, _ = resolve_instance_name(session_id, get_config().tag)

            lines = [f'\nexport HCOM_SESSION_ID={session_id}']
            lines.append(f'export HCOM_NAME={instance_name}')

            # Export to additional env var if configured
            name_export = os.environ.get('HCOM_NAME_EXPORT')
            if name_export:
                lines.append(f'export {name_export}={instance_name}')

            with open(env_file, 'a', newline='\n') as f:
                f.write('\n'.join(lines) + '\n')
        except Exception as e:
            log_hook_error('sessionstart:env_file_write', e)

    # Store MAPID → session_id mapping for Windows bash command identity resolution
    from ..shared import MAPID
    if session_id and MAPID:
        try:
            from ..core.db import get_db
            conn = get_db()
            conn.execute(
                "INSERT OR REPLACE INTO mapid_sessions (mapid, session_id, updated_at) VALUES (?, ?, ?)",
                (MAPID, session_id, time.time())
            )
            conn.commit()
        except Exception as e:
            log_hook_error('sessionstart:mapid_write', e)

    # Create instance for HCOM-launched (explicit opt-in via launch)
    if os.environ.get('HCOM_LAUNCHED') == '1' and session_id:
        try:
            from ..core.instances import resolve_instance_name, initialize_instance_in_position_file
            # Use resolve_instance_name for collision handling (not get_display_name)
            instance_name, _ = resolve_instance_name(session_id, get_config().tag)
            initialize_instance_in_position_file(
                instance_name,
                session_id=session_id,
                mapid=MAPID,
                enabled=True  # HCOM-launched = opted in
            )
            # Set terminal window and tab title (write directly to tty to bypass Claude's output capture)
            # OSC 1 = tab/icon, OSC 2 = window (same value for both to avoid duplication)
            try:
                title = f'hcom: {instance_name}'
                with open('/dev/tty', 'w') as tty:
                    tty.write(f'\033]1;{title}\007\033]2;{title}\007')
            except (OSError, IOError):
                pass  # No tty (background mode, etc.)
        except Exception as e:
            log_hook_error('sessionstart:create_instance', e)

    # Pull remote events on session start (catch up on messages)
    try:
        from ..relay import pull
        pull()
    except Exception:
        pass  # Silent failure - don't break hook

    # Only show message for HCOM-launched instances
    if os.environ.get('HCOM_LAUNCHED') == '1':
        parts = f"[HCOM is started, you can send messages with the command: {build_hcom_command()} send]"
    else:
        parts = f"[You can start HCOM with the command: {build_hcom_command()} start]"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": parts
        }
    }

    print(json.dumps(output))


def start_task(session_id: str, hook_data: dict[str, Any]) -> None:
    """Task started - enter subagent context

    Creates parent instance if doesn't exist.
    """
    from ..core.db import find_instance_by_session

    # Resolve or create parent instance
    instance_name = find_instance_by_session(session_id)
    if not instance_name:
        # Create minimal parent instance (enabled=False) for tracking
        from ..core.instances import resolve_instance_name, initialize_instance_in_position_file
        instance_name, _ = resolve_instance_name(session_id, get_config().tag)
        initialize_instance_in_position_file(
            instance_name,
            session_id=session_id,
            enabled=False
        )

    # Set active flag (track_subagent will append to subagents array)
    # Don't reset subagents array here - multiple parallel Tasks would overwrite each other
    instance_data = load_instance_position(instance_name)
    running_tasks = parse_running_tasks(instance_data.get('running_tasks', ''))
    running_tasks['active'] = True
    update_instance_position(instance_name, {'running_tasks': json.dumps(running_tasks)})

    # Set status for enabled instances only (with task prompt as detail)
    instance_data = load_instance_position(instance_name)
    if instance_data and instance_data.get('enabled', False):
        detail = _extract_tool_detail('Task', hook_data.get('tool_input', {}))
        set_status(instance_name, 'active', 'tool:Task', detail=detail)


def end_task(session_id: str, hook_data: dict[str, Any], interrupted: bool = False) -> None:
    """Task ended - deliver freeze messages (foreground only), cleanup handled by SubagentStop

    Args:
        session_id: Parent's session ID
        hook_data: Hook data from dispatcher
        interrupted: True if Task was interrupted (UserPromptSubmit handles cleanup)
    """
    from ..core.db import find_instance_by_session

    # Resolve parent instance name
    instance_name = find_instance_by_session(session_id)
    if not instance_name:
        return

    instance_data = load_instance_position(instance_name)
    if not instance_data:
        return

    if interrupted:
        # Interrupted via UserPromptSubmit - don't clear here
        # UserPromptSubmit will check transcripts and clean up dead subagents
        return

    # Deliver freeze messages (SubagentStop handles running_tasks cleanup)
    freeze_event_id = instance_data.get('last_event_id', 0)
    last_event_id = _deliver_freeze_messages(instance_name, freeze_event_id)
    update_instance_position(instance_name, {'last_event_id': last_event_id})


def _disable_tracked_subagents(instance_name: str, instance_data: dict[str, Any]) -> None:
    """Disable subagents in running_tasks with exit:interrupted"""
    running_tasks_json = instance_data.get('running_tasks', '')
    if not running_tasks_json:
        return

    try:
        running_tasks = json.loads(running_tasks_json)
        subagents = running_tasks.get('subagents', []) if isinstance(running_tasks, dict) else []
    except json.JSONDecodeError:
        return

    if not subagents:
        return

    conn = get_db()
    agent_id_map = {r['agent_id']: r['name'] for r in conn.execute(
        "SELECT name, agent_id FROM instances WHERE parent_name = ?", (instance_name,)
    ).fetchall() if r['agent_id']}

    for entry in subagents:
        if (aid := entry.get('agent_id')) and (name := agent_id_map.get(aid)):
            disable_instance(name, initiated_by='system', reason='exit:interrupted')
            set_status(name, 'inactive', 'exit:interrupted')


def _deliver_freeze_messages(instance_name: str, freeze_event_id: int) -> int:
    """Deliver messages from Task freeze period (foreground Tasks only).

    Background Tasks use live delivery instead - parent isn't frozen so messages flow in real-time.
    Returns the last event ID processed (for updating parent position).
    """
    from ..core.messages import should_deliver_message

    # Query freeze period messages
    events = get_events_since(freeze_event_id, event_type='message')

    if not events:
        return freeze_event_id

    # Determine last_event_id from events retrieved
    last_id = max(e['id'] for e in events)

    # Get subagents for message filtering
    conn = get_db()
    subagent_rows = conn.execute(
        "SELECT name, agent_id FROM instances WHERE parent_name = ?",
        (instance_name,)
    ).fetchall()
    subagent_names = [row['name'] for row in subagent_rows]

    # Filter messages with scope validation
    subagent_msgs = []
    parent_msgs = []

    for event in events:
        event_data = event['data']

        sender_name = event_data['from']

        # Build message dict
        msg = {
            'timestamp': event['timestamp'],
            'from': sender_name,
            'message': event_data['text']
        }

        try:
            # Messages FROM subagents
            if sender_name in subagent_names:
                subagent_msgs.append(msg)
            # Messages TO subagents via scope routing
            elif subagent_names and any(
                should_deliver_message(event_data, name, sender_name) for name in subagent_names
            ):
                if msg not in subagent_msgs:  # Avoid duplicates
                    subagent_msgs.append(msg)
            # Messages TO parent via scope routing
            elif should_deliver_message(event_data, instance_name, sender_name):
                parent_msgs.append(msg)
        except (ValueError, KeyError) as e:
            # ValueError: corrupt message data
            # KeyError: old message format missing 'scope' field
            # Only show error if instance is enabled (bypass path can run for disabled)
            inst = load_instance_position(instance_name)
            if inst and inst.get('enabled', False):
                print(
                    f"Error: Invalid message format in event {event['id']}: {e}. "
                    f"Run 'hcom reset logs' to clear old/corrupt messages.",
                    file=sys.stderr
                )
            continue

    # Combine and format messages
    all_relevant = subagent_msgs + parent_msgs
    all_relevant.sort(key=lambda m: m['timestamp'])

    if all_relevant:
        formatted = '\n'.join(f"{msg['from']}: {msg['message']}" for msg in all_relevant)

        # Format subagent list with agent_ids for correlation
        subagent_list = ', '.join(
            f"{row['name']} (agent_id: {row['agent_id']})" if row['agent_id'] else row['name']
            for row in subagent_rows
        ) if subagent_rows else 'none'

        summary = (
            f"[Task tool completed - Message history during Task tool]\n"
            f"Subagents: {subagent_list}\n"
            f"The following {len(all_relevant)} message(s) occurred:\n\n"
            f"{formatted}\n\n"
            f"[End of message history. Subagents have finished and are no longer active.]"
        )

        output = {
            "systemMessage": "[Task subagent messages shown to instance]",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": summary
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    return last_id


def pretooluse(hook_data: dict[str, Any], instance_name: str, tool_name: str) -> None:
    """Parent PreToolUse: status tracking with tool-specific detail

    Called only for enabled instances with validated existence.
    File collision detection handled via event subscriptions (hcom events collision).
    """
    detail = _extract_tool_detail(tool_name, hook_data.get('tool_input', {}))

    # Skip status update for Claude's internal memory operations
    # These Edit calls on session-memory/ files happen while Claude appears idle
    if tool_name in ('Edit', 'Write') and 'session-memory/' in detail:
        return

    set_status(instance_name, 'active', f'tool:{tool_name}', detail=detail)


def update_status(instance_name: str, tool_name: str) -> None:
    """Update parent status (direct call, no checks)"""
    set_status(instance_name, 'active', f'tool:{tool_name}')


def stop(instance_name: str, instance_data: dict[str, Any]) -> None:
    """Parent Stop: TCP polling loop using shared helper"""
    from .family import poll_messages

    # Use shared polling helper (instance_data guaranteed by dispatcher)
    wait_timeout = instance_data.get('wait_timeout')
    timeout = wait_timeout or get_config().timeout

    # Persist effective timeout for observability (hcom list --json, TUI)
    update_instance_position(instance_name, {'wait_timeout': timeout})

    exit_code, output, timed_out = poll_messages(
        instance_name,
        timeout,
        disable_on_timeout=False  # Parents don't auto-disable on timeout
    )

    if output:
        print(json.dumps(output, ensure_ascii=False))

    if timed_out:
        set_status(instance_name, 'inactive', 'exit:timeout')

    sys.exit(exit_code)


def posttooluse(hook_data: dict[str, Any], instance_name: str, instance_data: dict[str, Any], updates: dict[str, Any] | None = None) -> None:
    """Parent PostToolUse: launch context, bootstrap, messages"""
    from ..shared import HCOM_COMMAND_PATTERN
    import re

    tool_name = hook_data.get('tool_name', '')
    tool_input = hook_data.get('tool_input', {})
    outputs_to_combine: list[dict[str, Any]] = []

    # Clear blocked status - tool completed means approval was granted
    if instance_data.get('status') == 'blocked':
        set_status(instance_name, 'active', f'approved:{tool_name}')

    # Pull remote events (rate-limited) - receive messages during operation
    try:
        from ..relay import pull
        pull()  # relay.py logs errors internally
    except Exception as e:
        log_hook_error('posttooluse:relay_pull', e)

    # External stop notification (for ALL tools)
    if output := check_external_stop_notification(instance_name, instance_data):
        outputs_to_combine.append(output)

    # Bash-specific flows
    if tool_name == 'Bash':
        command = tool_input.get('command', '')

        # Check hcom command pattern - bootstrap and updates on hcom commands
        matches = list(re.finditer(HCOM_COMMAND_PATTERN, command))
        if matches:
            # Persist updates (transcript_path, directory, etc.) for vanilla instances
            # Vanilla instances opt-in via hcom start - this is their first chance to store metadata
            if updates:
                update_instance_position(instance_name, updates)

            # Bootstrap
            if output := _inject_bootstrap_if_needed(instance_name, instance_data):
                outputs_to_combine.append(output)

    # Message delivery for ALL tools (parent only)
    if output := _get_posttooluse_messages(instance_name, instance_data):
        outputs_to_combine.append(output)

    # Combine and deliver if any outputs
    if outputs_to_combine:
        combined = _combine_posttooluse_outputs(outputs_to_combine)
        print(json.dumps(combined, ensure_ascii=False))

    sys.exit(0)


def _inject_bootstrap_if_needed(instance_name: str, instance_data: dict[str, Any]) -> dict[str, Any] | None:
    """Parent context: inject bootstrap text if not announced

    Returns hook output dict or None.
    """
    if instance_data.get('name_announced', False):
        return None

    msg = build_hcom_bootstrap_text(instance_name)
    update_instance_position(instance_name, {'name_announced': True})

    # Track bootstrap count for first-time user hints
    from ..core.paths import increment_flag_counter
    increment_flag_counter('instance_count')

    return {
        "systemMessage": "[HCOM info shown to instance]",
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg
        }
    }


def _get_posttooluse_messages(instance_name: str, _instance_data: dict[str, Any]) -> dict[str, Any] | None:
    """Parent context: check for unread messages
    Returns hook output dict or None.
    """
    from ..core.messages import get_unread_messages, format_hook_messages

    # Instance guaranteed enabled by dispatcher
    messages, _ = get_unread_messages(instance_name, update_position=True)
    if not messages:
        return None

    formatted = format_hook_messages(messages, instance_name)
    set_status(instance_name, 'active', f"deliver:{messages[0]['from']}", msg_ts=messages[-1]['timestamp'])

    return {
        "systemMessage": formatted,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": formatted
        }
    }


def _combine_posttooluse_outputs(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine multiple PostToolUse outputs
    Returns combined hook output dict.
    """
    if len(outputs) == 1:
        return outputs[0]

    # Combine systemMessages
    system_msgs = [o.get('systemMessage') for o in outputs if o.get('systemMessage')]
    combined_system = ' + '.join(system_msgs) if system_msgs else None

    # Combine additionalContext with separator
    contexts = [
        o['hookSpecificOutput']['additionalContext']
        for o in outputs
        if 'hookSpecificOutput' in o
    ]
    combined_context = '\n\n---\n\n'.join(contexts)

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": combined_context
        }
    }
    if combined_system:
        result["systemMessage"] = combined_system

    return result


def userpromptsubmit(_hook_data: dict[str, Any], instance_name: str, updates: dict[str, Any], is_matched_resume: bool, instance_data: dict[str, Any]) -> None:
    """Parent UserPromptSubmit: timestamp, bootstrap"""

    # Instance guaranteed to exist by dispatcher
    name_announced = instance_data.get('name_announced', False)

    # Session_ended prevents user receiving messages(?) so reset it.
    if is_matched_resume and instance_data.get('session_ended'):
        update_instance_position(instance_name, {'session_ended': False})
        instance_data['session_ended'] = False  # Resume path reactivates Stop hook polling

    # Show bootstrap if not already announced (HCOM-launched instances only)
    # Vanilla instances get bootstrap in PostToolUse after cmd_start creates instance
    show_bootstrap = False
    msg = None

    if not name_announced:
        # Only HCOM-launched instances show bootstrap in UserPromptSubmit
        if os.environ.get('HCOM_LAUNCHED') == '1':
            msg = build_hcom_bootstrap_text(instance_name)
            show_bootstrap = True

    # Show message if needed
    if msg:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": msg
            }
        }
        print(json.dumps(output), file=sys.stdout)

    # Mark bootstrap as shown
    if show_bootstrap:
        update_instance_position(instance_name, {'name_announced': True})
        # Track bootstrap count for first-time user hints
        from ..core.paths import increment_flag_counter
        increment_flag_counter('instance_count')

    # Persist updates (transcript_path, directory, tag, etc.)
    if updates:
        update_instance_position(instance_name, updates)

    # Set status to active (user submitted prompt)
    set_status(instance_name, 'active', 'prompt')


def notify(hook_data: dict[str, Any], instance_name: str, updates: dict[str, Any], instance_data: dict[str, Any]) -> None:
    """Parent Notification: update status to blocked (parent only, handler filters subagent context)"""
    message = hook_data.get('message', '')

    # Filter out generic "waiting for input" - not a meaningful status change
    if message == "Claude is waiting for your input":
        return

    if updates:
        update_instance_position(instance_name, updates)
    set_status(instance_name, 'blocked', message)


def sessionend(hook_data: dict[str, Any], instance_name: str, updates: dict[str, Any]) -> None:
    """Parent SessionEnd: mark ended, set final status"""
    reason = hook_data.get('reason', 'unknown')

    # Set session_ended flag to tell Stop hook to exit
    updates['session_ended'] = True

    # Set status to inactive with reason as context (reason: clear, logout, prompt_input_exit, other)
    set_status(instance_name, 'inactive', f'exit:{reason}')

    try:
        update_instance_position(instance_name, updates)
    except Exception as e:
        log_hook_error(f'sessionend:update_instance_position({instance_name})', e)

    # Notify instance to wake and exit cleanly
    notify_instance(instance_name)
