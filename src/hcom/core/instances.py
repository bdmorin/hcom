"""Instance state management - tracking, status, and group membership"""
from __future__ import annotations
from pathlib import Path
from typing import Any
import time
import os

from ..shared import format_age

# Configuration
SKIP_HISTORY = True  # New instances start at current log position (skip old messages)

def parse_running_tasks(json_str: str) -> dict[str, Any]:
    """Parse running_tasks JSON with safe defaults

    Returns dict with structure: {'active': bool, 'subagents': list}
    """
    import json

    if not json_str:
        return {'active': False, 'subagents': []}

    try:
        rt = json.loads(json_str)
        if not isinstance(rt, dict):
            return {'active': False, 'subagents': []}
        rt.setdefault('active', False)
        rt.setdefault('subagents', [])
        return rt
    except json.JSONDecodeError:
        return {'active': False, 'subagents': []}

def is_remote_instance(instance_data: dict[str, Any]) -> bool:
    """Check if instance is synced from another device (has origin_device_id)."""
    return bool(instance_data.get('origin_device_id'))


def is_external_sender(instance_data: dict[str, Any]) -> bool:
    """Check if instance is an external sender (created via --from --wait).
    External senders have empty/null session_id and mapid (no Claude hooks).
    Remote instances (synced from other devices) are NOT external.
    Subagents have parent_session_id, so are not external even without session_id/mapid."""
    if is_remote_instance(instance_data):
        return False
    if instance_data.get('parent_session_id'):
        return False
    return not instance_data.get('session_id') and not instance_data.get('mapid')

# ==================== Core Instance I/O ====================

def load_instance_position(instance_name: str) -> dict[str, Any]:
    """Load position data for a single instance (DB wrapper)"""
    from .db import get_instance
    data = get_instance(instance_name)
    return data if data else {}

def update_instance_position(instance_name: str, update_fields: dict[str, Any]) -> None:
    """Update instance position atomically (DB wrapper)

    Creates instance with defaults if doesn't exist (auto-vivification).
    """
    from .db import update_instance, get_instance
    from ..hooks.utils import log_hook_error

    try:
        # Auto-vivify if needed - capture session_id/MAPID for Windows compatibility TODO: probably should remove this defensive bollocks but not sure if its needed in some cases
        if not get_instance(instance_name):
            from ..shared import MAPID
            import os
            session_id = os.environ.get('HCOM_SESSION_ID')
            initialize_instance_in_position_file(instance_name, session_id, mapid=MAPID)

        # Convert booleans to integers for SQLite
        update_copy = update_fields.copy()
        for bool_field in ['enabled', 'tcp_mode', 'background',
                           'name_announced', 'launch_context_announced',
                           'stop_pending', 'stop_notified', 'session_ended']:
            if bool_field in update_copy and isinstance(update_copy[bool_field], bool):
                update_copy[bool_field] = int(update_copy[bool_field])

        update_instance(instance_name, update_copy)
    except Exception as e:
        log_hook_error(f'update_instance_position:{instance_name}', e)
        pass  # Silent to user, logged for debugging

# ==================== Instance Helper Functions ====================

def is_parent_instance(instance_data: dict[str, Any] | None) -> bool:
    """Check if instance is a parent (has session_id, no parent_session_id)"""
    if not instance_data:
        return False
    has_session = bool(instance_data.get('session_id'))
    has_parent = bool(instance_data.get('parent_session_id'))
    return has_session and not has_parent

def is_subagent_instance(instance_data: dict[str, Any] | None) -> bool:
    """Check if instance is a subagent (has parent_session_id)"""
    if not instance_data:
        return False
    return bool(instance_data.get('parent_session_id'))

# ==================== Status Functions ====================

def get_instance_status(pos_data: dict[str, Any]) -> tuple[bool, str, str, str, int]:
    """Get current status of instance. Returns (enabled, status, age_string, description, age_seconds).

    age_string format: "16m" (clean format, no parens/suffix - consumers handle display)
    age_seconds: raw integer seconds for programmatic filtering

    Status is activity state (what instance is doing).
    Enabled is participation flag (whether instance can send/receive HCOM).
    These are orthogonal - can be disabled but still active.
    """
    enabled = pos_data.get('enabled', False)
    status = pos_data.get('status', 'inactive')
    status_time = pos_data.get('status_time', 0)
    status_context = pos_data.get('status_context', '')

    # Handle string status_time (can happen with remote instances from sync)
    if isinstance(status_time, str):
        try:
            status_time = int(float(status_time))
        except (ValueError, TypeError):
            status_time = 0

    now = int(time.time())
    age = now - status_time if status_time else 0
    # Fallback to created_at for never-started instances (status_time=0)
    if not age:
        created_at = pos_data.get('created_at', 0)
        if created_at:
            age = now - int(created_at)

    # Heartbeat timeout check: instance was idle but heartbeat died
    # This detects terminated instances (closed window/crashed) that were idle
    if status == 'idle':
        last_stop = pos_data.get('last_stop', 0)
        if last_stop:  # Only check heartbeat if last_stop is set
            heartbeat_age = now - last_stop
            tcp_mode = pos_data.get('tcp_mode', False)
            is_remote = bool(pos_data.get('origin_device_id'))
            # Remote instances use 40s threshold (sync interval), local depends on tcp_mode
            threshold = 40 if (tcp_mode or is_remote) else 2
            if heartbeat_age > threshold:
                status = 'inactive'
                status_context = 'stale:idle'
                age = heartbeat_age
    # Activity timeout check: no status updates for extended period
    # This detects terminated instances that were active/blocked/etc when closed
    elif status not in ['inactive']:
        timeout = pos_data.get('wait_timeout', 1800)
        min_threshold = max(timeout + 60, 600)  # Timeout + 1min buffer, minimum 10min
        status_age = now - status_time if status_time else 0
        if status_age > min_threshold:
            prev_status = status  # Capture before changing
            status = 'inactive'
            status_context = f'stale:{prev_status}'
            age = status_age

    # Auto-disable instances inactive for >24hr (lazy cleanup on status read)
    # Moves them to collapsed "stopped" section in TUI, reduces clutter
    if status == 'inactive' and enabled and age > 86400:  # 24 hours
        instance_name = pos_data.get('name')
        if instance_name:
            update_instance_position(instance_name, {'enabled': False})
            enabled = False

    # Build description from status and context
    description = get_status_description(status, status_context)

    return (enabled, status, format_age(age), description, age)


def get_status_description(status: str, context: str = '') -> str:
    """Build human-readable status description from status + metadata tokens

    Metadata token format:
    - deliver:{sender} - message delivery
    - tool:{name} - tool execution
    - exit:{reason} - exit states (timeout, orphaned, task_completed, disabled, clear)
    - stale:{prev_status} - stale detection preserving previous state
    - unknown - unknown state
    - Empty string - simple idle (no context needed)
    """
    if status == 'active':
        if context.startswith('deliver:'):
            sender = context[8:]  # "deliver:alice" → "alice"
            return f"active: msg from {sender}"
        elif context.startswith('tool:'):
            tool = context[5:]  # "tool:Bash" → "Bash"
            return f"active: {tool}"
        return context if context else "active"
    elif status == 'idle':
        return "idle"
    elif status == 'blocked':
        return context if context else "permission needed"
    elif status == 'inactive':
        if context.startswith('stale:'):
            return "inactive: stale"
        elif context.startswith('exit:'):
            reason = context[5:]  # "exit:timeout" → "timeout"
            return f"inactive: {reason}"
        elif context == 'unknown':
            return "inactive: unknown"
        return f"inactive: {context}" if context else "inactive"
    return "unknown"

def set_status(instance_name: str, status: str, context: str = '', detail: str = '', msg_ts: str = ''):
    """Set instance status with timestamp and log status change event.

    Args:
        context: Type token (tool:Bash, deliver:alice, exit:timeout)
        detail: Value for the context (command string, file path, task prompt)
        msg_ts: Timestamp of last message read (for cross-device read receipts)
    """
    from .db import log_event
    # Check if this is first status update (for ready event / launcher notification)
    current_data = load_instance_position(instance_name)
    is_new = current_data.get('status_context') == 'new' if current_data else True

    # Update instance file
    update_instance_position(instance_name, {
        'status': status,
        'status_time': int(time.time()),
        'status_context': context,
        'status_detail': detail
    })

    if is_new:
        try:
            launcher = os.environ.get('HCOM_LAUNCHED_BY', 'unknown')
            batch_id = os.environ.get('HCOM_LAUNCH_BATCH_ID')

            event_data = {
                'action': 'ready',
                'by': launcher,
                'status': status,
                'context': context
            }
            if batch_id:
                event_data['batch_id'] = batch_id

            log_event(
                event_type='life',
                instance=instance_name,
                data=event_data
            )

            # Check if this is the last instance from a launch batch
            if launcher != 'unknown' and batch_id:
                from .db import get_db
                import json
                db = get_db()

                # Find the launch event for this batch
                launch_event = db.execute("""
                    SELECT data FROM events
                    WHERE type = 'life'
                      AND instance = ?
                      AND json_extract(data, '$.action') = 'launched'
                      AND json_extract(data, '$.batch_id') = ?
                    LIMIT 1
                """, (launcher, batch_id)).fetchone()

                if launch_event:
                    launch_data = json.loads(launch_event['data'])
                    expected_count = launch_data.get('launched', 0)

                    if expected_count > 0:
                        # Count ready events with matching batch_id
                        ready_count = db.execute("""
                            SELECT COUNT(*) as count FROM events
                            WHERE type = 'life'
                              AND json_extract(data, '$.action') = 'ready'
                              AND json_extract(data, '$.batch_id') = ?
                        """, (batch_id,)).fetchone()['count']

                        # If this is the last one, send notification to launcher
                        if ready_count >= expected_count:
                            # Check if notification already sent (idempotency)
                            existing = db.execute("""
                                SELECT 1 FROM events
                                WHERE type = 'message'
                                  AND instance = 'sys_[hcom-launcher]'
                                  AND json_extract(data, '$.text') LIKE ?
                                LIMIT 1
                            """, (f'%batch: {batch_id}%',)).fetchone()

                            if not existing:
                                from .messages import send_system_message

                                # Get instance names from this batch
                                ready_instances = db.execute("""
                                    SELECT DISTINCT instance FROM events
                                    WHERE type = 'life'
                                      AND json_extract(data, '$.action') = 'ready'
                                      AND json_extract(data, '$.batch_id') = ?
                                """, (batch_id,)).fetchall()

                                instances_list = ", ".join(row['instance'] for row in ready_instances)

                                send_system_message(
                                    '[hcom-launcher]',
                                    f"@{launcher} All {expected_count} instances ready: {instances_list} (batch: {batch_id})"
                                )
        except Exception as e:
            from ..hooks.utils import log_hook_error
            log_hook_error('set_status:batch_notification', e)

    # Log status change event (best-effort, non-blocking)
    # Include position + msg_ts for cross-device read receipt sync
    try:
        position = current_data.get('last_event_id', 0) if current_data else 0
        data = {'status': status, 'context': context, 'position': position}
        if detail:
            data['detail'] = detail
        if msg_ts:
            data['msg_ts'] = msg_ts
        log_event(event_type='status', instance=instance_name, data=data)
        # Push immediately on exit so remote devices see final state
        if status == 'inactive':
            from ..relay import push
            push(force=True)
    except Exception:
        pass  # Don't break hooks if event logging fails

# ==================== Identity Management ====================

# Shared wordlist for deterministic name generation
NAME_WORDS = [
    'ace', 'air', 'ant', 'arm', 'art', 'axe', 'bad', 'bag', 'bar', 'bat',
    'bed', 'bee', 'big', 'box', 'boy', 'bug', 'bus', 'cab', 'can', 'cap',
    'car', 'cat', 'cop', 'cow', 'cry', 'cup', 'cut', 'day', 'dog', 'dry',
    'ear', 'egg', 'eye', 'fan', 'pig', 'fly', 'fox', 'fun', 'gem', 'gun',
    'hat', 'hit', 'hot', 'ice', 'ink', 'jet', 'key', 'law', 'map', 'mix',
    'man', 'bob', 'noo', 'yes', 'poo', 'sue', 'tom', 'the', 'and', 'but',
    'age', 'aim', 'bro', 'bid', 'shi', 'buy', 'den', 'dig', 'dot', 'dye',
    'end', 'era', 'eve', 'few', 'fix', 'gap', 'gas', 'god', 'gym', 'nob',
    'hip', 'hub', 'hug', 'ivy', 'jab', 'jam', 'jay', 'jog', 'joy', 'lab',
    'lag', 'lap', 'leg', 'lid', 'lie', 'log', 'lot', 'mat', 'mop', 'mud',
    'net', 'new', 'nod', 'now', 'oak', 'odd', 'off', 'oil', 'old', 'one',
    'lol', 'owe', 'own', 'pad', 'pan', 'pat', 'pay', 'pea', 'pen', 'pet',
    'pie', 'pig', 'pin', 'pit', 'pot', 'pub', 'nah', 'rag', 'ran', 'rap',
    'rat', 'raw', 'red', 'rib', 'rid', 'rip', 'rod', 'row', 'rub', 'rug',
    'run', 'sad', 'sap', 'sat', 'saw', 'say', 'sea', 'set', 'wii', 'she',
    'shy', 'sin', 'sip', 'sir', 'sit', 'six', 'ski', 'sky', 'sly', 'son',
    'boo', 'soy', 'spa', 'spy', 'rat', 'sun', 'tab', 'tag', 'tan', 'tap',
    'pls', 'tax', 'tea', 'ten', 'tie', 'tip', 'toe', 'ton', 'top', 'toy',
    'try', 'tub', 'two', 'use', 'van', 'bum', 'war', 'wax', 'way', 'web',
    'wed', 'wet', 'who', 'why', 'wig', 'win', 'moo', 'won', 'wow', 'yak',
    'too', 'gay', 'yet', 'you', 'zip', 'zoo', 'ann', 'brb', 'wtf', 'hey',
    'bro', 'sus', 'meh', 'ass', 'pee', 'omg', 'nob', 'noo', 'yes', 'hmm'
]


def hash_to_name(input_str: str, collision_attempt: int = 0) -> str:
    """Hash any string to a pronounceable name (word + suffix).

    Args:
        input_str: String to hash
        collision_attempt: Increment to generate different name variant

    Returns:
        4-char name like 'boxe', 'cata', 'dogi'
    """
    hash_val = sum(ord(c) * (i + collision_attempt) for i, c in enumerate(input_str))
    word = NAME_WORDS[hash_val % len(NAME_WORDS)]

    # Add letter suffix for pronounceability
    last_char = word[-1]
    suffix_options = 'snrl' if last_char in 'aeiou' else 'aeiouy'
    letter_hash = sum(ord(c) for c in input_str[1:]) if len(input_str) > 1 else hash_val
    suffix = suffix_options[letter_hash % len(suffix_options)]

    return f"{word}{suffix}"


def get_base_name(session_id: str | None, collision_attempt: int = 0) -> str:
    """Get base name for instance using session_id deterministically.

    Args:
        session_id: Session ID to hash (required)
        collision_attempt: Collision counter for race resolution (default 0)

    Returns:
        Generated base name (may already exist in DB - caller must check)
    """
    if not session_id:
        raise ValueError("session_id required for instance naming")

    base_name = hash_to_name(session_id, collision_attempt)

    # Add single collision word if attempt > 0 (deterministic per attempt number)
    if collision_attempt > 0:
        collision_hash = sum(ord(c) * (collision_attempt + 1) for c in session_id)
        collision_word = NAME_WORDS[collision_hash % len(NAME_WORDS)]
        base_name = f"{base_name}{collision_word}"

    return base_name


def get_full_name(instance_data: dict[str, Any] | None) -> str:
    """Get full display name from instance data.

    Architecture: DB stores base name ('alice') + optional tag ('team').
    Full name ('team-alice') is computed at display time, not stored.
    Use this in display/output code. Use base name for DB lookups and routing.

    Returns:
        '{tag}-{name}' if tag exists, else just '{name}'

    Caches result on dict as '_full_name' for subsequent calls.
    """
    if not instance_data:
        return ''

    # Return cached value if available
    if '_full_name' in instance_data:
        return instance_data['_full_name']

    name = instance_data.get('name', '')
    tag = instance_data.get('tag')
    if tag:
        # Legacy check: if name already has tag prefix, return as-is
        prefix = f"{tag}-"
        if name.startswith(prefix):
            full_name = name  # Already full name (legacy format)
        else:
            full_name = f"{tag}-{name}"
    else:
        full_name = name

    # Cache on dict (safe - update functions use explicit field dicts)
    instance_data['_full_name'] = full_name
    return full_name


def get_display_name(session_id: str | None, tag: str | None = None, collision_attempt: int = 0) -> str:
    """DEPRECATED: Use get_base_name() + get_full_name() instead.

    For backwards compatibility, still returns {tag}-{base} format.
    New code should use get_base_name() for the PK and get_full_name() for display.
    """
    base = get_base_name(session_id, collision_attempt)
    if tag:
        return f"{tag}-{base}"
    return base

def resolve_instance_name(session_id: str, tag: str | None = None) -> tuple[str, dict | None]:
    """
    Resolve instance name (base name) for a session_id with hash collision handling.
    Searches existing instances first (reuses if found), generates new base name if not found.

    The returned name is the BASE name (e.g., 'alice'), not the full display name.
    Tag is stored separately in the instance record and can be changed at runtime.
    Use get_full_name(instance_data) to get '{tag}-{name}' for display.

    Hash collision handling:
    - Deterministic hash may generate same name for different session_ids (~1/1000 probability)
    - Retry with collision_attempt counter (preserves session_id → name mapping)
    - DB UNIQUE constraint provides paranoid safety net for TOCTOU (astronomically rare)

    Returns: (base_name, existing_data_or_none)
    """
    from .db import find_instance_by_session, get_instance

    # Search for existing instance with this session_id (DB query, not glob)
    if session_id:
        existing_name = find_instance_by_session(session_id)
        if existing_name:
            data = get_instance(existing_name)
            return existing_name, data

    # Not found - generate new BASE name (no tag prefix) with hash collision retry
    max_retries = 100
    for attempt in range(max_retries):
        # Generate base name only - tag stored separately in DB
        instance_name = get_base_name(session_id, collision_attempt=attempt)

        # Check if name already exists in DB
        existing = get_instance(instance_name)
        if existing:
            # Name exists - check if it's ours (idempotent) or hash collision
            if existing.get('session_id') == session_id:
                return instance_name, existing  # Our instance
            # Hash collision - different session_id generated same name, try next variant
            continue

        # Name appears free
        return instance_name, None

    raise RuntimeError(f"Cannot generate unique name for session after {max_retries} attempts")

def initialize_instance_in_position_file(instance_name: str, session_id: str | None = None, parent_session_id: str | None = None, enabled: bool | None = None, parent_name: str | None = None, mapid: str | None = None, agent_id: str | None = None, transcript_path: str | None = None) -> bool:
    """Initialize instance in DB with required fields (idempotent). Returns True on success, False on failure."""
    from .db import get_instance, save_instance, get_last_event_id
    import sqlite3

    try:
        # Check if already exists - if so, update it with provided params (don't skip)
        existing = get_instance(instance_name)
        if existing:
            # Instance exists (possibly placeholder) - update with provided metadata
            updates = {}
            if parent_session_id is not None:
                updates['parent_session_id'] = parent_session_id
            if parent_name is not None:
                updates['parent_name'] = parent_name
            if enabled is not None:
                updates['enabled'] = int(enabled)
            if mapid is not None:
                updates['mapid'] = mapid
            if agent_id is not None:
                updates['agent_id'] = agent_id
            if transcript_path is not None:
                updates['transcript_path'] = transcript_path

            # Fix last_event_id for new instances (SKIP_HISTORY fix)
            # If last_event_id is 0, this is a new instance being created
            if SKIP_HISTORY and existing.get('last_event_id', 0) == 0:
                launch_event_id_str = os.environ.get('HCOM_LAUNCH_EVENT_ID')
                if launch_event_id_str:
                    updates['last_event_id'] = int(launch_event_id_str)
                else:
                    updates['last_event_id'] = get_last_event_id()

            # Reset status_context for HCOM-launched resumed sessions (triggers ready event)
            if os.environ.get('HCOM_LAUNCHED') == '1':
                updates['status_context'] = 'new'

            if updates:
                from .db import update_instance
                update_instance(instance_name, updates)
            return True

        # Determine default enabled state: True for hcom-launched, False for vanilla
        is_hcom_launched = os.environ.get('HCOM_LAUNCHED') == '1'

        # Determine starting event ID: skip history or read from beginning
        initial_event_id = 0
        if SKIP_HISTORY:
            # Use launch event ID if available (for hcom-launched instances)
            # Otherwise use current max event ID (for vanilla instances)
            launch_event_id_str = os.environ.get('HCOM_LAUNCH_EVENT_ID')
            if launch_event_id_str:
                initial_event_id = int(launch_event_id_str)
            else:
                initial_event_id = get_last_event_id()

        # Determine enabled state: explicit param > hcom-launched > False
        if enabled is not None:
            default_enabled = enabled
        else:
            default_enabled = is_hcom_launched

        data = {
            "name": instance_name,
            "last_event_id": initial_event_id,
            "enabled": int(default_enabled),
            "directory": str(Path.cwd()),
            "last_stop": 0,
            "created_at": time.time(),
            "session_id": session_id if session_id else None,  # NULL not empty string
            "mapid": mapid or "",
            "transcript_path": "",
            "name_announced": 0,
            "tag": None,
            "status": "inactive",
            # status_context="new" triggers ready event on first status update (see set_status)
            "status_context": "new"
        }

        # Initialize tag for Claude instances/subagents (external senders should remain untagged)
        # Tag can be changed later via `hcom config -i self tag`.
        if session_id or mapid or parent_session_id:
            try:
                from .config import get_config
                tag = get_config().tag
                if tag:
                    data["tag"] = tag
            except Exception:
                pass

        # Add parent_session_id and parent_name for subagents
        if parent_session_id:
            data["parent_session_id"] = parent_session_id
        if parent_name:
            data["parent_name"] = parent_name
        if agent_id:
            data["agent_id"] = agent_id
        if transcript_path:
            data["transcript_path"] = transcript_path

        try:
            success = save_instance(instance_name, data)

            # Log creation event (only for HCOM participants)
            if success and default_enabled:
                try:
                    from .db import log_event

                    # Determine who launched this instance
                    launcher = os.environ.get('HCOM_LAUNCHED_BY', 'unknown')

                    log_event('life', instance_name, {
                        'action': 'created',
                        'by': launcher,
                        'enabled': default_enabled,
                        'is_hcom_launched': is_hcom_launched,
                        'is_subagent': bool(parent_session_id),
                        'parent_name': parent_name or ''
                    })
                except Exception as e:
                    from ..hooks.utils import log_hook_error
                    log_hook_error('initialize_instance:log_event', e)

            return success
        except sqlite3.IntegrityError:
            # UNIQUE constraint violation - paranoid safety net for hash collision TOCTOU
            # (Another process won the INSERT race after both checked DB. Astronomically rare.)
            # Safe to treat as success since instance exists with our intended name
            return True
    except Exception:
        return False

def enable_instance(instance_name: str, initiated_by: str = 'unknown', reason: str = '') -> None:
    """Enable instance
    Args:
        instance_name: Instance to enable
        initiated_by: Who initiated (from resolve_identity())
        reason: Context (e.g., 'manual', 'resume', 'launch')
    """
    update_instance_position(instance_name, {
        'enabled': True,
        'stop_pending': False,
        'stop_notified': False,
    })
    # Log all enable operations
    try:
        from .db import log_event
        log_event('life', instance_name, {
            'action': 'started',
            'by': initiated_by,
            'reason': reason
        })
        # Push lifecycle event (rate-limited)
        from ..relay import push
        push()
    except Exception:
        pass  # Don't break enable if logging/sync fails

__all__ = [
    'load_instance_position',
    'update_instance_position',
    'is_parent_instance',
    'is_subagent_instance',
    'is_remote_instance',
    'is_external_sender',
    'get_instance_status',
    'get_status_description',
    'set_status',
    # Identity management
    'get_base_name',
    'get_full_name',
    'get_display_name',  # Deprecated - use get_base_name + get_full_name
    'resolve_instance_name',
    'initialize_instance_in_position_file',
    'enable_instance',
]
