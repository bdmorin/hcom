"""Subagent context hook implementations"""
from __future__ import annotations
from typing import Any
import sys
import json

from ..core.config import get_config
from ..core.instances import load_instance_position, update_instance_position, set_status, parse_running_tasks
from ..core.db import get_db, find_instance_by_session
from .family import check_external_stop_notification

# ============ TASK CONTEXT TRACKING ============

def in_subagent_context(session_id_or_name: str) -> bool:
    """Check if session/instance is in subagent context (Task active).

    Uses database running_tasks.active field for cross-process detection.
    Task is active if running_tasks JSON has active=true.
    Note: Parent frozen only for foreground Tasks; background Tasks allow live bidirectional comms.

    Args:
        session_id_or_name: Either session_id (from hooks) or instance_name (from commands)
    """
    conn = get_db()

    # Try as session_id first (fast path for hooks)
    instance_name = find_instance_by_session(session_id_or_name)
    if not instance_name:
        # Try as instance_name (for commands)
        instance_name = session_id_or_name

    row = conn.execute(
        "SELECT running_tasks FROM instances WHERE name = ? LIMIT 1",
        (instance_name,)
    ).fetchone()

    if not row or not row['running_tasks']:
        return False

    try:
        running_tasks = json.loads(row['running_tasks'])
        return running_tasks.get('active', False)
    except (json.JSONDecodeError, AttributeError):
        return False


def check_dead_subagents(transcript_path: str, running_tasks: dict, subagent_timeout: int | None = None) -> list[str]:
    """Return list of dead subagent agent_ids by checking multiple signals

    Called by UserPromptSubmit to clean up stale subagents.

    Args:
        subagent_timeout: Parent's override timeout, or None to use global config
    """
    from pathlib import Path
    import time

    dead = []
    transcript_dir = Path(transcript_path).parent if transcript_path else None
    conn = get_db()
    # Subagent dead if transcript unchanged for 2x timeout (session ended before SubagentStop cleanup)
    timeout = subagent_timeout if subagent_timeout is not None else get_config().subagent_timeout
    stale_threshold = timeout * 2

    for subagent in running_tasks.get('subagents', []):
        agent_id = subagent.get('agent_id')
        if not agent_id:
            continue

        # Rare: instance disabled but still in running_tasks (SubagentStop cleanup failed)
        row = conn.execute(
            "SELECT enabled FROM instances WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()
        if row and not row['enabled']:
            dead.append(agent_id)
            continue

        if not transcript_dir:
            dead.append(agent_id)
            continue

        agent_transcript = transcript_dir / f'agent-{agent_id}.jsonl'
        try:
            if not agent_transcript.exists():
                dead.append(agent_id)
                continue

            # Stale: transcript not modified in 2x timeout = session ended without cleanup
            mtime = agent_transcript.stat().st_mtime
            if time.time() - mtime > stale_threshold:
                dead.append(agent_id)
                continue

            # Check last 4KB for interrupt marker
            with open(agent_transcript, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode('utf-8', errors='ignore')
                if '[Request interrupted by user]' in tail:
                    dead.append(agent_id)
        except Exception:
            dead.append(agent_id)  # Can't read = assume dead

    return dead


def cleanup_dead_subagents(session_id: str, transcript_path: str) -> None:
    """Check and remove dead subagents from running_tasks

    Called by UserPromptSubmit when in subagent context.
    """
    from .utils import log_hook_error

    instance_name = find_instance_by_session(session_id)
    if not instance_name:
        log_hook_error('cleanup_dead:no_instance', session_id)
        return

    instance_data = load_instance_position(instance_name)
    if not instance_data:
        log_hook_error('cleanup_dead:no_data', instance_name)
        return

    running_tasks = parse_running_tasks(instance_data.get('running_tasks', ''))
    if not running_tasks.get('subagents'):
        log_hook_error('cleanup_dead:no_subagents', instance_name)
        return

    log_hook_error('cleanup_dead:checking', f'{instance_name} subagents={running_tasks.get("subagents")} transcript={transcript_path}')
    # Pass parent's subagent_timeout override to check_dead_subagents
    dead_ids = check_dead_subagents(transcript_path, running_tasks, instance_data.get('subagent_timeout'))
    log_hook_error('cleanup_dead:result', f'dead_ids={dead_ids}')
    if not dead_ids:
        return

    # Remove dead subagents
    for agent_id in dead_ids:
        _remove_subagent_from_parent(instance_name, agent_id)
        # Also disable the subagent instance if it exists
        conn = get_db()
        row = conn.execute("SELECT name FROM instances WHERE agent_id = ?", (agent_id,)).fetchone()
        if row:
            from .utils import disable_instance
            disable_instance(row['name'], initiated_by='system', reason='exit:interrupted')
            set_status(row['name'], 'inactive', 'exit:interrupted')


def track_subagent(parent_session_id: str, agent_id: str, agent_type: str) -> None:
    """Track subagent in parent's running_tasks.subagents array

    Appends {agent_id, type} to parent's running_tasks.subagents array.
    """
    instance_name = find_instance_by_session(parent_session_id)
    if not instance_name:
        return

    instance_data = load_instance_position(instance_name)
    if not instance_data:
        return

    # Load existing running_tasks structure
    running_tasks = parse_running_tasks(instance_data.get('running_tasks', ''))
    running_tasks['active'] = True  # Ensure active flag is set

    # Add subagent if not already tracked
    subagents = running_tasks['subagents']
    if not any(s['agent_id'] == agent_id for s in subagents):
        subagents.append({'agent_id': agent_id, 'type': agent_type})
        update_instance_position(instance_name, {'running_tasks': json.dumps(running_tasks)})


def _remove_subagent_from_parent(parent_name: str, agent_id: str) -> None:
    """Remove subagent from parent's running_tasks.subagents array

    Called when subagent exits (SubagentStop).
    Sets active=False when last subagent removed (enables parallel Task support).
    """
    parent_data = load_instance_position(parent_name)
    if not parent_data:
        return

    # Load existing running_tasks structure
    running_tasks = parse_running_tasks(parent_data.get('running_tasks', ''))
    if not running_tasks.get('subagents'):
        return

    # Remove subagent with matching agent_id
    running_tasks['subagents'] = [s for s in running_tasks['subagents'] if s.get('agent_id') != agent_id]

    # If no more subagents, clear active flag
    if not running_tasks['subagents']:
        running_tasks['active'] = False

    # Update parent
    update_instance_position(parent_name, {'running_tasks': json.dumps(running_tasks)})


# ============ HOOK HANDLERS ============


def posttooluse(hook_data: dict[str, Any], _instance_name: str, _instance_data: dict[str, Any] | None) -> None:
    """Subagent PostToolUse: pull remote events, external stop notification, message delivery

    Handles subagents running hcom commands (identified by --agentid in Bash command).
    """
    import re
    from ..core.db import get_db
    from ..core.messages import get_unread_messages, format_hook_messages

    tool_name = hook_data.get('tool_name', '')
    tool_input = hook_data.get('tool_input', {})

    # Only handle Bash commands with --agentid flag
    if tool_name != 'Bash':
        sys.exit(0)

    command = tool_input.get('command', '')
    if '--agentid' not in command:
        sys.exit(0)

    # Extract agent_id and resolve subagent
    match = re.search(r'--agentid\s+(\S+)', command)
    if not match:
        sys.exit(0)

    agent_id = match.group(1)
    conn = get_db()
    row = conn.execute("SELECT name FROM instances WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        sys.exit(0)

    subagent_name = row['name']
    subagent_data = load_instance_position(subagent_name)
    if not subagent_data:
        sys.exit(0)

    if not subagent_data.get('enabled', False):
        # Disabled instances don't deliver messages, but may show a one-shot external stop notification.
        if output := check_external_stop_notification(subagent_name, subagent_data):
            print(json.dumps(output, ensure_ascii=False))
        sys.exit(0)

    # Pull remote events (rate-limited)
    try:
        from ..relay import pull
        pull()
    except Exception as e:
        # Best-effort sync - log for debugging
        from .utils import log_hook_error
        log_hook_error('subagent_stop:relay_pull', e)

    outputs = []

    # External stop notification
    if output := check_external_stop_notification(subagent_name, subagent_data):
        outputs.append(output)

    # Message delivery (like parent PostToolUse)
    messages, _ = get_unread_messages(subagent_name, update_position=True)
    if messages:
        formatted = format_hook_messages(messages, subagent_name)
        set_status(subagent_name, 'active', f"deliver:{messages[0]['from']}", msg_ts=messages[-1]['timestamp'])
        outputs.append({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": formatted
            }
        })

    # Combine outputs if multiple
    if outputs:
        if len(outputs) == 1:
            print(json.dumps(outputs[0], ensure_ascii=False))
        else:
            contexts = [o['hookSpecificOutput']['additionalContext'] for o in outputs if 'hookSpecificOutput' in o]
            combined = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": '\n\n---\n\n'.join(contexts)
                }
            }
            print(json.dumps(combined, ensure_ascii=False))

    sys.exit(0)


def subagent_start(hook_data: dict[str, Any]) -> None:
    """SubagentStart: Surface agent_id to subagent"""
    from .utils import build_hcom_command

    agent_id = hook_data.get('agent_id')
    if not agent_id:
        sys.exit(0)

    # Minimal hint - cmd_start resolves parent from identity and looks up type from running_tasks
    hcom_cmd = build_hcom_command()
    hint = "[HCOM INFORMATION] hcom is a communication tool for interacting with other claude code instances.\n"
    hint += f"To use hcom, you MUST run this command first: '{hcom_cmd} start --agentid {agent_id}'\n"
    hint += f"If you have been prompted to run any hcom commands, assume you must add --agentid {agent_id} to every command\n"
    hint += f"Important: hcom will not work until you first register with: '{hcom_cmd} start --agentid {agent_id}'"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": hint
        }
    }

    print(json.dumps(output))
    sys.exit(0)


def subagent_stop(hook_data: dict[str, Any]) -> None:
    """SubagentStop: Message polling using agent_id (lazy creation pattern)"""
    from .utils import log_hook_error

    # Extract agent_id
    agent_id = hook_data.get('agent_id')
    if not agent_id:
        sys.exit(0)

    # Query for subagent by agent_id (stored when subagent ran hcom start)
    conn = get_db()
    row = conn.execute(
        "SELECT name, transcript_path, parent_name, enabled FROM instances WHERE agent_id = ?",
        (agent_id,)
    ).fetchone()

    if not row:
        # No instance = subagent hasn't run hcom start yet (not opted in)
        sys.exit(0)

    subagent_id = row['name']
    parent_name = row['parent_name']
    enabled_at_entry = row['enabled']

    log_hook_error('subagent_stop:enter', f'{subagent_id} agent_id={agent_id} enabled={enabled_at_entry}')

    # Store transcript_path if not already set
    if not row['transcript_path']:
        transcript_path = hook_data.get('agent_transcript_path')
        if transcript_path:
            update_instance_position(subagent_id, {'transcript_path': transcript_path})

    # Poll messages using shared helper
    # Resolve timeout: parent instance override > global config
    timeout = None
    if parent_name:
        parent_data = load_instance_position(parent_name)
        if parent_data:
            timeout = parent_data.get('subagent_timeout')
    if timeout is None:
        timeout = get_config().subagent_timeout
    from .family import poll_messages

    exit_code, output, timed_out = poll_messages(
        subagent_id,
        timeout,
        disable_on_timeout=False
    )

    log_hook_error('subagent_stop:poll_done', f'{subagent_id} exit_code={exit_code} timed_out={timed_out}')

    if output:
        print(json.dumps(output, ensure_ascii=False))

    # exit_code=2: message delivered, subagent continues processing, SubagentStop fires again
    # exit_code=0: no message/timeout, disable and cleanup
    if exit_code == 0:
        update_instance_position(subagent_id, {'enabled': False})
        set_status(subagent_id, 'inactive', 'exit:timeout' if timed_out else 'exit:task_completed')
        if parent_name:
            _remove_subagent_from_parent(parent_name, agent_id)

    sys.exit(exit_code)

