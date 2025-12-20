"""SQLite event storage - unified database for messages, status, and lifecycle events"""
from __future__ import annotations
import sqlite3
import shutil
import json
import os
import threading
from typing import Any, Optional
from datetime import datetime, timezone

from .paths import hcom_path, ARCHIVE_DIR
from ..shared import parse_iso_timestamp

# Database configuration
DB_FILE = "hcom.db"
SCHEMA_VERSION = 4  # Bump when schema changes incompatibly (triggers archive + fresh DB)
_thread_local = threading.local()  # Per-thread connection storage
_write_lock = threading.Lock()  # Protect concurrent writes

# ==================== Connection Management ====================

def get_db() -> sqlite3.Connection:
    """Get thread-local database connection, creating if needed.

    Returns per-thread connection with WAL mode enabled for concurrent access.
    Each thread gets its own connection to avoid SQLite threading issues.

    Detects if DB file was deleted/replaced (e.g., by `hcom reset logs`) and
    reconnects automatically.
    """
    db_path = hcom_path(DB_FILE)

    # Check if existing connection is stale (DB file deleted or replaced)
    if hasattr(_thread_local, 'conn') and _thread_local.conn is not None:
        if hasattr(_thread_local, 'db_inode'):
            try:
                current_inode = os.stat(db_path).st_ino if db_path.exists() else None
                if current_inode != _thread_local.db_inode:
                    # DB file changed - close stale connection
                    try:
                        _thread_local.conn.close()
                    except Exception:
                        pass
                    _thread_local.conn = None
                    _thread_local.db_inode = None
            except Exception:
                pass

    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        _thread_local.conn = sqlite3.connect(str(db_path))
        _thread_local.conn.row_factory = sqlite3.Row

        # Track inode for stale connection detection
        try:
            _thread_local.db_inode = os.stat(db_path).st_ino
        except Exception:
            _thread_local.db_inode = None

        # Enable foreign key constraints (disabled by default in SQLite)
        _thread_local.conn.execute("PRAGMA foreign_keys = ON")

        # Enable WAL mode for concurrent reads/writes
        _thread_local.conn.execute("PRAGMA journal_mode=WAL")
        _thread_local.conn.execute("PRAGMA wal_autocheckpoint=1000")
        _thread_local.conn.execute("PRAGMA busy_timeout=5000")

        # Check schema version - archives and reconnects if outdated
        if not check_schema_version(_thread_local.conn):
            # Reconnect after archive (recursive call with fresh DB)
            return get_db()

        init_db(_thread_local.conn)

    return _thread_local.conn

def close_db() -> None:
    """Close thread-local database connection and clear cache.

    Idempotent - safe to call multiple times or when no connection exists.
    Only closes connection for current thread.
    """
    if hasattr(_thread_local, 'conn') and _thread_local.conn is not None:
        _thread_local.conn.close()
        _thread_local.conn = None


def get_archive_timestamp() -> str:
    """Get timestamp for archive files."""
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def archive_db(reason: str = "schema") -> str | None:
    """Archive current database and return archive path.

    Creates archive in ~/.hcom/archive/session-{timestamp}/ with:
    - hcom.db (main database)
    - hcom.db-wal (WAL file if exists)
    - hcom.db-shm (shared memory file if exists)

    Thread-safe: Uses _write_lock to prevent concurrent archive within process.
    Multi-process: No cross-process lock. Concurrent processes may create duplicate
    archives (harmless - just extra copies). Acceptable for ephemeral session data.

    Windows: If delete fails (PermissionError), returns None to signal incomplete.
    Caller should raise/fail rather than recurse.

    Args:
        reason: Why archiving (for logging) - "schema", "reset", "manual"

    Returns:
        Archive path on success, None if delete failed (Windows lock)
    """
    db_file = hcom_path(DB_FILE)
    db_wal = hcom_path(f'{DB_FILE}-wal')
    db_shm = hcom_path(f'{DB_FILE}-shm')

    with _write_lock:
        if not db_file.exists():
            return None

        # Close connection before archiving
        close_db()

        timestamp = get_archive_timestamp()
        session_archive = hcom_path(ARCHIVE_DIR, f'session-{timestamp}')
        session_archive.mkdir(parents=True, exist_ok=True)

        try:
            # Checkpoint WAL before archiving (PASSIVE mode doesn't block readers)
            temp_conn = sqlite3.connect(str(db_file))
            temp_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            temp_conn.close()
        except Exception:
            pass  # Checkpoint optional

        # Copy all DB files to archive (copy before delete - archive safe even if delete fails)
        shutil.copy2(db_file, session_archive / DB_FILE)
        if db_wal.exists():
            shutil.copy2(db_wal, session_archive / f'{DB_FILE}-wal')
        if db_shm.exists():
            shutil.copy2(db_shm, session_archive / f'{DB_FILE}-shm')

        # Delete main DB and WAL/SHM files
        # On Windows, may fail if other process has handle
        try:
            db_file.unlink()
            db_wal.unlink(missing_ok=True)
            db_shm.unlink(missing_ok=True)
        except PermissionError:
            # Can't delete - return None to signal incomplete archive
            # Prevents recursive get_db() spin. Next separate process will handle it.
            import sys
            print(f"hcom: Archived to {session_archive} (DB locked, delete pending)", file=sys.stderr)
            return None

        return str(session_archive)


def check_schema_version(conn: sqlite3.Connection) -> bool:
    """Check if DB schema version matches current. Returns True if compatible.

    Checks both:
    1. user_version pragma matches SCHEMA_VERSION
    2. Required tables exist (events, instances, kv)

    If incompatible or tables missing, archives the DB and returns False.

    Special case: user_version=0 with existing tables = pre-versioned DB, needs archive.
    user_version=0 with no tables = fresh DB, safe to initialize.
    """
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        version = 0

    # Check what tables exist
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except Exception:
        tables = set()

    required = {'events', 'instances', 'kv'}

    if version == 0:
        # Fresh DB (no tables) - safe to initialize
        if not tables:
            return True
        # Pre-versioned DB with tables - archive it (incompatible schema)
        if tables.intersection(required):
            import sys
            print("hcom: Pre-versioned DB found, archiving...", file=sys.stderr)
            conn.close()
            _thread_local.conn = None
            archive_path = archive_db("schema")
            if archive_path:
                print(f"hcom: Archived to {archive_path}", file=sys.stderr)
                print("       Query with: hcom archive 1", file=sys.stderr)
                log_reset_event()  # Log to fresh DB
                return False
            else:
                raise RuntimeError("hcom: DB locked by another process. Close other hcom instances and retry.")
        # Has tables but not ours - fresh enough
        return True

    # Check version matches
    if version != SCHEMA_VERSION:
        import sys
        print(f"hcom: DB schema v{version} → v{SCHEMA_VERSION}, archiving old data...", file=sys.stderr)
        conn.close()
        _thread_local.conn = None
        archive_path = archive_db("schema")
        if archive_path:
            print(f"hcom: Archived to {archive_path}", file=sys.stderr)
            print("       Query with: hcom archive 1", file=sys.stderr)
            log_reset_event()  # Log to fresh DB
            return False
        else:
            # archive_db returned None = couldn't delete (Windows lock)
            # Raise to prevent spin; user should close other hcom processes
            raise RuntimeError("hcom: DB locked by another process. Close other hcom instances and retry.")

    # Verify required tables exist (catches corruption/partial states)
    if not required.issubset(tables):
        import sys
        missing = required - tables
        print(f"hcom: DB missing tables {missing}, archiving...", file=sys.stderr)
        conn.close()
        _thread_local.conn = None
        archive_path = archive_db("corruption")
        if archive_path:
            print(f"hcom: Archived to {archive_path}", file=sys.stderr)
            log_reset_event()  # Log to fresh DB
            return False
        else:
            raise RuntimeError("hcom: DB locked by another process. Close other hcom instances and retry.")

    return True


# ==================== Schema Management ====================

def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    """Create database schema if not exists. Idempotent.

    Schema versioning: check_schema_version() handles incompatible changes by archiving.
    No ALTER TABLE migrations - bump SCHEMA_VERSION instead for clean slate.

    Schema:
        events(id, timestamp, type, instance, data)
        instances(name, session_id, parent_session_id, ...)
        mapid_sessions(mapid, session_id, updated_at)
        kv(key, value)
    """
    if conn is None:
        conn = get_db()

    # Create events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL,
            instance TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)

    # Create instances table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instances (
            name TEXT PRIMARY KEY,
            session_id TEXT UNIQUE,
            mapid TEXT,
            parent_session_id TEXT,
            parent_name TEXT,
            tag TEXT,
            last_event_id INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0,
            status TEXT DEFAULT 'unknown',
            status_time INTEGER DEFAULT 0,
            status_context TEXT DEFAULT '',
            status_detail TEXT DEFAULT '',
            last_stop INTEGER DEFAULT 0,
            directory TEXT,
            created_at REAL NOT NULL,
            transcript_path TEXT DEFAULT '',
            tcp_mode INTEGER DEFAULT 0,
            wait_timeout INTEGER DEFAULT 1800,
            notify_port INTEGER,
            background INTEGER DEFAULT 0,
            background_log_file TEXT DEFAULT '',
            name_announced INTEGER DEFAULT 0,
            launch_context_announced INTEGER DEFAULT 0,
            stop_pending INTEGER DEFAULT 0,
            stop_notified INTEGER DEFAULT 0,
            session_ended INTEGER DEFAULT 0,
            agent_id TEXT UNIQUE,
            running_tasks TEXT DEFAULT '',
            origin_device_id TEXT DEFAULT '',
            hints TEXT DEFAULT '',
            subagent_timeout INTEGER,
            FOREIGN KEY (parent_session_id) REFERENCES instances(session_id) ON DELETE SET NULL
        )
    """)

    # Soft migration: add columns if they don't exist (for existing DBs)
    try:
        conn.execute("ALTER TABLE instances ADD COLUMN hints TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE instances ADD COLUMN subagent_timeout INTEGER")
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Create MAPID → session_id mapping table for Windows identity resolution
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mapid_sessions (
            mapid TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)

    # KV table for relay state and config
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    # Create indexes for common query patterns
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON events(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_instance ON events(instance)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_type_instance ON events(type, instance)")

    # Create instance indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON instances(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parent_session_id ON instances(parent_session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parent_name ON instances(parent_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enabled ON instances(enabled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON instances(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON instances(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mapid ON instances(mapid) WHERE mapid != ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_id_unique ON instances(agent_id) WHERE agent_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_origin ON instances(origin_device_id)")

    # Create mapid_sessions index
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mapid_sessions_updated ON mapid_sessions(updated_at DESC)")

    # Create flattened events view for simpler SQL queries
    # DROP first to ensure schema changes are applied (CREATE VIEW IF NOT EXISTS won't update)
    conn.execute("DROP VIEW IF EXISTS events_v")
    conn.execute("""
        CREATE VIEW IF NOT EXISTS events_v AS
        SELECT
            id, timestamp, type, instance, data,
            -- message fields
            json_extract(data, '$.from') as msg_from,
            json_extract(data, '$.text') as msg_text,
            json_extract(data, '$.scope') as msg_scope,
            json_extract(data, '$.sender_kind') as msg_sender_kind,
            json_extract(data, '$.delivered_to') as msg_delivered_to,
            json_extract(data, '$.mentions') as msg_mentions,
            -- message envelope fields
            json_extract(data, '$.intent') as msg_intent,
            json_extract(data, '$.thread') as msg_thread,
            json_extract(data, '$.reply_to') as msg_reply_to,
            json_extract(data, '$.reply_to_local') as msg_reply_to_local,
            -- status fields
            json_extract(data, '$.status') as status_val,
            json_extract(data, '$.context') as status_context,
            json_extract(data, '$.detail') as status_detail,
            -- life fields
            json_extract(data, '$.action') as life_action,
            json_extract(data, '$.by') as life_by,
            json_extract(data, '$.batch_id') as life_batch_id,
            json_extract(data, '$.reason') as life_reason
        FROM events
    """)

    # Set schema version
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()

# ==================== Event Operations ====================

def log_event(
    event_type: str,
    instance: str,
    data: dict[str, Any],
    timestamp: Optional[str] = None
) -> int:
    """Insert event and return its ID.

    Thread-safe: Uses lock to protect concurrent writes.

    Args:
        event_type: Event type ('message', 'status', 'life')
        instance: Instance name (sender for messages, subject for status/life)
        data: Type-specific event data
        timestamp: Optional ISO 8601 timestamp (defaults to now)

    Returns:
        Event ID (autoincrement primary key)
    """
    conn = get_db()
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    with _write_lock:
        cursor = conn.execute(
            "INSERT INTO events (timestamp, type, instance, data) VALUES (?, ?, ?, ?)",
            (timestamp, event_type, instance, json.dumps(data))
        )
        conn.commit()
        event_id = cursor.lastrowid

    # Check event subscriptions (inline, no daemon)
    _check_event_subscriptions(event_id, event_type, instance, data, timestamp)

    # Sync export moved to hook dispatcher (rate-limited, builds from SQLite)
    # No duplicate JSONL append needed - single source of truth
    return event_id


def log_reset_event() -> None:
    """Log _device reset event + set relay timestamp. Call after any DB archive/reset."""
    import time
    from .device import get_device_uuid

    log_event('life', '_device', {'action': 'reset', 'device': get_device_uuid()})
    kv_set('relay_local_reset_ts', str(time.time()))


def get_events_since(
    last_event_id: int = 0,
    event_type: Optional[str] = None,
    instance: Optional[str] = None
) -> list[dict[str, Any]]:
    """Query events by ID position with optional filters.

    Args:
        last_event_id: Return events with ID > this value (0 = all events)
        event_type: Optional filter by event type
        instance: Optional filter by instance

    Returns:
        List of events ordered by ID, each with: id, timestamp, type, instance, data (parsed JSON)
    """
    conn = get_db()

    query = "SELECT id, timestamp, type, instance, data FROM events WHERE id > ?"
    params: list[Any] = [last_event_id]

    if event_type is not None:
        query += " AND type = ?"
        params.append(event_type)

    if instance is not None:
        query += " AND instance = ?"
        params.append(instance)

    query += " ORDER BY id"

    cursor = conn.execute(query, params)
    return [
        {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "type": row["type"],
            "instance": row["instance"],
            "data": json.loads(row["data"])
        }
        for row in cursor.fetchall()
    ]

def get_last_event_id() -> int:
    """Get current maximum event ID.

    Returns:
        Maximum event ID, or 0 if no events exist
    """
    conn = get_db()
    cursor = conn.execute("SELECT MAX(id) FROM events")
    result = cursor.fetchone()[0]
    return result if result is not None else 0


def get_last_stop_event(instance_name: str) -> dict[str, Any] | None:
    """Get the last stop event for an instance.

    Returns:
        Dict with 'stopped_by' and 'reason', or None if no stop event found
    """
    conn = get_db()
    row = conn.execute("""
        SELECT json_extract(data, '$.by') as stopped_by,
               json_extract(data, '$.reason') as reason
        FROM events
        WHERE instance = ? AND type = 'life'
          AND json_extract(data, '$.action') = 'stopped'
        ORDER BY id DESC LIMIT 1
    """, (instance_name,)).fetchone()
    return dict(row) if row else None


# ==================== Instance Operations ====================

def get_instance(name: str) -> dict[str, Any] | None:
    """Get instance by name. Returns dict or None."""
    conn = get_db()
    row = conn.execute("SELECT * FROM instances WHERE name = ?", (name,)).fetchone()
    if not row:
        return None

    # Convert Row to dict
    return dict(row)

def save_instance(name: str, data: dict[str, Any]) -> bool:
    """Insert or update instance using UPSERT. Returns True on success."""
    conn = get_db()

    try:
        with _write_lock:
            data_copy = data.copy()

            # UPSERT - simpler and race-free
            columns = ', '.join(data_copy.keys())
            placeholders = ', '.join('?' * len(data_copy))
            update_clause = ', '.join(f"{k} = excluded.{k}" for k in data_copy.keys() if k != 'name')

            conn.execute(
                f"""
                INSERT INTO instances ({columns}) VALUES ({placeholders})
                ON CONFLICT(name) DO UPDATE SET {update_clause}
                """,
                tuple(data_copy.values())
            )

            conn.commit()
            return True
    except sqlite3.Error as e:
        import sys
        print(f"DB error saving instance {name}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        import sys
        print(f"Unexpected error saving instance {name}: {e}", file=sys.stderr)
        return False

def update_instance(name: str, updates: dict[str, Any]) -> bool:
    """Update specific instance fields. Returns True on success."""
    if not updates:
        return True

    conn = get_db()

    try:
        with _write_lock:
            updates_copy = updates.copy()

            # Simple UPDATE
            set_clause = ', '.join(f"{k} = ?" for k in updates_copy.keys())
            conn.execute(
                f"UPDATE instances SET {set_clause} WHERE name = ?",
                (*updates_copy.values(), name)
            )

            conn.commit()
            return True
    except sqlite3.Error as e:
        import sys
        print(f"DB error updating instance {name}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        import sys
        print(f"Unexpected error updating instance {name}: {e}", file=sys.stderr)
        return False

def delete_instance(name: str) -> bool:
    """Delete instance from database. Returns True on success."""
    conn = get_db()

    try:
        with _write_lock:
            conn.execute("DELETE FROM instances WHERE name = ?", (name,))
            conn.commit()
            return True
    except sqlite3.Error as e:
        import sys
        print(f"DB error deleting instance {name}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        import sys
        print(f"Unexpected error deleting instance {name}: {e}", file=sys.stderr)
        return False

def find_instance_by_session(session_id: str) -> str | None:
    """Find instance name by session_id. Returns name or None.

    Note: Vanilla instances have session_id=NULL, not matched by this lookup.
    """
    if not session_id:
        return None  # Don't match empty/None session_id

    conn = get_db()
    row = conn.execute("SELECT name FROM instances WHERE session_id = ?", (session_id,)).fetchone()
    return row['name'] if row else None

def get_instance_by_mapid(mapid: str) -> dict[str, Any] | None:
    """Get instance by mapid (launch token or WT_SESSION). Returns latest if multiple match.

    Note: Multiple instances can share same mapid (terminal reused for new conversations).
    Returns latest by created_at.
    """
    if not mapid:
        return None

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM instances WHERE mapid = ? ORDER BY created_at DESC LIMIT 1",
        (mapid,)
    ).fetchone()

    if not row:
        return None

    # Convert Row to dict
    return dict(row)

# ==================== High-Level Query Helpers ====================

def iter_instances(enabled_only: bool = False):
    """Iterate instances efficiently (generator, not giant dict)."""
    conn = get_db()
    query = "SELECT * FROM instances"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY created_at DESC"

    for row in conn.execute(query):
        yield dict(row)

# ==================== Launch Batch Queries ====================

LAUNCH_TIMEOUT_SECONDS = 30  # Timeout for batch to become ready

def get_launch_status(launcher: str | None = None) -> dict | None:
    """Get aggregated launch status across all pending/recent batches.

    Priority:
    1. All pending batches (ready < expected) by launcher
    2. All batches from last 60s by launcher
    3. Most recent batch by launcher
    4. None

    Returns:
        Dict with: expected, ready, instances, batches (list), launcher
        Or None if no launches found.
    """
    conn = get_db()
    from datetime import datetime, timezone

    launcher_filter = "AND e.instance = ?" if launcher else ""
    params = (launcher,) if launcher else ()

    # Get all launch events by this launcher
    launches = conn.execute(f"""
        SELECT e.timestamp, e.instance as launcher,
               json_extract(e.data, '$.batch_id') as batch_id,
               json_extract(e.data, '$.launched') as expected
        FROM events e
        WHERE e.type = 'life'
          AND json_extract(e.data, '$.action') = 'launched'
          {launcher_filter}
        ORDER BY e.id DESC
        LIMIT 20
    """, params).fetchall()

    if not launches:
        return None

    # Get ready counts per batch
    def get_ready(batch_id: str) -> tuple[int, list[str]]:
        rows = conn.execute("""
            SELECT instance FROM events
            WHERE type = 'life'
              AND json_extract(data, '$.action') = 'ready'
              AND json_extract(data, '$.batch_id') = ?
        """, (batch_id,)).fetchall()
        return len(rows), [r['instance'] for r in rows]

    # Build batch info with ready counts
    batches = []
    for row in launches:
        ready_count, ready_instances = get_ready(row['batch_id'])
        batches.append({
            'batch_id': row['batch_id'],
            'launcher': row['launcher'],
            'expected': row['expected'] or 0,
            'ready': ready_count,
            'instances': ready_instances,
            'timestamp': row['timestamp'],
            'pending': ready_count < (row['expected'] or 0),
        })

    # Use provided launcher or get from first batch
    effective_launcher = launcher or batches[0]['launcher']

    # Cutoff for "recent" - successful launch should be ready by then
    cutoff = datetime.now(timezone.utc).timestamp() - LAUNCH_TIMEOUT_SECONDS

    # Priority 1: pending batches (incomplete AND recent - timeout stuck launches)
    pending = []
    for b in batches:
        if b['pending']:
            dt = parse_iso_timestamp(b['timestamp'])
            if dt and dt.timestamp() > cutoff:
                pending.append(b)
    if pending:
        return _aggregate_batches(pending, effective_launcher)

    # Priority 2: batches from last 60s
    recent = []
    for b in batches:
        dt = parse_iso_timestamp(b['timestamp'])
        if dt and dt.timestamp() > cutoff:
            recent.append(b)
    if recent:
        return _aggregate_batches(recent, effective_launcher)

    # Priority 3: most recent
    return _aggregate_batches([batches[0]], effective_launcher)


def _aggregate_batches(batches: list[dict], launcher: str) -> dict:
    """Aggregate multiple batches into single status."""
    total_expected = sum(b['expected'] for b in batches)
    total_ready = sum(b['ready'] for b in batches)
    all_instances = []
    for b in batches:
        all_instances.extend(b['instances'])

    return {
        'expected': total_expected,
        'ready': total_ready,
        'instances': all_instances,
        'batches': [b['batch_id'] for b in batches],
        'launcher': launcher,
        'timestamp': batches[0]['timestamp'],  # most recent
    }


def get_launch_batch(batch_id: str) -> dict | None:
    """Get single batch status by ID prefix."""
    conn = get_db()

    row = conn.execute("""
        SELECT e.timestamp, e.instance as launcher,
               json_extract(e.data, '$.batch_id') as batch_id,
               json_extract(e.data, '$.launched') as expected
        FROM events e
        WHERE e.type = 'life'
          AND json_extract(e.data, '$.action') = 'launched'
          AND json_extract(e.data, '$.batch_id') LIKE ?
        ORDER BY e.id DESC LIMIT 1
    """, (f'{batch_id}%',)).fetchone()

    if not row:
        return None

    ready_rows = conn.execute("""
        SELECT instance FROM events
        WHERE type = 'life'
          AND json_extract(data, '$.action') = 'ready'
          AND json_extract(data, '$.batch_id') = ?
    """, (row['batch_id'],)).fetchall()

    return {
        'batch_id': row['batch_id'],
        'expected': row['expected'] or 0,
        'ready': len(ready_rows),
        'instances': [r['instance'] for r in ready_rows],
        'launcher': row['launcher'],
        'timestamp': row['timestamp'],
    }

# ==================== KV Store ====================

def kv_get(key: str) -> str | None:
    """Get value from kv table."""
    conn = get_db()
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else None

def kv_set(key: str, value: str | None) -> None:
    """Set or delete value in kv table."""
    conn = get_db()
    with _write_lock:
        if value is None:
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                (key, value)
            )
        conn.commit()


# ==================== Event Subscriptions ====================


def _log_sub_error(sub_id: str, error: str, exc: Exception | None = None) -> None:
    """Log subscription errors to hooks.log."""
    from .paths import hcom_path, LOGS_DIR
    import traceback
    try:
        log_file = hcom_path(LOGS_DIR) / "hooks.log"
        ts = datetime.now(timezone.utc).isoformat()
        msg = f"{ts}|sub:{sub_id}|{error}"
        if exc:
            tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            msg += f": {exc}\n{tb}"
        with open(log_file, 'a') as f:
            f.write(msg + "\n")
    except (OSError, PermissionError):
        pass


def _format_sub_notification(sub_id: str, event_id: int, event_type: str, instance: str, data: dict[str, Any]) -> str:
    """Format event notification - concise pipe-delimited for readability."""
    # Preset-specific prefixes
    if sub_id.startswith('collision'):
        prefix = "COLLISION WARNING: "
    else:
        prefix = ""

    parts = [f"[sub:{sub_id}]", f"#{event_id}", event_type, instance]

    if event_type == 'message':
        text = data.get('text', '')
        if len(text) > 60:
            text = text[:57] + '...'
        parts.append(f"from:{data.get('from', '?')}")
        parts.append(f'"{text}"')
    elif event_type == 'status':
        parts.append(data.get('status', '?'))
        if ctx := data.get('context', ''):
            parts.append(ctx)
        if detail := data.get('detail', ''):
            if len(detail) > 40:
                # Commands: keep start (see what command)
                # Files: keep end (see filename)
                if ctx and 'Bash' in ctx:
                    detail = detail[:37] + '...'
                else:
                    detail = '...' + detail[-37:]
            parts.append(detail)
    elif event_type == 'life':
        parts.append(data.get('action', '?'))
        if by := data.get('by', ''):
            parts.append(f"by:{by}")

    return prefix + ' | '.join(parts)


def _check_event_subscriptions(
    event_id: int,
    event_type: str,
    instance: str,
    data: dict[str, Any],
    timestamp: str
) -> None:
    """Check subscriptions and send matching events to subscribers.

    Called inline from log_event(). Errors logged to hooks.log.
    """
    # Recursion guard: skip system messages (prevents notification loops)
    if instance.startswith('sys_'):
        return

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE 'events_sub:%'"
        ).fetchall()
    except Exception as e:
        _log_sub_error('*', 'query failed', e)
        return

    if not rows:
        return

    for row in rows:
        sub_id = '?'
        try:
            sub = json.loads(row['value'])
            sub_id = sub.get('id', row['key'])

            # Skip already processed
            if event_id <= sub.get('last_id', 0):
                continue

            # Check SQL filter
            sql = sub.get('sql', '')
            if sql:
                try:
                    match = conn.execute(
                        f"SELECT 1 FROM events_v WHERE id = ? AND ({sql})",
                        (event_id,)
                    ).fetchone()
                except Exception as e:
                    _log_sub_error(sub_id, f'SQL error: {sql[:50]}', e)
                    continue
                if not match:
                    continue

            # Match - send notification
            caller = sub.get('caller', '')
            if not caller:
                _log_sub_error(sub_id, 'no caller')
                continue

            notification = _format_sub_notification(sub_id, event_id, event_type, instance, data)
            if not _send_sub_notification(caller, notification):
                _log_sub_error(sub_id, f'notify {caller} failed')

            # Update last_id or remove if --once
            if sub.get('once'):
                kv_set(row['key'], None)
            else:
                sub['last_id'] = event_id
                kv_set(row['key'], json.dumps(sub))

        except json.JSONDecodeError as e:
            _log_sub_error(sub_id, 'corrupt JSON', e)
        except Exception as e:
            _log_sub_error(sub_id, 'unexpected error', e)


def _send_sub_notification(caller: str, message: str) -> bool:
    """Send subscription notification. Returns True on success."""
    from .messages import send_system_message
    try:
        result = send_system_message('[hcom-events]', f"@{caller} {message}")
        return result is not None
    except Exception:
        return False


__all__ = [
    # Database management
    'get_db',
    'close_db',
    'init_db',
    'archive_db',
    'check_schema_version',
    'DB_FILE',
    'SCHEMA_VERSION',
    # Events
    'log_event',
    'log_reset_event',
    'get_events_since',
    'get_last_event_id',
    # Instances (low-level)
    'get_instance',
    'save_instance',
    'update_instance',
    'find_instance_by_session',
    'get_instance_by_mapid',
    # Instances (high-level queries)
    'iter_instances',
    # Launch batch
    'LAUNCH_TIMEOUT_SECONDS',
    'get_launch_status',
    'get_launch_batch',
    # KV store
    'kv_get',
    'kv_set',
]
