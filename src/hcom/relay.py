"""Cross-device relay - state-embedded events

Custom server implementation:
  Required endpoints:
    POST /push/{device_id}  — receive JSON: {"state": {...}, "events": [...]}
    GET /poll?since=&timeout=  — long-poll, return {"devices": {...}, "ts": float}
    GET /version  — return {"v": 1}
    GET /devices  — return list of active devices

  Reference: https://huggingface.co/spaces/aannoo/hcom-relay/blob/main/app.py

  Configure with:
    hcom config relay https://your-server.example.com
    hcom config relay_token <token>  # optional auth
"""
from __future__ import annotations
import json
import sqlite3
import time
import urllib.request
import urllib.error
import socket
from typing import Any
from datetime import datetime

from .core.device import get_device_uuid, get_device_short_id
from .core.db import get_db, log_event, kv_get, kv_set, _write_lock
from .core.config import get_config
from .core.paths import hcom_path
from .shared import parse_iso_timestamp

_LOG_PATH = None
_poll_ts: float = 0


def _log(op: str, detail: str = '', **kwargs) -> None:
    """Log relay operation."""
    global _LOG_PATH
    if _LOG_PATH is None:
        _LOG_PATH = hcom_path('.tmp', 'logs', 'relay.log')
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%H:%M:%S')
    line = f"{ts}|{op}|{detail}" + "".join(f"|{k}={v}" for k, v in kwargs.items())
    try:
        with open(_LOG_PATH, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass  # Log file write failure is non-critical


def _get_relay_url() -> str | None:
    """Get relay URL from config (returns None if disabled)."""
    config = get_config()
    if not config.relay_enabled:
        return None
    url = config.relay
    return url.rstrip('/') if url and url.startswith('http') else None


def is_relay_enabled() -> bool:
    """Check if relay is configured AND enabled."""
    config = get_config()
    return bool(config.relay and config.relay_enabled)


def _get_auth_headers() -> dict[str, str]:
    """Get auth headers."""
    headers = {'Content-Type': 'application/json'}
    if token := get_config().relay_token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _http(method: str, url: str, data: bytes = None, timeout: int = 5) -> tuple[int, bytes]:
    """HTTP request."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in _get_auth_headers().items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return (e.code, b'')
    except (urllib.error.URLError, socket.timeout):
        return (0, b'')


# ==================== State ====================

def build_state() -> dict[str, Any]:
    """Build current instance state snapshot."""
    conn = get_db()
    rows = conn.execute("""
        SELECT name, enabled, status, status_context, status_time, parent_name, session_id, parent_session_id, agent_id, directory, transcript_path, wait_timeout, last_stop, tcp_mode
        FROM instances WHERE COALESCE(origin_device_id, '') = ''
    """).fetchall()

    instances = {}
    for row in rows:
        name = row['name']
        if name.startswith('_') or name.startswith('sys_'):
            continue
        instances[name] = {
            'enabled': bool(row['enabled']),
            'status': row['status'] or 'unknown',
            'context': row['status_context'] or '',
            'status_time': row['status_time'] or 0,
            'parent': row['parent_name'] or None,
            'session_id': row['session_id'] or None,
            'parent_session_id': row['parent_session_id'] or None,
            'agent_id': row['agent_id'] or None,
            'directory': row['directory'] or None,
            'transcript': row['transcript_path'] or None,
            'wait_timeout': row['wait_timeout'] or 1800,
            'last_stop': row['last_stop'] or 0,
            'tcp_mode': bool(row['tcp_mode'])
        }

    # Get reset timestamp (local only - exclude imported events)
    reset_row = conn.execute("""
        SELECT timestamp FROM events
        WHERE type = 'life' AND instance = '_device'
        AND json_extract(data, '$.action') = 'reset'
        AND json_extract(data, '$._relay') IS NULL
        ORDER BY id DESC LIMIT 1
    """).fetchone()

    reset_ts = 0.0
    if reset_row and reset_row['timestamp']:
        dt = parse_iso_timestamp(reset_row['timestamp'])
        if dt:
            reset_ts = dt.timestamp()

    return {
        'instances': instances,
        'short_id': get_device_short_id(),
        'reset_ts': reset_ts
    }


# ==================== Push ====================

def push(force: bool = False) -> tuple[bool, str | None]:
    """Push state and new events to server.

    Returns:
        (success, error_message) - error_message is None on success
    """
    url = _get_relay_url()
    if not url:
        return (False, None)  # Not configured, not an error

    # Rate limit
    if not force:
        last = float(kv_get('relay_last_push') or 0)
        if time.time() - last < 1.0:
            return (True, None)

    device_id = get_device_uuid()
    state = build_state()

    # Get new events since last push (exclude imported events - they have _relay marker)
    last_push_id = int(kv_get('relay_last_push_id') or 0)
    conn = get_db()
    rows = conn.execute("""
        SELECT id, timestamp, type, instance, data FROM events
        WHERE id > ? AND instance NOT LIKE '%:%'
        AND instance != '_device'
        AND json_extract(data, '$._relay') IS NULL
        ORDER BY id LIMIT 100
    """, (last_push_id,)).fetchall()

    events = []
    max_id = last_push_id
    for row in rows:
        events.append({
            'id': row['id'],  # Monotonic event ID for dedup on pull
            'ts': row['timestamp'],
            'type': row['type'],
            'instance': row['instance'],
            'data': json.loads(row['data'])
        })
        max_id = max(max_id, row['id'])

    status, content = _http(
        'POST', f"{url}/push/{device_id}",
        data=json.dumps({'state': state, 'events': events}).encode(),
        timeout=3
    )
    if status == 200:
        kv_set('relay_last_push', str(time.time()))
        kv_set('relay_last_push_id', str(max_id))
        kv_set('relay_status', 'ok')
        kv_set('relay_last_error', None)
        _log('PUSH', f'events={len(events)}')
        return (True, None)
    elif status == 0:
        error = 'network unreachable'
        kv_set('relay_status', 'error')
        kv_set('relay_last_error', error)
        _log('ERROR', 'push failed', error=error)
        return (False, error)
    else:
        error = f'server returned {status}'
        kv_set('relay_status', 'error')
        kv_set('relay_last_error', error)
        _log('ERROR', 'push failed', error=error)
        return (False, error)


# ==================== Pull ====================

def pull(timeout: int = 0) -> tuple[dict[str, Any], str | None]:
    """Pull remote devices. timeout=0 for immediate, >0 for long-poll.

    Returns:
        (result_dict, error_message) - error_message is None on success
    """
    global _poll_ts

    url = _get_relay_url()
    if not url:
        return ({'devices': {}}, None)  # Not configured

    status, content = _http(
        'GET', f"{url}/poll?since={_poll_ts}&timeout={timeout}",
        timeout=timeout + 5 if timeout else 5
    )
    if status == 200 and content:
        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            error = f'invalid json: {e}'
            kv_set('relay_status', 'error')
            kv_set('relay_last_error', error)
            _log('ERROR', 'pull failed', error=error)
            return ({'devices': {}}, error)

        _poll_ts = result.get('ts', _poll_ts)
        kv_set('relay_status', 'ok')
        kv_set('relay_last_error', None)

        devices = result.get('devices', {})
        if devices:
            own_device = get_device_uuid()
            _apply_remote_devices(devices, own_device)

        return (result, None)
    elif status == 0:
        error = 'network unreachable'
        kv_set('relay_status', 'error')
        kv_set('relay_last_error', error)
        _log('ERROR', 'pull failed', error=error)
        return ({'devices': {}}, error)
    else:
        error = f'server returned {status}'
        kv_set('relay_status', 'error')
        kv_set('relay_last_error', error)
        _log('ERROR', 'pull failed', error=error)
        return ({'devices': {}}, error)


def _apply_remote_devices(devices: dict[str, dict], own_device: str) -> None:
    """Apply remote device state and events."""
    conn = get_db()
    own_short_id = get_device_short_id()

    # Get local reset timestamp from KV (set by cmd_reset for cross-process reliability)
    # Fallback to events table for long-running pollers that missed the KV write
    local_reset_ts = float(kv_get('relay_local_reset_ts') or 0)
    if local_reset_ts == 0:
        row = conn.execute("""
            SELECT timestamp FROM events
            WHERE type='life' AND instance='_device'
              AND json_extract(data, '$.action')='reset'
              AND json_extract(data, '$._relay') IS NULL
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        if row:
            local_reset_ts = _parse_ts(row[0])
            if local_reset_ts:
                kv_set('relay_local_reset_ts', str(local_reset_ts))
    if local_reset_ts == 0:
        _log('WARN', 'local_reset_ts=0, quarantine disabled')

    for device_id, payload in devices.items():
        if device_id == own_device:
            continue

        state = payload.get('state', {})
        events = payload.get('events', [])
        short_id = state.get('short_id', device_id[:4].upper())
        reset_ts = state.get('reset_ts', 0)

        # Detect short_id collision: two different devices with same short_id
        # Would cause instance primary key collisions (name:SHORT)
        cached_device = kv_get(f'relay_short_{short_id}')
        if cached_device and cached_device != device_id:
            _log('COLLISION', f'short_id={short_id}', existing=cached_device[:8], incoming=device_id[:8])
            continue  # Skip this device to prevent data corruption
        if not cached_device:
            kv_set(f'relay_short_{short_id}', device_id)

        # Check for device reset FIRST - always clean old data before deciding to import
        # This must run even if we later skip the device (stale check)
        cached_reset = float(kv_get(f'relay_reset_{device_id}') or 0)
        if reset_ts > cached_reset:
            with _write_lock:
                conn.execute("DELETE FROM instances WHERE origin_device_id = ?", (device_id,))
                conn.execute("DELETE FROM events WHERE json_extract(data, '$._relay.device') = ?", (device_id,))
                conn.commit()
            kv_set(f'relay_reset_{device_id}', str(reset_ts))
            # Reset event cursor so new events from restarted device are imported
            kv_set(f'relay_events_{device_id}', '0')
            _log('RESET', f'device={short_id}')

        # Note: Device-level quarantine removed - caused deadlocks when devices reset at different times.
        # Per-event and per-instance timestamp filtering handles stale data instead.

        # Get current remote instances for this device (to detect removals)
        current_remote = {row['name'] for row in conn.execute(
            "SELECT name FROM instances WHERE origin_device_id = ?", (device_id,)
        ).fetchall()}

        # Upsert instances from state (no lifecycle reconstruction!)
        seen_instances = set()
        for name, inst in state.get('instances', {}).items():
            # Skip instances with no activity or activity from before our reset
            status_time = inst.get('status_time', 0)
            if local_reset_ts > 0 and status_time < local_reset_ts:
                continue

            namespaced = f"{name}:{short_id}"
            seen_instances.add(namespaced)
            # Namespace parent with short_id suffix
            parent_namespaced = f"{inst['parent']}:{short_id}" if inst.get('parent') else None
            try:
                with _write_lock:
                    conn.execute("""
                        INSERT INTO instances (
                            name, origin_device_id, enabled, status, status_context, status_time,
                            parent_name, directory, transcript_path, created_at,
                            session_id, parent_session_id, agent_id, wait_timeout, last_stop, tcp_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            enabled = excluded.enabled, status = excluded.status,
                            status_context = excluded.status_context, status_time = excluded.status_time,
                            parent_name = excluded.parent_name,
                            directory = excluded.directory, transcript_path = excluded.transcript_path,
                            session_id = excluded.session_id, parent_session_id = excluded.parent_session_id,
                            agent_id = excluded.agent_id, wait_timeout = excluded.wait_timeout,
                            last_stop = excluded.last_stop, tcp_mode = excluded.tcp_mode
                    """, (
                        namespaced, device_id, inst.get('enabled', False),
                        inst.get('status', 'unknown'), inst.get('context', ''),
                        inst.get('status_time', 0),
                        parent_namespaced, inst.get('directory'), inst.get('transcript'),
                        time.time(),
                        inst.get('session_id'), inst.get('parent_session_id'), inst.get('agent_id'),
                        inst.get('wait_timeout', 1800), inst.get('last_stop', 0), inst.get('tcp_mode', False)
                    ))
                    conn.commit()
            except sqlite3.Error as e:
                _log('ERROR', f'instance upsert failed: {namespaced}', error=str(e)[:50])

        # Remove instances no longer in state (stopped/removed on remote)
        stale = current_remote - seen_instances
        if stale:
            with _write_lock:
                for name in stale:
                    conn.execute("DELETE FROM instances WHERE name = ?", (name,))
                conn.commit()

        # Handle control events targeting this device
        _handle_control_events(events, own_short_id, device_id)

        # Insert events (for history + message delivery)
        # Dedup by monotonic event ID (not timestamp - avoids clock skew issues)
        last_event_id = int(kv_get(f'relay_events_{device_id}') or 0)

        # Migration: detect old timestamp values (> 1 billion) and reset
        # Event IDs are SQLite auto-increment, never reach billions
        if last_event_id > 1_000_000_000:
            last_event_id = 0
            kv_set(f'relay_events_{device_id}', '0')

        # Detect ID regression: remote DB was recreated without proper reset event
        # SQLite autoincrement IDs never decrease, so regression = DB recreation
        if events and last_event_id > 0:
            remote_max_id = max((e.get('id', 0) for e in events if e.get('type') != 'control'), default=0)
            if remote_max_id > 0 and remote_max_id < last_event_id:
                _log('RESET_DETECTED', f'device={short_id}', reason=f'id_regression:{remote_max_id}<{last_event_id}')
                with _write_lock:
                    conn.execute("DELETE FROM instances WHERE origin_device_id = ?", (device_id,))
                    conn.execute("DELETE FROM events WHERE json_extract(data, '$._relay.device') = ?", (device_id,))
                    conn.commit()
                last_event_id = 0
                kv_set(f'relay_events_{device_id}', '0')

        max_event_id = last_event_id

        for event in events:
            # Skip control events (already handled above)
            if event.get('type') == 'control':
                continue

            # Skip _device events (reset_ts is in state, not events)
            if event.get('instance') == '_device':
                continue

            raw_event_id = event.get('id', 0)
            try:
                event_id = int(raw_event_id)
            except (TypeError, ValueError):
                _log('BAD_EVENT_ID', f'device={short_id}', raw=raw_event_id)
                continue
            if event_id <= last_event_id:
                continue  # Already have this event

            # Skip events from before our reset (stale data from peer's old DB)
            event_ts = _parse_ts(event.get('ts', 0))
            if local_reset_ts > 0 and event_ts > 0 and event_ts < local_reset_ts:
                continue

            # Namespace instance
            instance = event.get('instance', '')
            if instance and ':' not in instance and not instance.startswith('_'):
                instance = f"{instance}:{short_id}"

            # Namespace 'from' and 'mentions' in message data
            data = event.get('data', {}).copy()
            if 'from' in data and ':' not in data['from']:
                data['from'] = f"{data['from']}:{short_id}"
            if 'mentions' in data:
                data['mentions'] = [
                    f"{m}:{short_id}" if ':' not in m else m
                    for m in data['mentions']
                ]

            # Strip our device suffix from delivered_to so local instances match
            if 'delivered_to' in data:
                data['delivered_to'] = [
                    name.rsplit(':', 1)[0] if name.upper().endswith(f':{own_short_id}') else name
                    for name in data['delivered_to']
                ]

            # Store relay origin for cross-device reply_to resolution
            data['_relay'] = {'device': device_id, 'short': short_id, 'id': event_id}

            log_event(
                event_type=event.get('type', 'unknown'),
                instance=instance,
                data=data,
                timestamp=event.get('ts')
            )
            max_event_id = max(max_event_id, event_id)

        if max_event_id > last_event_id:
            kv_set(f'relay_events_{device_id}', str(max_event_id))

        # Update sync timestamp for this device (separate from event ID cursor)
        kv_set(f'relay_sync_time_{device_id}', str(time.time()))

    # Wake local TCP instances so they see new messages immediately
    from .core.runtime import notify_all_instances
    notify_all_instances()


def _parse_ts(ts) -> float:
    """Parse timestamp to float."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        dt = parse_iso_timestamp(ts)
        if dt:
            return dt.timestamp()
    return 0.0


# ==================== Remote Control ====================

def send_control(action: str, target: str, device_short_id: str) -> bool:
    """Send control command to remote device."""
    url = _get_relay_url()
    if not url:
        return False

    device_id = get_device_uuid()
    short_id = get_device_short_id()

    control_event = {
        'ts': time.time(),
        'type': 'control',
        'instance': '_control',
        'data': {
            'action': action,
            'target': target,
            'target_device': device_short_id,
            'from': f"_:{short_id}",
            'from_device': device_id
        }
    }

    # Push immediately with control event
    state = build_state()
    status, _ = _http(
        'POST', f"{url}/push/{device_id}",
        data=json.dumps({'state': state, 'events': [control_event]}).encode(),
        timeout=3
    )
    if status == 200:
        _log('CONTROL', f'{action} {target}:{device_short_id}')
        return True
    elif status == 0:
        _log('ERROR', 'control failed', error='network unreachable')
    else:
        _log('ERROR', 'control failed', error=f'status={status}')
    return False


def _handle_control_events(events: list[dict], own_short_id: str, source_device: str) -> None:
    """Process control events targeting this device."""
    from .hooks.utils import disable_instance
    from .core.instances import enable_instance

    # Dedup: skip already-processed control events from this device
    last_ctrl_ts = float(kv_get(f'relay_ctrl_{source_device}') or 0)
    max_ctrl_ts = last_ctrl_ts

    for event in events:
        if event.get('type') != 'control':
            continue

        # Timestamp dedup
        event_ts = _parse_ts(event.get('ts', 0))
        if event_ts <= last_ctrl_ts:
            continue
        max_ctrl_ts = max(max_ctrl_ts, event_ts)

        data = event.get('data', {})
        target_device = data.get('target_device', '').upper()

        if target_device != own_short_id:
            continue  # Not for us

        action = data.get('action')
        target = data.get('target')

        if not target:
            continue

        if action == 'stop':
            initiated_by = data.get('from', 'remote')
            disable_instance(target, initiated_by=initiated_by, reason='remote')
            _log('CONTROL_RECV', f'stop {target}', from_=initiated_by)
        elif action == 'start':
            enable_instance(target)
            _log('CONTROL_RECV', f'start {target}', from_=data.get('from'))

    # Persist dedup timestamp
    if max_ctrl_ts > last_ctrl_ts:
        kv_set(f'relay_ctrl_{source_device}', str(max_ctrl_ts))


# ==================== Wait Helper ====================

def relay_wait(timeout: float = 25.0) -> bool:
    """Drop-in replacement for sync_wait(). Returns True if new data imported.

    Used by cmd_send --wait and cmd_events --wait.
    """
    # Push first (rate-limited internally)
    push()

    # Pull with long-poll
    result, _ = pull(timeout=int(min(timeout, 25)))

    return bool(result.get('devices'))


def get_relay_status() -> dict[str, Any]:
    """Get relay status for TUI display.

    Returns dict with:
        configured: bool - relay URL is set
        enabled: bool - relay is enabled (config flag)
        status: 'ok' | 'error' | None - last operation result
        error: str | None - last error message
        last_push: float - timestamp of last successful push
    """
    config = get_config()
    return {
        'configured': bool(config.relay),
        'enabled': config.relay_enabled,
        'status': kv_get('relay_status'),
        'error': kv_get('relay_last_error'),
        'last_push': float(kv_get('relay_last_push') or 0),
    }


# ==================== Public API ====================

__all__ = [
    'push',
    'pull',
    'relay_wait',
    'build_state',
    'send_control',
    'get_relay_status',
    'is_relay_enabled',
]
