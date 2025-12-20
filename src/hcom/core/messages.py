"""Message operations - filtering, routing, and delivery"""
from __future__ import annotations

from .instances import load_instance_position, update_instance_position
from .config import get_config
from ..shared import MENTION_PATTERN, SENDER, SenderIdentity, HcomError, parse_iso_timestamp, format_age
from .helpers import validate_scope, is_mentioned
import sys

# ==================== Formatting Helpers ====================

def format_recipients(delivered_to: list[str], max_show: int = 10) -> str:
    """Format recipients list for display.

    Args:
        delivered_to: Instances that received the message (enabled at send time)
        max_show: Max names to show before truncating

    Returns:
        "alice, bob" or "5 instances" or "(none)"
    """
    if not delivered_to:
        return "(none)"

    if len(delivered_to) > max_show:
        return f"{len(delivered_to)} instances"

    return ", ".join(delivered_to)


# ==================== Scope Computation ====================

def compute_scope(message: str, enabled_instances: list[dict]) -> tuple[tuple[str, dict], str | None]:
    """Compute message scope and routing data.

    Args:
        message: Message text
        enabled_instances: List of enabled instance dicts (with 'name' and 'tag' fields)

    Returns:
        ((scope, extra_data), None) on success
        (None, error_message) on validation failure

    Scope types:
        - 'broadcast': No @mentions → everyone
        - 'mentions': Has @targets → explicit targets only

    STRICT FAILURE: @mentions to non-existent or disabled instances return error

    @mention matching uses full display name ({tag}-{name} or {name}):
        - @api-alice matches instance with tag='api', name='alice'
        - @api- matches all instances with tag='api' (prefix match)
        - @alice matches instance with name='alice' (no tag or base name match)
    """
    from .instances import get_full_name

    # Build full name lookup: {full_name: base_name}
    # We store base_name in mentions list (that's the PK) but match against full_name
    full_to_base = {}
    full_names = []
    for inst in enabled_instances:
        if isinstance(inst, str):
            # Backwards compat: plain string (base name or legacy full name)
            full_to_base[inst] = inst
            full_names.append(inst)
        else:
            full = get_full_name(inst)
            base = inst.get('name', '')
            full_to_base[full] = base
            full_names.append(full)

    # Check for @mentions
    if '@' in message:
        mentions = MENTION_PATTERN.findall(message)
        if mentions:
            # Validate all mentions match ENABLED instances only
            matched_base_names = []
            unmatched = []

            for mention in mentions:
                # Check if mention matches any ENABLED instance (prefix match on full name)
                # If mention has no :, only match local instances (exclude :-suffixed remotes)
                # If mention has :, allow matching remote instances
                if ':' in mention:
                    # Mention includes device suffix - match any instance with prefix
                    matches = [full_to_base[fn] for fn in full_names
                              if fn.lower().startswith(mention.lower())]
                else:
                    # Mention is bare name - only match local instances (no : in name)
                    # Don't match across underscore boundary (reserved for subagent hierarchy)
                    matches = [full_to_base[fn] for fn in full_names
                              if ':' not in fn and fn.lower().startswith(mention.lower())
                              and (len(fn) == len(mention) or fn[len(mention)] != '_')]
                if matches:
                    matched_base_names.extend(matches)
                else:
                    unmatched.append(mention)

            # STRICT: fail on unmatched mentions (non-existent OR disabled)
            if unmatched:
                # Special case: literal "@mention" in message text
                if 'mention' in unmatched:
                    return None, "The literal text '@mention' is not a valid target - use actual instance names"

                display = format_recipients(full_names)
                error = f"@mentions to non-existent or stopped instances: {', '.join(f'@{m}' for m in unmatched)}\nAvailable: {display}"
                return None, error

            # Deduplicate matched instances (store base names for DB lookup)
            unique_instances = list(dict.fromkeys(matched_base_names))
            return ('mentions', {'mentions': unique_instances}), None

    # No @mentions → broadcast to everyone
    return ('broadcast', {}), None


def _should_deliver(scope: str, extra: dict, receiver_name: str, sender_name: str) -> bool:
    """Check if message should be delivered to receiver based on scope.

    Args:
        scope: Message scope ('broadcast', 'mentions')
        extra: Extra scope data (mentions list for 'mentions' scope)
        receiver_name: Instance to check delivery for
        sender_name: Sender name (excluded from delivery)

    Returns:
        True if receiver should get the message
    """
    if receiver_name == sender_name:
        return False

    validate_scope(scope)

    if scope == 'broadcast':
        return True
    if scope == 'mentions':
        return receiver_name in extra.get('mentions', [])

    return False

# ==================== Core Message Operations ====================

def resolve_reply_to(reply_to: str) -> tuple[int | None, str | None]:
    """Resolve reply_to reference to local event ID.

    Handles both local (42) and cross-device (42:BOXE) formats.

    Args:
        reply_to: Event reference string - "42" or "42:BOXE"

    Returns:
        (local_event_id, warning_message)
        - local_event_id: Resolved local event ID, or None if can't resolve
        - warning_message: Warning string if resolution failed, else None
    """
    from .db import get_db

    conn = get_db()

    if ':' in reply_to:
        # Cross-device format: 42:BOXE
        parts = reply_to.split(':', 1)
        try:
            remote_id = int(parts[0])
            short_device = parts[1].upper()
        except (ValueError, IndexError):
            return None, f"Invalid reply_to format: {reply_to}"

        # Look up by relay origin metadata
        row = conn.execute(
            """
            SELECT id FROM events
            WHERE json_extract(data, '$._relay.short') = ?
              AND (
                json_extract(data, '$._relay.id') = ?
                OR json_extract(data, '$._relay.id') = ?
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (short_device, remote_id, str(remote_id)),
        ).fetchone()

        if row:
            return row['id'], None
        return None, f"Remote event {reply_to} not found locally"
    else:
        # Local format: 42
        try:
            local_id = int(reply_to)
        except ValueError:
            return None, f"Invalid reply_to format: {reply_to}"

        # Verify event exists
        row = conn.execute("SELECT id FROM events WHERE id = ?", (local_id,)).fetchone()
        if row:
            return local_id, None
        return None, f"Event #{reply_to} not found"


def get_thread_from_event(event_id: int) -> str | None:
    """Get thread field from an event by ID.

    Args:
        event_id: Local event ID

    Returns:
        Thread string if present, else None
    """
    from .db import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT json_extract(data, '$.thread') as thread FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    return row['thread'] if row and row['thread'] else None


def unescape_bash(text: str) -> str:
    """Remove bash escape sequences from message content.

    Bash escapes special characters when constructing commands. Since hcom
    receives messages as command arguments, we unescape common sequences
    that don't affect the actual message intent.

    NOTE: We do NOT unescape '\\\\' to '\\'. If double backslashes survived
    bash processing, the user intended them (e.g., Windows paths, regex, JSON).
    Unescaping would corrupt legitimate data.
    """
    # Common bash escapes that appear in double-quoted strings
    replacements = [
        ('\\!', '!'),   # History expansion
        ('\\$', '$'),   # Variable expansion
        ('\\`', '`'),   # Command substitution
        ('\\"', '"'),   # Double quote
        ("\\'", "'"),   # Single quote (less common in double quotes but possible)
    ]
    for escaped, unescaped in replacements:
        text = text.replace(escaped, unescaped)
    return text

def send_message(
    identity: SenderIdentity,
    message: str,
    envelope: dict[str, str] | None = None
) -> list[str]:
    """Send a message to the database and notify all instances.

    Args:
        identity: Sender identity (kind + name + instance_data)
        message: Message text
        envelope: Optional envelope fields {intent, reply_to, thread}

    Returns:
        delivered_to list (base names of enabled instances that will receive)

    Raises:
        HcomError: If validation fails or database write fails
    """
    from .db import log_event, get_db
    from .instances import get_full_name

    conn = get_db()

    # Get enabled instances with name and tag (for @mention validation with full names)
    enabled_rows = conn.execute(
        "SELECT name, tag FROM instances WHERE enabled = 1"
    ).fetchall()
    enabled_instances = [{'name': row['name'], 'tag': row['tag']} for row in enabled_rows]

    # For @mention validation: enabled instances + CLI identity (bigboss as plain string)
    mentionable = enabled_instances + [SENDER]

    # Compute scope and routing data (validates @mentions against full names)
    scope_result, error = compute_scope(message, mentionable)
    if error:
        raise HcomError(error)

    scope, extra = scope_result

    # Compute delivered_to: base names of enabled instances in scope
    # Use base name for delivery check since that's what's stored in mentions
    delivered_to = [
        inst['name'] for inst in enabled_instances
        if _should_deliver(scope, extra, inst['name'], identity.name)
    ]

    # Build event data
    # Note: 'from' and 'delivered_to' store BASE names for DB consistency.
    # Display code converts to full names via get_full_name() at render time.
    data = {
        'from': identity.name,          # Base name (display code converts to full)
        'sender_kind': identity.kind,   # 'external' or 'instance' for filtering
        'scope': scope,                 # Routing scope
        'text': message,
        'delivered_to': delivered_to,   # Base names of recipients
    }

    # Add scope extra data (mentions, group_id)
    if extra:
        data.update(extra)

    # Add envelope fields if provided
    if envelope:
        if intent := envelope.get('intent'):
            data['intent'] = intent
        if reply_to := envelope.get('reply_to'):
            data['reply_to'] = reply_to
            # Resolve to local event ID for easier queries
            local_id, _ = resolve_reply_to(reply_to)
            if local_id:
                data['reply_to_local'] = local_id
        if thread := envelope.get('thread'):
            data['thread'] = thread

    # Log to SQLite with namespace separation
    # External senders use 'ext_{name}' prefix for clear namespace isolation
    # System senders use 'sys_{name}' prefix (e.g., sys_[hcom-launcher])
    # Instance senders use real instance name
    # Actual sender name preserved in data['from'] for display
    if identity.kind == 'external':
        routing_instance = f'ext_{identity.name}'
    elif identity.kind == 'system':
        routing_instance = f'sys_{identity.name}'
    else:
        routing_instance = identity.name

    try:
        log_event(
            event_type='message',
            instance=routing_instance,
            data=data
        )
    except Exception as e:
        raise HcomError(f"Failed to write message to database: {e}")

    # Push to relay server immediately (messages always push)
    try:
        from ..relay import push
        push(force=True)
    except Exception:
        pass  # Best effort

    # Notify all instances after successful write
    from .runtime import notify_all_instances
    notify_all_instances()

    return delivered_to


def send_system_message(sender_name: str, message: str) -> list[str]:
    """Send a system notification message.

    Args:
        sender_name: System sender identifier (e.g., 'hcom-launcher', 'hcom-watchdog')
        message: Message text (can include @mentions for targeting)

    Returns:
        delivered_to list

    Raises:
        HcomError: If validation fails or database write fails
    """
    identity = SenderIdentity(kind='system', name=sender_name, instance_data=None)
    return send_message(identity, message)


def get_unread_messages(instance_name: str, update_position: bool = False) -> tuple[list[dict[str, str]], int]:
    """Get unread messages for instance with scope-based filtering
    Args:
        instance_name: Name of instance to get messages for
        update_position: If True, mark messages as read by updating position
    Returns:
        Tuple of (messages, max_event_id)
    """
    from .db import get_events_since

    # Get last processed event ID from instance file
    instance_data = load_instance_position(instance_name)
    last_event_id = instance_data.get('last_event_id', 0)

    # Query new message events
    events = get_events_since(last_event_id, event_type='message')

    if not events:
        return [], last_event_id

    messages = []
    for event in events:
        event_data = event['data']

        # Validate scope field present
        if 'scope' not in event_data:
            print(
                f"Error: Message event {event['id']} missing 'scope' field (old format). "
                f"Run 'hcom reset logs' to clear old messages.",
                file=sys.stderr
            )
            continue

        # Skip own messages
        sender_name = event_data['from']
        if sender_name == instance_name:
            continue

        # Apply scope-based filtering
        try:
            if should_deliver_message(event_data, instance_name, sender_name):
                msg = {
                    'timestamp': event['timestamp'],
                    'from': sender_name,
                    'message': event_data['text'],
                    'delivered_to': event_data.get('delivered_to', []),
                    'event_id': event['id'],
                }
                # Include envelope fields if present
                if intent := event_data.get('intent'):
                    msg['intent'] = intent
                if thread := event_data.get('thread'):
                    msg['thread'] = thread
                if relay := event_data.get('_relay'):
                    msg['_relay'] = relay
                messages.append(msg)
        except ValueError as e:
            print(
                f"Error: Corrupt message data in event {event['id']}: {e}. "
                f"Run 'hcom reset logs' to clear corrupt messages.",
                file=sys.stderr
            )
            continue

    # Max event ID from events we processed
    max_event_id = events[-1]['id'] if events else last_event_id

    # Only update position (ie mark as read) if explicitly requested (after successful delivery)
    if update_position:
        update_instance_position(instance_name, {'last_event_id': max_event_id})

    return messages, max_event_id

# ==================== Message Filtering & Routing ====================

def should_deliver_message(event_data: dict, receiver_name: str, sender_name: str) -> bool:
    """Check if message should be delivered based on scope.

    Args:
        event_data: Message event data with 'scope' field
        receiver_name: Instance to check delivery for
        sender_name: Sender name (excluded from delivery)

    Returns:
        True if receiver should get the message

    Raises:
        KeyError: If scope field missing (old message format)
        ValueError: If scope value invalid
    """
    if receiver_name == sender_name:
        return False

    if 'scope' not in event_data:
        raise KeyError("Message missing 'scope' field (old format)")

    scope = event_data['scope']
    validate_scope(scope)

    if scope == 'broadcast':
        return True

    if scope == 'mentions':
        mentions = event_data.get('mentions', [])
        # Strip device suffix for cross-device matching
        # e.g., 'mude' matches 'mude:BOXE' after stripping
        receiver_base = receiver_name.split(':')[0]
        return any(receiver_base == m.split(':')[0] for m in mentions)

    return False


# Note: determine_message_recipients() removed - obsolete after scope refactor
# Use compute_scope() + _should_deliver() directly instead (see send_message() or get_recipient_feedback())


def get_subagent_messages(parent_name: str, since_id: int = 0, limit: int = 0) -> tuple[list[dict[str, str]], int, dict[str, int]]:
    """Get messages from/to subagents of parent instance with scope-based filtering
    Args:
        parent_name: Parent instance name (e.g., 'alice')
        since_id: Event ID to read from (default 0 = all messages)
        limit: Max messages to return (0 = all)
    Returns:
        Tuple of (messages from/to subagents, last_event_id, per_subagent_counts)
        per_subagent_counts: {'alice_reviewer': 2, 'alice_debugger': 0, ...}
    """
    from .db import get_events_since

    # Query all message events since last check
    events = get_events_since(since_id, event_type='message')

    if not events:
        return [], since_id, {}

    # Get all subagent names for this parent using SQL query
    from .db import get_db
    conn = get_db()
    subagent_names = [row['name'] for row in
                      conn.execute("SELECT name FROM instances WHERE parent_name = ?", (parent_name,)).fetchall()]

    # Initialize per-subagent counts
    per_subagent_counts = {name: 0 for name in subagent_names}
    subagent_names_set = set(subagent_names)  # For fast lookup

    # Filter for messages from/to subagents and track per-subagent counts
    subagent_messages = []
    for event in events:
        event_data = event['data']

        # Validate scope field present
        if 'scope' not in event_data:
            print(
                f"Error: Message event {event['id']} missing 'scope' field (old format). "
                f"Run 'hcom reset logs' to clear old messages.",
                file=sys.stderr
            )
            continue

        sender_name = event_data['from']

        # Build message dict
        msg = {
            'timestamp': event['timestamp'],
            'from': sender_name,
            'message': event_data['text']
        }

        # Messages FROM subagents
        if sender_name in subagent_names_set:
            subagent_messages.append(msg)
            # Track which subagents would receive this message
            for subagent_name in subagent_names:
                if subagent_name != sender_name:
                    try:
                        if should_deliver_message(event_data, subagent_name, sender_name):
                            per_subagent_counts[subagent_name] += 1
                    except ValueError as e:
                        print(
                            f"Error: Corrupt message data in event {event['id']}: {e}. "
                            f"Run 'hcom reset logs' to clear corrupt messages.",
                            file=sys.stderr
                        )
                        continue
        # Messages TO subagents via @mentions or broadcasts
        elif subagent_names:
            # Check which subagents should receive this message
            matched = False
            for subagent_name in subagent_names:
                try:
                    if should_deliver_message(event_data, subagent_name, sender_name):
                        if not matched:
                            subagent_messages.append(msg)
                            matched = True
                        per_subagent_counts[subagent_name] += 1
                except ValueError as e:
                    print(
                        f"Error: Corrupt message data in event {event['id']}: {e}. "
                        f"Run 'hcom reset logs' to clear corrupt messages.",
                        file=sys.stderr
                    )
                    break  # Skip remaining subagents for this message

    if limit > 0:
        subagent_messages = subagent_messages[-limit:]

    last_event_id = events[-1]['id'] if events else since_id
    return subagent_messages, last_event_id, per_subagent_counts

# ==================== Message Formatting ====================

def _build_message_prefix(msg: dict) -> str:
    """Build message prefix from envelope fields.

    Format: [intent:thread #id] or [intent #id] or [thread:name #id] or [new message #id]
    Remote messages: #id:DEVICE

    Args:
        msg: Message dict with optional 'intent', 'thread', 'event_id', '_relay'

    Returns:
        Formatted prefix string like "[request:pr-42 #42]"
    """
    intent = msg.get('intent')
    thread = msg.get('thread')
    event_id = msg.get('event_id')
    relay = msg.get('_relay', {})

    # Build ID reference (local or remote)
    if relay and relay.get('short') and relay.get('id'):
        id_ref = f"#{relay['id']}:{relay['short']}"
    elif event_id:
        id_ref = f"#{event_id}"
    else:
        id_ref = ""

    # Build prefix based on envelope fields
    if intent and thread:
        prefix = f"{intent}:{thread}"
    elif intent:
        prefix = intent
    elif thread:
        prefix = f"thread:{thread}"
    else:
        prefix = "new message"

    if id_ref:
        return f"[{prefix} {id_ref}]"
    return f"[{prefix}]"


def format_hook_messages(messages: list[dict[str, str]], instance_name: str) -> str:
    """Format messages for hook feedback.

    Single message uses verbose format: "sender → you + N others"
    Multiple messages use compact format: "sender → you (+N)"

    Format includes envelope info: [intent:thread #id] sender → recipient: text
    """
    from .instances import get_full_name

    def _others_count(msg: dict) -> int:
        """Count other recipients (excluding self)"""
        delivered_to = msg.get('delivered_to', [])
        # Others = total recipients minus self
        return max(0, len(delivered_to) - 1)

    def _get_sender_display_name(sender_base_name: str) -> str:
        """Get full display name for sender (base name -> tag-base or base)"""
        sender_data = load_instance_position(sender_base_name)
        return get_full_name(sender_data) or sender_base_name

    if len(messages) == 1:
        msg = messages[0]
        others = _others_count(msg)
        if others > 0:
            recipient = f"{instance_name} (+{others} other{'s' if others > 1 else ''})"
        else:
            recipient = instance_name
        prefix = _build_message_prefix(msg)
        sender_display = _get_sender_display_name(msg['from'])
        reason = f"{prefix} {sender_display} → {recipient}: {msg['message']}"
    else:
        parts = []
        for msg in messages:
            others = _others_count(msg)
            if others > 0:
                recipient = f"{instance_name} (+{others})"
            else:
                recipient = instance_name
            prefix = _build_message_prefix(msg)
            sender_display = _get_sender_display_name(msg['from'])
            parts.append(f"{prefix} {sender_display} → {recipient}: {msg['message']}")
        reason = f"[{len(messages)} new messages] | {' | '.join(parts)}"

    # Append hints to messages: instance-specific first, then global config
    instance_data = load_instance_position(instance_name)
    hints = None
    if instance_data:
        hints = instance_data.get('hints')  # Per-instance override
    if not hints:
        hints = get_config().hints  # Global fallback
    if hints:
        reason = f"{reason} | [{hints}]"

    return reason

def get_read_receipts(identity: SenderIdentity, max_text_length: int = 50, limit: int = None) -> list[dict]:
    """Get read receipts for messages sent by sender.
    Args:
        identity: SenderIdentity for the sender (external or instance)
        max_text_length: Maximum text length before truncation (default 50)
        limit: Maximum number of recent messages to return (default None = all)
    Returns:
        List of dicts with keys: id, age, text, read_by, total_recipients
    """
    from .db import get_db
    from datetime import datetime, timezone
    import json

    conn = get_db()

    # Determine storage name: external senders use ext_ prefix, instances use real name
    storage_name = f'ext_{identity.name}' if identity.kind == 'external' else identity.name

    # Query by storage name
    query = """
        SELECT e.id, e.timestamp, e.data
        FROM events e
        WHERE e.type = 'message'
          AND e.instance = ?
        ORDER BY e.id DESC
    """

    if limit is not None:
        query += f" LIMIT {int(limit)}"

    sent_messages = conn.execute(query, (storage_name,)).fetchall()

    if not sent_messages:
        return []

    # Get all instances
    active_instances_query = """
        SELECT name, tag, last_event_id, session_id, mapid, parent_session_id, origin_device_id
        FROM instances
        WHERE name != ?
    """
    active_instances = conn.execute(active_instances_query, (identity.name,)).fetchall()

    if not active_instances:
        return []

    instance_reads = {row['name']: row['last_event_id'] for row in active_instances}
    instance_data_cache = {row['name']: {
        'tag': row['tag'],
        'session_id': row['session_id'],
        'mapid': row['mapid'],
        'parent_session_id': row['parent_session_id'],
        'origin_device_id': row['origin_device_id']
    } for row in active_instances}

    # For remote instances, get their max msg_ts from status events
    remote_msg_ts = {}
    remote_instances = [row['name'] for row in active_instances if row['origin_device_id']]
    if remote_instances:
        # Query max msg_ts per remote instance from their status events
        for inst_name in remote_instances:
            row = conn.execute("""
                SELECT json_extract(data, '$.msg_ts') as msg_ts
                FROM events
                WHERE type = 'status' AND instance = ?
                  AND json_extract(data, '$.msg_ts') IS NOT NULL
                ORDER BY id DESC LIMIT 1
            """, (inst_name,)).fetchone()
            if row and row['msg_ts']:
                remote_msg_ts[inst_name] = row['msg_ts']

    receipts = []
    now = datetime.now(timezone.utc)

    for msg_row in sent_messages:
        msg_id = msg_row['id']
        msg_timestamp = msg_row['timestamp']
        msg_data = json.loads(msg_row['data'])
        msg_text = msg_data['text']

        # Validate scope field present (skip old messages)
        if 'scope' not in msg_data:
            continue

        # Use delivered_to for read receipt denominator
        if 'delivered_to' not in msg_data:
            continue

        delivered_to = msg_data['delivered_to']

        # Find recipients that HAVE read this message
        read_by = []
        for inst_name in delivered_to:
            inst_data = instance_data_cache.get(inst_name)

            # Remote instance: compare msg_ts (timestamp-based)
            if inst_data and inst_data.get('origin_device_id'):
                if inst_name in remote_msg_ts and remote_msg_ts[inst_name] >= msg_timestamp:
                    read_by.append(inst_name)
                continue

            # Local instance: compare position (ID-based)
            if instance_reads.get(inst_name, 0) >= msg_id:
                # For external senders, only mark as read if they were @mentioned
                if inst_data:
                    from ..core.instances import is_external_sender
                    if is_external_sender(inst_data):
                        inst_tag = inst_data.get('tag')
                        if not is_mentioned(msg_text, inst_name, inst_tag):
                            continue
                read_by.append(inst_name)

        total_recipients = len(delivered_to)

        if total_recipients > 0:
            # Calculate age
            msg_time = parse_iso_timestamp(msg_timestamp)
            age_str = format_age((now - msg_time).total_seconds()) if msg_time else "?"

            # Truncate text
            if len(msg_text) > max_text_length:
                truncated_text = msg_text[:max_text_length - 3] + "..."
            else:
                truncated_text = msg_text

            receipts.append({
                'id': msg_id,
                'age': age_str,
                'text': truncated_text,
                'read_by': read_by,
                'total_recipients': total_recipients
            })

    return receipts

__all__ = [
    'format_recipients',
    'compute_scope',
    '_should_deliver',
    'unescape_bash',
    'send_message',
    'get_unread_messages',
    'should_deliver_message',
    'get_subagent_messages',
    'format_hook_messages',
    'get_read_receipts',
]
