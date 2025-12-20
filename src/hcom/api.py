"""Public API for TUI, external tools, and Python scripts.

Two layers:
1. Re-exports (for TUI/internal): get_config, cmd_send, etc.
2. High-level API (for scripts): whoami(), send(), instances(), etc.

CLI and internal code can import from specific modules.
TUI and plugins should import from api.py for stability.
External scripts should use the high-level functions.
"""
from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone
from typing import Any

# Core utilities (re-exports for TUI)
from .core.config import (
    get_config,
    reload_config,
    HcomConfig,
    HcomConfigError,
    ConfigSnapshot,
    load_config_snapshot,
    save_config_snapshot,
    save_config,
    dict_to_hcom_config,
)
from .core.paths import hcom_path, ensure_hcom_directories
from .core.instances import (
    get_instance_status,
    set_status,
    load_instance_position,
    update_instance_position,
)
from .core.messages import (
    send_message,
    get_unread_messages,
    get_read_receipts,
)

# Commands (for TUI to call directly)
from .commands.admin import (
    cmd_reset,
    cmd_events,
)
from .commands.lifecycle import (
    cmd_launch,
    cmd_start,
    cmd_stop,
)
from .commands.messaging import cmd_send

# Shared utilities and constants
from .shared import (
    ClaudeArgsSpec,
    resolve_claude_args,
    HcomError,
    SenderIdentity,
)


# ==================== High-Level Python API ====================

def _ensure_init():
    """Ensure hcom directories and db exist."""
    if not ensure_hcom_directories():
        raise HcomError("Failed to create hcom directories (check permissions)")
    from .core.db import init_db
    init_db()


def whoami(*, field: str | None = None) -> dict[str, Any] | str | bool | None:
    """Get current instance info.

    Args:
        field: Optional specific field to return

    Returns:
        Dict with name, session_id, connected, directory, status
        Or single field value if field specified

    Raises:
        HcomError: If can't resolve identity

    Example:
        >>> api.whoami()
        {"name": "alice", "session_id": "abc123", "connected": True, ...}
        >>> api.whoami(field="name")
        "alice"
    """
    from .commands.utils import resolve_identity
    from .core.instances import get_full_name

    _ensure_init()

    identity = resolve_identity()  # raises HcomError

    data = identity.instance_data or {}
    result = {
        "name": get_full_name(data) or identity.name,
        "session_id": identity.session_id or "",
        "connected": data.get("enabled", False),
        "directory": data.get("directory", ""),
        "status": data.get("status", "unknown"),
        "transcript_path": data.get("transcript_path", ""),
        "parent_name": data.get("parent_name", ""),
    }

    if field:
        return result.get(field)
    return result


def instances(*, name: str | None = None, all: bool = False) -> list[dict] | dict:
    """List instances.

    Args:
        name: Specific instance to query
        all: Include disabled instances (default: enabled only)

    Returns:
        List of instance dicts, or single dict if name specified

    Raises:
        HcomError: If specific name not found

    Example:
        >>> api.instances()
        [{"name": "debater-bob", "status": "active", "enabled": True, ...}, ...]
        >>> api.instances(name="bob")
        {"name": "bob", "status": "active", ...}
    """
    from .core.db import iter_instances
    from .core.instances import get_full_name

    _ensure_init()

    if name:
        data = load_instance_position(name)
        if not data:
            raise HcomError(f"Instance not found: {name}")
        return {
            "name": get_full_name(data) or name,
            "session_id": data.get("session_id", ""),
            "enabled": data.get("enabled", False),
            "status": data.get("status", "unknown"),
            "directory": data.get("directory", ""),
            "parent_name": data.get("parent_name", ""),
        }

    result = []
    for data in iter_instances(enabled_only=not all):
        result.append({
            "name": get_full_name(data) or data["name"],
            "session_id": data.get("session_id", ""),
            "enabled": data.get("enabled", False),
            "status": data.get("status", "unknown"),
            "directory": data.get("directory", ""),
            "parent_name": data.get("parent_name", ""),
        })
    return result


def send(
    message: str,
    *,
    to: str | None = None,
    sender: str | None = None,
    intent: str | None = None,
    reply_to: str | None = None,
    thread: str | None = None,
) -> list[str]:
    """Send message.

    Args:
        message: Message text (can include @mentions)
        to: Optional target (prepended as @mention if not in message)
        sender: Optional sender name for external tools (--from equivalent)
        intent: Message intent (request, inform, ack, error)
        reply_to: Event ID to reply to (e.g., "42" or "42:BOXE" for cross-device)
        thread: Thread name for grouping related messages

    Returns:
        List of instance names message was delivered to

    Raises:
        HcomError: If validation fails, identity unresolvable, or delivery fails

    Example:
        >>> api.send("@bob hello")
        ["bob"]
        >>> api.send("hello", to="bob")
        ["bob"]
        >>> api.send("build done", sender="ci-bot")  # external tool
        ["alice", "bob"]
        >>> api.send("review this", to="bob", intent="request", thread="pr-123")
        ["bob"]
        >>> api.send("done", to="alice", intent="ack", reply_to="42")
        ["alice"]
    """
    from .core.ops import op_send
    from .core.helpers import validate_intent
    from .core.messages import resolve_reply_to, get_thread_from_event
    from .commands.utils import resolve_identity

    _ensure_init()

    if to and not message.lstrip().startswith(f"@{to}"):
        message = f"@{to} {message}"

    # Build envelope
    envelope = {}
    if intent:
        try:
            validate_intent(intent)
        except ValueError as e:
            raise HcomError(str(e))
        envelope['intent'] = intent

    if reply_to:
        local_id, error = resolve_reply_to(reply_to)
        if error:
            raise HcomError(f"Invalid reply_to: {error}")
        envelope['reply_to'] = reply_to
        # Thread inheritance
        if not thread and local_id:
            parent_thread = get_thread_from_event(local_id)
            if parent_thread:
                thread = parent_thread

    if thread:
        envelope['thread'] = thread

    # Validate: ack requires reply_to
    if intent == 'ack' and 'reply_to' not in envelope:
        raise HcomError("Intent 'ack' requires reply_to")

    # Build identity
    if sender:
        identity = SenderIdentity(kind="external", name=sender, instance_data=None)
    else:
        identity = resolve_identity()  # raises HcomError

    return op_send(identity, message, envelope=envelope if envelope else None)


def events(*, sql: str | None = None, last: int = 20) -> list[dict]:
    """Query events from database.

    Args:
        sql: Optional SQL WHERE clause (injected directly - caller controls)
        last: Max events to return

    Returns:
        List of event dicts with ts, type, instance, data (parsed JSON)

    Schema (events_v view columns):
        Base: id, timestamp, type, instance
        Message: msg_from, msg_text, msg_scope, msg_sender_kind, msg_delivered_to, msg_mentions
        Envelope: msg_intent, msg_thread, msg_reply_to, msg_reply_to_local
        Status: status_val, status_context, status_detail
        Lifecycle: life_action, life_by, life_batch_id, life_reason

    Example:
        >>> api.events(last=5)
        [{"ts": "2024-...", "type": "message", "instance": "alice", "data": {...}}, ...]
        >>> api.events(sql="type='message' AND msg_from='bob'")
        [...]
    """
    from .core.db import get_db

    _ensure_init()
    conn = get_db()

    query = "SELECT * FROM events_v WHERE 1=1"
    if sql:
        query += f" AND ({sql})"
    query += f" ORDER BY id DESC LIMIT {last}"

    try:
        rows = conn.execute(query).fetchall()
    except Exception as e:
        raise HcomError(f"SQL error: {e}")

    result = []
    for row in reversed(rows):
        try:
            result.append({
                "ts": row["timestamp"],
                "type": row["type"],
                "instance": row["instance"],
                "data": json.loads(row["data"]),
            })
        except (json.JSONDecodeError, TypeError):
            pass  # Skip corrupt events
    return result


def wait(sql: str, *, timeout: int = 60) -> dict | None:
    """Wait for event matching SQL.

    Uses 10s lookback to catch recent events (race condition handling).

    Args:
        sql: SQL WHERE clause (see events_v schema in events() docstring)
        timeout: Max seconds to wait

    Returns:
        Matching event dict (ts, type, instance, data) or None if timeout

    Raises:
        HcomError: If SQL is invalid

    Example:
        >>> api.wait("type='message' AND msg_from='bob'", timeout=30)
        {"ts": "...", "type": "message", "instance": "bob", "data": {...}}
        >>> api.wait("type='status' AND status_val='done'")
        None  # timeout
    """
    from .core.db import get_db, get_last_event_id

    _ensure_init()
    conn = get_db()

    # Check for matching events in last 10s (race condition window)
    lookback_ts = datetime.fromtimestamp(time.time() - 10, tz=timezone.utc).isoformat()
    lookback_query = f"SELECT * FROM events_v WHERE timestamp > ? AND ({sql}) ORDER BY id DESC LIMIT 1"

    try:
        row = conn.execute(lookback_query, [lookback_ts]).fetchone()
        if row:
            return {
                "ts": row["timestamp"],
                "type": row["type"],
                "instance": row["instance"],
                "data": json.loads(row["data"]),
            }
    except Exception as e:
        raise HcomError(f"SQL error: {e}")

    # Poll for new events
    start = time.time()
    last_id = get_last_event_id()

    while time.time() - start < timeout:
        query = f"SELECT * FROM events_v WHERE id > ? AND ({sql}) ORDER BY id LIMIT 1"
        try:
            row = conn.execute(query, [last_id]).fetchone()
            if row:
                return {
                    "ts": row["timestamp"],
                    "type": row["type"],
                    "instance": row["instance"],
                    "data": json.loads(row["data"]),
                }
            # Update last_id from any new events (even non-matching)
            any_new = conn.execute("SELECT MAX(id) FROM events WHERE id > ?", [last_id]).fetchone()
            if any_new and any_new[0]:
                last_id = any_new[0]
        except Exception as e:
            raise HcomError(f"SQL error: {e}")
        time.sleep(0.5)

    return None


def transcript(*, name: str | None = None, last: int = 10, full: bool = False, range: str | None = None) -> list[dict]:
    """Get conversation transcript.

    Args:
        name: Instance to get transcript for (default: self)
        last: Number of recent exchanges (ignored if range specified)
        full: Include tool I/O and edit details (detailed mode)
        range: Absolute position range "N-M" (stable for sharing between agents)

    Returns:
        List of exchange dicts from transcript parsing (keys include: position, user, action, files, timestamp;
        detailed mode adds tools/edits/errors).
        Empty list if no transcript available (normal for external senders).

    Raises:
        HcomError: If specific instance name not found or invalid range format

    Example:
        >>> api.transcript()
        [{"position": 1, "user": "hello", "action": "Hi!", "files": [], "timestamp": "..."}]
        >>> api.transcript(name="bob", last=5)
        [...]
        >>> api.transcript(name="bob", range="10-20")  # stable absolute positions
        [...]
    """
    from .core.transcript import get_thread
    from .commands.utils import resolve_identity
    import re

    _ensure_init()

    # Parse range if provided
    range_tuple = None
    if range:
        match = re.match(r'^(\d+)-(\d+)$', range)
        if not match:
            raise HcomError(f"Invalid range format: {range} (expected N-M, e.g. '10-20')")
        start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < 1:
            raise HcomError("Range positions must be >= 1")
        if start > end:
            raise HcomError("Range start must be <= end")
        range_tuple = (start, end)

    # Resolve target
    if name:
        data = load_instance_position(name)
        if not data:
            raise HcomError(f"Instance not found: {name}")
    else:
        identity = resolve_identity()  # raises HcomError
        data = identity.instance_data

    # No instance data or no transcript = empty thread (not an error)
    if not data:
        return []

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return []

    result = get_thread(transcript_path, last=last, detailed=full, range_tuple=range_tuple)
    if result.get("error"):
        return []  # File unreadable, return empty

    return result.get("exchanges", [])


def messages(*, unread: bool = True, last: int = 20) -> list[dict]:
    """Get messages (convenience wrapper for events).

    Args:
        unread: Only unread messages for current instance (default: True)
        last: Max messages to return

    Returns:
        List of message dicts with ts, from, text, mentions, delivered_to,
        and optional envelope fields (intent, thread, reply_to, reply_to_local).
        The 'from' field uses full display names (tag-base or base).

    Example:
        >>> api.messages()  # unread messages for me
        [{"ts": "...", "from": "bob", "text": "hello @alice", "mentions": ["alice"]}]
        >>> api.messages(unread=False, last=50)  # all recent messages
        [...]
    """
    from .commands.utils import resolve_identity
    from .core.instances import get_full_name

    _ensure_init()

    if unread:
        identity = resolve_identity()
        sql = f"type='message' AND msg_delivered_to LIKE '%{identity.name}%'"
    else:
        sql = "type='message'"

    raw = events(sql=sql, last=last)
    result = []
    for e in raw:
        data = e.get("data", {})
        # Convert sender base name to full display name
        sender_base = data.get("from", "")
        sender_data = load_instance_position(sender_base) if sender_base else None
        sender_display = get_full_name(sender_data) or sender_base
        result.append({
            "ts": e["ts"],
            "from": sender_display,
            "text": data.get("text", ""),
            "mentions": data.get("mentions", []),
            "delivered_to": data.get("delivered_to", []),
            "intent": data.get("intent"),
            "thread": data.get("thread"),
            "reply_to": data.get("reply_to"),
            "reply_to_local": data.get("reply_to_local"),
        })
    return result


def subscribe(sql: str, *, once: bool = False, for_instance: str | None = None) -> str:
    """Create event subscription.

    Args:
        sql: SQL WHERE clause to match events (see events_v schema in events() docstring)
        once: Auto-remove after first match
        for_instance: Subscribe on behalf of another instance

    Returns:
        Subscription ID (e.g., "sub-a3f2")

    Raises:
        HcomError: If SQL invalid or identity resolution fails

    Example:
        >>> api.subscribe("type='message' AND msg_mentions LIKE '%@me%'")
        "sub-a3f2"
        >>> api.subscribe("type='status'", once=True)
        "sub-b4c1"
    """
    from .core.db import get_db, get_last_event_id, kv_set
    from .commands.utils import resolve_identity
    from hashlib import sha256

    _ensure_init()

    # Resolve subscriber
    if for_instance:
        subscriber = for_instance
    else:
        identity = resolve_identity()  # raises HcomError
        subscriber = identity.name

    # Validate SQL by attempting a query
    conn = get_db()
    try:
        conn.execute(f"SELECT 1 FROM events_v WHERE ({sql}) LIMIT 0")
    except Exception as e:
        raise HcomError(f"Invalid SQL: {e}")

    # Generate subscription ID (match existing format: 4 char hash)
    now = time.time()
    sub_hash = sha256(f"{subscriber}:{sql}:{now}".encode()).hexdigest()[:4]
    sub_id = f"sub-{sub_hash}"

    # Store subscription (field names must match db.py checker expectations)
    sub_data = {
        "id": sub_id,
        "sql": sql,
        "caller": subscriber,
        "once": once,
        "last_id": get_last_event_id(),
        "created": now,
    }
    kv_set(f"events_sub:{sub_id}", json.dumps(sub_data))

    return sub_id


def subscriptions() -> list[dict]:
    """List active subscriptions.

    Returns:
        List of subscription dicts with id, sql, caller, created, once

    Example:
        >>> api.subscriptions()
        [{"id": "sub-a3f2", "sql": "type='message'", "caller": "alice", ...}]
    """
    from .core.db import get_db

    _ensure_init()
    conn = get_db()

    rows = conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE 'events_sub:%'"
    ).fetchall()

    result = []
    for row in rows:
        try:
            data = json.loads(row["value"])
            result.append({
                "id": data.get("id", ""),
                "sql": data.get("sql", ""),
                "caller": data.get("caller", ""),
                "created": data.get("created", 0),
                "once": data.get("once", False),
            })
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def unsubscribe(sub_id: str) -> bool:
    """Remove subscription.

    Args:
        sub_id: Subscription ID (with or without 'sub-' prefix)

    Returns:
        True if removed, False if not found

    Example:
        >>> api.unsubscribe("sub-a3f2")
        True
        >>> api.unsubscribe("nonexistent")
        False
    """
    from .core.db import get_db, kv_set

    _ensure_init()

    # Handle prefix
    if not sub_id.startswith("sub-"):
        sub_id = f"sub-{sub_id}"

    key = f"events_sub:{sub_id}"
    conn = get_db()
    row = conn.execute("SELECT 1 FROM kv WHERE key = ?", (key,)).fetchone()

    if not row:
        return False

    kv_set(key, None)
    return True


def stop(*, name: str | None = None) -> bool:
    """Stop instance.

    Args:
        name: Instance to stop (default: self)

    Returns:
        True if stopped, False if already stopped

    Raises:
        HcomError: If instance not found

    Example:
        >>> api.stop()  # stop self
        >>> api.stop(name="bob")
    """
    from .core.ops import op_stop
    from .commands.utils import resolve_identity

    _ensure_init()
    identity = resolve_identity()
    return op_stop(name or identity.name, initiated_by=identity.name)


def start(*, name: str | None = None) -> bool:
    """Start/enable instance.

    Args:
        name: Instance to start (default: self)

    Returns:
        True if started, False if already started

    Raises:
        HcomError: If instance not found or headless instance has exited

    Example:
        >>> api.start()  # start self
        >>> api.start(name="bob")
    """
    from .core.ops import op_start
    from .commands.utils import resolve_identity

    _ensure_init()
    identity = resolve_identity()
    return op_start(name or identity.name, initiated_by=identity.name)


def launch(
    count: int = 1,
    *,
    tag: str | None = None,
    prompt: str | None = None,
    background: bool | None = None,
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    claude_args: str | None = None,
) -> dict:
    """Launch Claude instances.

    Args:
        count: Number of instances to launch
        tag: HCOM_TAG value (creates tag-* instance names)
        prompt: Initial prompt to send (None = inherit from claude_args)
        background: Headless mode (None = inherit from claude_args)
        system_prompt: System prompt (None = inherit from claude_args)
        append_system_prompt: Appended system prompt (None = inherit from claude_args)
        claude_args: Claude CLI args string (--resume, --model, --agent, etc.)

    Returns:
        {"batch_id": str, "launched": int, "failed": int, "background": bool, "log_files": list}

    Raises:
        HcomError: If validation fails or no instances launched

    Example:
        >>> api.launch()
        {"batch_id": "abc123", "launched": 1, "failed": 0, ...}
        >>> api.launch(3, tag="worker", prompt="Do task", background=True)
        {"batch_id": "def456", "launched": 3, "failed": 0, ...}
        >>> api.launch(1, claude_args="--resume abc123 --model opus")
        {"batch_id": "ghi789", "launched": 1, ...}
    """
    from .core.ops import op_launch
    from .commands.utils import resolve_identity
    from .claude_args import resolve_claude_args, merge_claude_args, add_background_defaults

    _ensure_init()

    # Parse claude_args string into spec
    config = get_config()
    env_spec = resolve_claude_args(None, config.claude_args)
    cli_spec = resolve_claude_args(shlex.split(claude_args) if claude_args else None, None)

    # Merge: env defaults + claude_args string
    if cli_spec.clean_tokens or cli_spec.positional_tokens or cli_spec.system_entries:
        spec = merge_claude_args(env_spec, cli_spec)
    else:
        spec = env_spec

    # Apply typed kwargs via update() - these override claude_args
    if prompt is not None or background is not None or system_prompt is not None or append_system_prompt is not None:
        # Determine system prompt updates
        sys_flag = None
        sys_value = None
        if system_prompt is not None:
            sys_flag = "--system-prompt"
            sys_value = system_prompt
        elif append_system_prompt is not None:
            sys_flag = "--append-system-prompt"
            sys_value = append_system_prompt

        spec = spec.update(
            prompt=prompt,
            background=background,
            system_flag=sys_flag,
            system_value=sys_value,
        )

    if spec.has_errors():
        raise HcomError('\n'.join(spec.errors))

    spec = add_background_defaults(spec)
    final_args = spec.rebuild_tokens(include_system=True)

    # Resolve launcher
    identity = resolve_identity()

    return op_launch(
        count,
        final_args,
        launcher=identity.name,
        tag=tag or config.tag,
        background=spec.is_background,
    )


__all__ = [
    # Config (re-exports)
    'get_config',
    'reload_config',
    'HcomConfig',
    'HcomConfigError',
    'ConfigSnapshot',
    'load_config_snapshot',
    'save_config_snapshot',
    'save_config',
    'dict_to_hcom_config',
    # Paths (re-exports)
    'hcom_path',
    'ensure_hcom_directories',
    # Instances (re-exports)
    'get_instance_status',
    'set_status',
    'load_instance_position',
    'update_instance_position',
    # Messages (re-exports)
    'send_message',
    'get_unread_messages',
    'get_read_receipts',
    # Commands (re-exports)
    'cmd_launch',
    'cmd_start',
    'cmd_stop',
    'cmd_send',
    'cmd_reset',
    'cmd_events',
    # Shared (re-exports)
    'ClaudeArgsSpec',
    'resolve_claude_args',
    'HcomError',
    'SenderIdentity',
    # High-level API
    'whoami',
    'instances',
    'send',
    'events',
    'wait',
    'transcript',
    'messages',
    'subscribe',
    'subscriptions',
    'unsubscribe',
    'stop',
    'start',
    'launch',
]
