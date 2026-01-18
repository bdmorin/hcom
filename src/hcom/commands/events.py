"""Events commands for HCOM"""

import sys
import json
import time
from datetime import datetime
from .utils import format_error
from ..shared import CommandContext


def _cmd_events_launch(argv: list[str], instance_name: str | None = None) -> int:
    """Wait for launches ready, output JSON. Internal - called by launch output."""
    from ..core.db import get_launch_status, get_launch_batch, init_db
    from .utils import resolve_identity, validate_flags

    # Validate flags
    if error := validate_flags("events launch", argv):
        print(format_error(error), file=sys.stderr)
        return 1

    init_db()

    # Parse batch_id arg (for specific batch lookup)
    batch_id = argv[0] if argv and not argv[0].startswith("--") else None

    # Find launcher identity if in AI tool context (Claude, Gemini, Codex)
    from ..shared import is_inside_ai_tool

    launcher = instance_name  # Use explicit instance_name if provided
    if not launcher and is_inside_ai_tool():
        try:
            launcher = resolve_identity().name
        except Exception:
            pass

    # Get status - specific batch or aggregated
    if batch_id:
        status_data = get_launch_batch(batch_id)
    else:
        status_data = get_launch_status(launcher)

    if not status_data:
        msg = "You haven't launched any instances" if launcher else "No launches found"
        print(json.dumps({"status": "no_launches", "message": msg}))
        return 0

    # Wait up to 30s for all instances to be ready
    start_time = time.time()
    while (
        status_data["ready"] < status_data["expected"] and time.time() - start_time < 30
    ):
        time.sleep(0.5)
        if batch_id:
            status_data = get_launch_batch(batch_id)
        else:
            status_data = get_launch_status(launcher)
        if not status_data:
            # DB reset or batch pruned mid-wait
            print(
                json.dumps(
                    {
                        "status": "error",
                        "message": "Launch data disappeared (DB reset or pruned)",
                    }
                )
            )
            return 1

    # Output JSON
    is_timeout = status_data["ready"] < status_data["expected"]
    status = "timeout" if is_timeout else "ready"
    result = {
        "status": status,
        "expected": status_data["expected"],
        "ready": status_data["ready"],
        "instances": status_data["instances"],
        "launcher": status_data["launcher"],
        "timestamp": status_data["timestamp"],
    }
    # Include batches list if aggregated
    if "batches" in status_data:
        result["batches"] = status_data["batches"]
    else:
        result["batch_id"] = status_data.get("batch_id")

    if is_timeout:
        result["timed_out"] = True
        # Identify which batch(es) failed
        batch_info = result.get("batch_id") or (
            result.get("batches", ["?"])[0] if result.get("batches") else "?"
        )
        result["hint"] = (
            f"Launch failed: {status_data['ready']}/{status_data['expected']} ready after 30s (batch: {batch_info}). Check ~/.hcom/.tmp/logs/background_*.log or hcom list -v"
        )
    print(json.dumps(result))

    return 0 if status == "ready" else 1


# Preset subscriptions (name -> sql) - use events_v flat fields
# File-write tool contexts by platform:
#   Claude: tool:Write, tool:Edit
#   Gemini: tool:write_file, tool:replace
#   Codex: tool:apply_patch
_FILE_WRITE_CONTEXTS = (
    "('tool:Write', 'tool:Edit', 'tool:write_file', 'tool:replace', 'tool:apply_patch')"
)

# System-wide presets (no target parameter)
PRESET_SUBSCRIPTIONS = {
    # Uses 'events_v.' prefix for outer table refs (bare names don't resolve in nested subquery)
    "collision": f"""type = 'status' AND status_context IN {_FILE_WRITE_CONTEXTS} AND EXISTS (SELECT 1 FROM events_v e WHERE e.type = 'status' AND e.status_context IN {_FILE_WRITE_CONTEXTS} AND e.status_detail = events_v.status_detail AND e.instance != events_v.instance AND ABS(strftime('%s', events_v.timestamp) - strftime('%s', e.timestamp)) < 20)""",
    # Lifecycle events for any instance
    "created": "type = 'life' AND life_action = 'created'",
    "stopped": "type = 'life' AND life_action = 'stopped'",
    "blocked": "type = 'status' AND status_val = 'blocked'",
}

# Parameterized presets: {target} replaced with instance name
# Usage: hcom events sub idle:veki, hcom events sub file_edits:nova
# Note: LIKE patterns use ESCAPE '\\' - targets must have wildcards escaped
PARAMETERIZED_PRESETS = {
    # Turn ended - instance went back to listening (finished task)
    "idle": "type = 'status' AND instance = '{target}' AND status_val = 'listening'",
    # File edits - any file write tool across Claude/Gemini/Codex
    "file_edits": f"type = 'status' AND instance = '{{target}}' AND status_context IN {_FILE_WRITE_CONTEXTS}",
    # User input - bigboss messages or user prompts to instance
    # ESCAPE '\\' ensures _ and % in names are matched literally
    "user_input": "((type = 'message' AND msg_from = 'bigboss' AND msg_delivered_to LIKE '%{target}%' ESCAPE '\\') OR (type = 'status' AND instance = '{target}' AND status_context = 'prompt'))",
    # Instance lifecycle
    "created": "type = 'life' AND instance = '{target}' AND life_action = 'created'",
    "stopped": "type = 'life' AND instance = '{target}' AND life_action = 'stopped'",
    "blocked": "type = 'status' AND instance = '{target}' AND status_val = 'blocked'",
}

# Command presets: match on status_detail (the command text)
# Usage:
#   hcom events sub cmd:"git commit"           - any instance running commands containing "git commit"
#   hcom events sub cmd:nova:"git commit"      - only instance nova
#   hcom events sub cmd-starts:"git"           - commands starting with "git"
#   hcom events sub cmd-exact:"git status"     - exact command match
# Tool contexts covered:
#   Claude: tool:Bash
#   Gemini: tool:run_shell_command
#   Codex:  tool:shell (via TranscriptWatcher)
_SHELL_TOOL_CONTEXTS = "('tool:Bash', 'tool:run_shell_command', 'tool:shell')"

COMMAND_PRESETS = {
    # Contains (default) - LIKE '%pattern%' with ESCAPE for literal matching
    "cmd": f"type = 'status' AND status_context IN {_SHELL_TOOL_CONTEXTS} AND status_detail LIKE '%{{pattern}}%' ESCAPE '\\'",
    "cmd-starts": f"type = 'status' AND status_context IN {_SHELL_TOOL_CONTEXTS} AND status_detail LIKE '{{pattern}}%' ESCAPE '\\'",
    "cmd-exact": f"type = 'status' AND status_context IN {_SHELL_TOOL_CONTEXTS} AND status_detail = '{{pattern}}'",
}


def cmd_events(argv: list[str], *, ctx: CommandContext | None = None) -> int:
    """Query events from SQLite: hcom events [launch|sub|unsub] [--last N] [--wait SEC] [--sql EXPR] [--name NAME]"""
    from ..core.db import get_db, init_db, get_last_event_id
    from .utils import parse_name_flag, validate_flags
    from ..core.identity import resolve_identity

    init_db()  # Ensure schema exists

    # Identity (instance-only): CLI supplies ctx (preferred). Direct calls may still pass --name.
    from_value = ctx.explicit_name if ctx else None
    argv_parsed = argv
    if ctx is None:
        from_value, argv_parsed = parse_name_flag(argv)

    # Resolve identity if --name provided
    # caller_name: used for subscriptions (can be external name or instance name)
    # instance_name: only set for real instances (used for message delivery)
    caller_name = None
    instance_name = None
    if ctx and ctx.identity and ctx.identity.kind == "instance":
        instance_name = ctx.identity.name
        caller_name = ctx.identity.name
    elif from_value:
        try:
            identity = resolve_identity(name=from_value)
            if identity.kind == "instance":
                instance_name = identity.name
                caller_name = identity.name
        except Exception as e:
            print(format_error(f"Cannot resolve '{from_value}': {e}"), file=sys.stderr)
            return 1

    # Handle 'launch' subcommand
    if argv_parsed and argv_parsed[0] == "launch":
        return _cmd_events_launch(argv_parsed[1:], instance_name=instance_name)

    # Handle 'sub' subcommand (list or subscribe)
    if argv_parsed and argv_parsed[0] == "sub":
        return _events_sub(argv_parsed[1:], caller_name=caller_name)

    # Handle 'unsub' subcommand (unsubscribe)
    if argv_parsed and argv_parsed[0] == "unsub":
        return _events_unsub(argv_parsed[1:], caller_name=caller_name)

    # Validate flags before parsing (use argv_parsed which has --name removed)
    if error := validate_flags("events", argv_parsed):
        print(format_error(error), file=sys.stderr)
        return 1

    # Use already-parsed values from above
    argv = argv_parsed

    # Parse arguments
    last_n = 20  # Default: last 20 events
    wait_timeout = None
    sql_where = None

    i = 0
    while i < len(argv):
        if argv[i] == "--last" and i + 1 < len(argv):
            try:
                last_n = int(argv[i + 1])
            except ValueError:
                print(
                    f"Error: --last must be an integer, got '{argv[i + 1]}'",
                    file=sys.stderr,
                )
                return 1
            i += 2
        elif argv[i] == "--wait":
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                try:
                    wait_timeout = int(argv[i + 1])
                except ValueError:
                    print(
                        f"Error: --wait must be an integer, got '{argv[i + 1]}'",
                        file=sys.stderr,
                    )
                    return 1
                i += 2
            else:
                wait_timeout = 60  # Default: 60 seconds
                i += 1
        elif argv[i] == "--sql" and i + 1 < len(argv):
            # Fix shell escaping: bash/zsh escape ! as \! in double quotes (history expansion)
            # SQLite doesn't use backslash escaping, so strip these artifacts
            sql_where = argv[i + 1].replace("\\!", "!")
            i += 2
        else:
            i += 1

    # Pull remote events for fresh data (skip if --wait mode, which has its own polling)
    if wait_timeout is None:
        try:
            from ..relay import is_relay_handled_by_tui, pull

            if not is_relay_handled_by_tui():
                pull()
        except Exception:
            pass

    # Build base query for filters
    db = get_db()
    filter_query = ""

    # Add user SQL WHERE clause directly (no validation needed)
    # Note: SQL injection is not a security concern in hcom's threat model.
    # User (or ai) owns ~/.hcom/hcom.db and can already run: sqlite3 ~/.hcom/hcom.db "anything"
    # Validation would block legitimate queries while providing no actual security.
    if sql_where:
        filter_query += f" AND ({sql_where})"

    # Wait mode: block until matching event or timeout
    if wait_timeout:
        import socket
        import select

        # Check for matching events in last 10s (race condition window)
        from datetime import timezone

        lookback_timestamp = datetime.fromtimestamp(
            time.time() - 10, tz=timezone.utc
        ).isoformat()
        lookback_query = f"SELECT * FROM events_v WHERE timestamp > ?{filter_query} ORDER BY id DESC LIMIT 1"

        try:
            lookback_row = db.execute(lookback_query, [lookback_timestamp]).fetchone()
        except Exception as e:
            print(f"Error in SQL WHERE clause: {e}", file=sys.stderr)
            return 2

        if lookback_row:
            try:
                event = {
                    "ts": lookback_row["timestamp"],
                    "type": lookback_row["type"],
                    "instance": lookback_row["instance"],
                    "data": json.loads(lookback_row["data"]),
                }
                # Found recent matching event, return immediately
                print(json.dumps(event))
                return 0
            except (json.JSONDecodeError, TypeError):
                pass  # Ignore corrupt event, continue to wait loop

        # Setup TCP notification for instant wake on local events
        notify_server = None
        notify_port = None
        if instance_name:
            try:
                notify_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                notify_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                notify_server.bind(("127.0.0.1", 0))
                notify_server.listen(128)
                notify_server.setblocking(False)
                notify_port = notify_server.getsockname()[1]
                # Register in notify_endpoints
                from ..core.db import upsert_notify_endpoint

                upsert_notify_endpoint(instance_name, "events_wait", notify_port)
            except Exception:
                if notify_server:
                    try:
                        notify_server.close()
                    except Exception:
                        pass
                notify_server = None

        start_time = time.time()
        last_id: int | str = get_last_event_id()

        try:
            while time.time() - start_time < wait_timeout:
                query = f"SELECT * FROM events_v WHERE id > ?{filter_query} ORDER BY id"

                try:
                    rows = db.execute(query, [last_id]).fetchall()
                except Exception as e:
                    print(f"Error in SQL WHERE clause: {e}", file=sys.stderr)
                    return 2

                if rows:
                    # Process matching events
                    for row in rows:
                        try:
                            event = {
                                "ts": row["timestamp"],
                                "type": row["type"],
                                "instance": row["instance"],
                                "data": json.loads(row["data"]),
                            }

                            # Event matches all conditions, print and exit
                            print(json.dumps(event))
                            return 0

                        except (json.JSONDecodeError, TypeError) as e:
                            # Skip corrupt events, log to stderr
                            print(
                                f"Warning: Skipping corrupt event ID {row['id']}: {e}",
                                file=sys.stderr,
                            )
                            continue

                    # All events processed, update last_id and continue waiting
                    last_id = rows[-1]["id"]

                # Check if current instance received messages (interrupt wait to notify)
                from .utils import resolve_identity
                from ..core.messages import get_unread_messages
                from ..pty.pty_common import build_listen_instruction

                # Use explicit instance_name if provided, otherwise auto-detect
                if instance_name:
                    check_instance = instance_name
                else:
                    try:
                        check_instance = resolve_identity().name
                    except Exception:
                        check_instance = None
                if check_instance:
                    messages, _ = get_unread_messages(
                        check_instance, update_position=False
                    )
                    if messages:
                        # Notify without marking read; delivery happens via hooks or listen
                        print(build_listen_instruction(check_instance))
                        return 0

                # Sync remote events + wait for local TCP notification
                from ..relay import relay_wait, is_relay_enabled

                remaining = wait_timeout - (time.time() - start_time)
                if remaining > 0:
                    # Short relay poll (doesn't block long), then TCP select for local wake
                    if is_relay_enabled():
                        relay_wait(min(remaining, 2))  # Short poll, don't block

                    # TCP select for instant local wake, or short sleep as fallback
                    if notify_server:
                        wait_time = min(remaining, 5.0)  # Check relay again every 5s
                        readable, _, _ = select.select(
                            [notify_server], [], [], wait_time
                        )
                        if readable:
                            # Drain pending notifications
                            while True:
                                try:
                                    notify_server.accept()[0].close()
                                except BlockingIOError:
                                    break
                    else:
                        time.sleep(0.5)

            print(json.dumps({"timed_out": True}))
            return 1
        finally:
            if notify_server:
                try:
                    notify_server.close()
                except Exception:
                    pass
                # Clean up notify endpoint from DB to prevent stale port accumulation
                if instance_name and notify_port:
                    try:
                        from ..core.db import delete_notify_endpoint

                        delete_notify_endpoint(
                            instance_name, kind="events_wait", port=notify_port
                        )
                    except Exception:
                        pass

    # Snapshot mode (default)
    query = "SELECT * FROM events_v WHERE 1=1"
    query += filter_query
    query += " ORDER BY id DESC"
    query += f" LIMIT {last_n}"

    try:
        rows = db.execute(query).fetchall()
    except Exception as e:
        print(f"Error in SQL WHERE clause: {e}", file=sys.stderr)
        return 2
    # Reverse to chronological order
    for row in reversed(rows):
        try:
            event = {
                "ts": row["timestamp"],
                "type": row["type"],
                "instance": row["instance"],
                "data": json.loads(row["data"]),
            }
            print(json.dumps(event))
        except (json.JSONDecodeError, TypeError) as e:
            # Skip corrupt events, log to stderr
            print(
                f"Warning: Skipping corrupt event ID {row['id']}: {e}", file=sys.stderr
            )
            continue
    return 0


# ==================== Event Subscriptions ====================


def _events_sub(argv: list[str], caller_name: str | None = None, silent: bool = False) -> int:
    """Subscribe to events or list subscriptions.

    hcom events sub                  - list all subscriptions
    hcom events sub "sql"            - custom SQL subscription
    hcom events sub collision        - preset: file collision warnings
    hcom events sub idle:nova        - preset: nova returns to listening
    hcom events sub cmd:"git"        - commands containing "git" (excludes self)
    hcom events sub cmd:nova:"git"   - only nova's commands containing "git"
    hcom events sub cmd-starts:"py"  - commands starting with "py"
    hcom events sub cmd-exact:"ls"   - exact command match
    hcom events sub "sql" --once     - one-shot (auto-removed after first match)
    hcom events sub "sql" --for X    - subscribe on behalf of instance X
    """
    from ..core.db import get_db, get_last_event_id, kv_set, kv_get
    from ..core.instances import load_instance_position
    from .utils import resolve_identity, validate_flags
    from hashlib import sha256

    # Validate flags
    if error := validate_flags("events sub", argv):
        print(format_error(error), file=sys.stderr)
        return 1

    # Parse args
    once = "--once" in argv
    target_instance = None
    i = 0
    sql_parts = []
    while i < len(argv):
        if argv[i] == "--once":
            i += 1
        elif argv[i] == "--for":
            if i + 1 >= len(argv):
                print("Error: --for requires name", file=sys.stderr)
                return 1
            target_instance = argv[i + 1]
            i += 2
        elif not argv[i].startswith("-"):
            sql_parts.append(argv[i])
            i += 1
        else:
            i += 1

    conn = get_db()
    now = time.time()

    # No args = list subscriptions
    if not sql_parts:
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE 'events_sub:%'"
        ).fetchall()

        if not rows:
            print("No active subscriptions")
            return 0

        subs = []
        for row in rows:
            try:
                subs.append(json.loads(row["value"]))
            except Exception:
                pass

        if not subs:
            print("No active subscriptions")
            return 0

        print(f"{'ID':<10} {'FOR':<12} {'MODE':<10} FILTER")
        for sub in subs:
            mode = "once" if sub.get("once") else "continuous"
            sql_display = (
                sub["sql"][:35] + "..." if len(sub["sql"]) > 35 else sub["sql"]
            )
            print(f"{sub['id']:<10} {sub['caller']:<12} {mode:<10} {sql_display}")

        return 0

    # Check for preset subscription (system-wide or parameterized)
    preset_arg = sql_parts[0] if len(sql_parts) == 1 else None
    if preset_arg:
        # Parse preset:target syntax (e.g., idle:veki, file_edits:nova)
        if ":" in preset_arg and not preset_arg.startswith("'"):
            preset_name, target = preset_arg.split(":", 1)
        else:
            preset_name, target = preset_arg, None

        # Check system-wide presets first (when no target provided)
        if not target and preset_name in PRESET_SUBSCRIPTIONS:
            # Resolve caller for preset key
            try:
                caller = caller_name if caller_name else resolve_identity().name
            except Exception:
                print(
                    format_error(f"Cannot enable '{preset_name}' without identity."),
                    file=sys.stderr,
                )
                print("Run 'hcom start' first, or use --name.", file=sys.stderr)
                return 1

            sub_key = f"events_sub:{preset_name}_{caller}"
            if kv_get(sub_key):
                if not silent:
                    print(f"{preset_name} already enabled")
                return 0

            kv_set(
                sub_key,
                json.dumps(
                    {
                        "id": f"{preset_name}_{caller}",
                        "caller": caller,
                        "sql": PRESET_SUBSCRIPTIONS[preset_name],
                        "created": now,
                        "last_id": get_last_event_id(),
                        "once": once,
                    }
                ),
            )
            mode_str = " (once)" if once else ""
            if not silent:
                print(f"{preset_name} enabled{mode_str}")
            return 0

        # Check parameterized presets (require target)
        if preset_name in PARAMETERIZED_PRESETS:
            if not target:
                print(
                    format_error(
                        f"Preset '{preset_name}' requires target: {preset_name}:<instance>"
                    ),
                    file=sys.stderr,
                )
                print(f"Example: hcom events sub {preset_name}:veki", file=sys.stderr)
                return 1

            # Resolve caller for notifications
            try:
                caller = caller_name if caller_name else resolve_identity().name
            except Exception:
                print(
                    format_error(f"Cannot enable '{preset_name}' without identity."),
                    file=sys.stderr,
                )
                print("Run 'hcom start' first, or use --name.", file=sys.stderr)
                return 1

            # Substitute target in SQL (escape SQL quotes and LIKE wildcards)
            # Order matters: escape backslash first, then wildcards, then quotes
            escaped_target = (
                target.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                .replace("'", "''")
            )
            sql = PARAMETERIZED_PRESETS[preset_name].format(target=escaped_target)
            sub_key = f"events_sub:{preset_name}_{target}_{caller}"
            if kv_get(sub_key):
                if not silent:
                    print(f"{preset_name}:{target} already enabled")
                return 0

            kv_set(
                sub_key,
                json.dumps(
                    {
                        "id": f"{preset_name}_{target}_{caller}",
                        "caller": caller,
                        "sql": sql,
                        "created": now,
                        "last_id": get_last_event_id(),
                        "once": once,
                    }
                ),
            )
            if not silent:
                print(f"{preset_name}:{target} enabled")
            return 0

        # Check command presets (cmd, cmd-starts, cmd-exact)
        # Syntax: cmd:"pattern" or cmd:instance:"pattern"
        if preset_name in COMMAND_PRESETS:
            # Resolve caller for notifications
            try:
                caller = caller_name if caller_name else resolve_identity().name
            except Exception:
                print(
                    format_error(f"Cannot enable '{preset_name}' without identity."),
                    file=sys.stderr,
                )
                print("Run 'hcom start' first, or use --name.", file=sys.stderr)
                return 1

            # Parse target which could be:
            #   "pattern"           -> all instances, pattern search
            #   instance:"pattern"  -> specific instance
            if not target:
                print(
                    format_error(
                        f"Preset '{preset_name}' requires pattern: {preset_name}:\"<command>\""
                    ),
                    file=sys.stderr,
                )
                print(
                    f'Example: hcom events sub {preset_name}:"git commit"',
                    file=sys.stderr,
                )
                return 1

            # Check if target contains instance:pattern (look for second colon after stripping quotes)
            instance_filter = None
            pattern = target

            # Strip surrounding quotes from pattern if present
            if (pattern.startswith('"') and pattern.endswith('"')) or (
                pattern.startswith("'") and pattern.endswith("'")
            ):
                pattern = pattern[1:-1]
            # Check for instance:pattern format (instance name won't have quotes/spaces)
            elif ":" in target:
                parts = target.split(":", 1)
                # If first part looks like instance name (no spaces, no quotes)
                if " " not in parts[0] and not parts[0].startswith(('"', "'")):
                    instance_filter = parts[0]
                    pattern = parts[1]
                    # Strip quotes from pattern
                    if (pattern.startswith('"') and pattern.endswith('"')) or (
                        pattern.startswith("'") and pattern.endswith("'")
                    ):
                        pattern = pattern[1:-1]

            # Escape for SQL: quotes and LIKE wildcards (for cmd/cmd-starts)
            # Order: backslash first, then wildcards, then quotes
            escaped_pattern = pattern
            if preset_name in ("cmd", "cmd-starts"):
                # LIKE patterns need wildcard escaping (preset has ESCAPE '\\')
                escaped_pattern = (
                    escaped_pattern.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
            escaped_pattern = escaped_pattern.replace("'", "''")

            # Build SQL from preset template
            sql = COMMAND_PRESETS[preset_name].format(pattern=escaped_pattern)

            # Add instance filter if specified, otherwise exclude caller (don't notify self)
            if instance_filter:
                escaped_instance = instance_filter.replace("'", "''")
                sql = f"({sql}) AND instance = '{escaped_instance}'"
            else:
                # Global subscription: exclude caller's own commands
                escaped_caller = caller.replace("'", "''")
                sql = f"({sql}) AND instance != '{escaped_caller}'"

            # Generate sub ID
            pattern_hash = sha256(pattern.encode()).hexdigest()[:4]
            sub_id = f"{preset_name}_{pattern_hash}_{caller}"
            if instance_filter:
                sub_id = f"{preset_name}_{instance_filter}_{pattern_hash}_{caller}"

            sub_key = f"events_sub:{sub_id}"
            if kv_get(sub_key):
                if not silent:
                    display = (
                        f"{preset_name}:{pattern}"
                        if not instance_filter
                        else f"{preset_name}:{instance_filter}:{pattern}"
                    )
                    print(f"{display} already enabled")
                return 0

            kv_set(
                sub_key,
                json.dumps(
                    {
                        "id": sub_id,
                        "caller": caller,
                        "sql": sql,
                        "created": now,
                        "last_id": get_last_event_id(),
                        "once": once,
                    }
                ),
            )

            if not silent:
                display = (
                    f"{preset_name}:{pattern}"
                    if not instance_filter
                    else f"{preset_name}:{instance_filter}:{pattern}"
                )
                print(f"{display} enabled")
                # Show what it matches
                test_count = conn.execute(
                    f"SELECT COUNT(*) FROM events_v WHERE ({sql})"
                ).fetchone()[0]
                if test_count > 0:
                    print(f"  historical matches: {test_count} events")
            return 0

    # Create custom subscription
    # Fix shell escaping: bash/zsh escape ! as \! in double quotes (history expansion)
    sql = " ".join(sql_parts).replace("\\!", "!")

    # Validate SQL syntax (use events_v for flat field access)
    try:
        conn.execute(f"SELECT 1 FROM events_v WHERE ({sql}) LIMIT 0")
    except Exception as e:
        print(f"Invalid SQL: {e}", file=sys.stderr)
        return 1

    # Resolve target (--for) or use caller's identity
    if target_instance:
        # Validate target instance exists
        target_data = load_instance_position(target_instance)
        if not target_data:
            # Try prefix match
            row = conn.execute(
                "SELECT name FROM instances WHERE name LIKE ? LIMIT 1",
                (f"{target_instance}%",),
            ).fetchone()
            if row:
                target_instance = row["name"]
                target_data = load_instance_position(target_instance)

        if not target_data:
            print(f"Not found: {target_instance}", file=sys.stderr)
            print("Use 'hcom list' to see available agents", file=sys.stderr)
            return 1

        caller = target_instance
    else:
        # Resolve caller - require explicit identity for subscriptions
        if caller_name:
            caller = caller_name
        else:
            try:
                caller = resolve_identity().name
            except Exception:
                print(
                    format_error(
                        "Cannot create subscription without identity. Run 'hcom start' first or use --name."
                    ),
                    file=sys.stderr,
                )
                return 1

    # Test against recent events to show what would match
    test_count = conn.execute(
        f"SELECT COUNT(*) FROM events_v WHERE ({sql})"
    ).fetchone()[0]

    # Generate ID
    sub_id = f"sub-{sha256(f'{caller}{sql}{now}'.encode()).hexdigest()[:4]}"

    # Store subscription
    key = f"events_sub:{sub_id}"
    value = json.dumps(
        {
            "id": sub_id,
            "sql": sql,
            "caller": caller,
            "once": once,
            "last_id": get_last_event_id(),
            "created": now,
        }
    )
    kv_set(key, value)

    # Output with validation feedback
    print(f"{sub_id}")
    print(f"  for: {caller}")
    print(f"  filter: {sql}")
    if test_count > 0:
        print(f"  historical matches: {test_count} events")
        # Show most recent match as example
        example = conn.execute(
            f"SELECT timestamp, type, instance, data FROM events_v WHERE ({sql}) ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if example:
            print(
                f"  latest match: [{example['type']}] {example['instance']} @ {example['timestamp'][:19]}"
            )
    else:
        print("  historical matches: 0 (filter will apply to future events only)")
        import re

        # Warn about = comparison on JSON array fields (common mistake)
        # These fields are arrays like ["name1","name2"], not strings
        array_fields = ["msg_delivered_to", "msg_mentions"]
        for field in array_fields:
            # Match patterns like: field='value' or field = 'value' (but not LIKE)
            if re.search(rf"\b{field}\s*=\s*['\"]", sql, re.IGNORECASE):
                print(
                    f"  Warning: {field} is a JSON array - use LIKE '%name%' not ='name'"
                )

        # Warn about json_extract paths that don't exist in recent events
        paths = re.findall(
            r"json_extract\s*\(\s*data\s*,\s*['\"](\$\.[^'\"]+)['\"]", sql
        )
        if paths:
            # Check which paths exist in recent events
            missing = []
            for path in set(paths):
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE json_extract(data, ?) IS NOT NULL LIMIT 1",
                    (path,),
                ).fetchone()
                if not exists:
                    missing.append(path)
            if missing:
                print(
                    f"  Warning: field(s) not found in any events: {', '.join(missing)} \nYou should probably double check the syntax"
                )

    return 0


def _events_unsub(argv: list[str], caller_name: str | None = None) -> int:
    """Remove subscription: hcom events unsub <id|preset|preset:target>"""
    from ..core.db import get_db, kv_set
    from .utils import resolve_identity, validate_flags

    # Validate flags
    if error := validate_flags("events unsub", argv):
        print(format_error(error), file=sys.stderr)
        return 1

    if not argv:
        print("Usage: hcom events unsub <id>", file=sys.stderr)
        return 1

    sub_id = argv[0]

    # Parse preset:target syntax (e.g., idle:veki)
    if ":" in sub_id and not sub_id.startswith("sub-"):
        preset_name, target = sub_id.split(":", 1)
    else:
        preset_name, target = sub_id, None

    # Handle system-wide preset names first (when no target)
    if not target and preset_name in PRESET_SUBSCRIPTIONS:
        try:
            caller = caller_name if caller_name else resolve_identity().name
        except Exception:
            print(
                format_error(f"Cannot disable '{sub_id}' without identity."),
                file=sys.stderr,
            )
            print("Run 'hcom start' first, or use --name.", file=sys.stderr)
            return 1
        key = f"events_sub:{preset_name}_{caller}"
        conn = get_db()
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if not row:
            print(f"{preset_name} not enabled")
            return 0
        kv_set(key, None)
        print(f"{preset_name} disabled")
        return 0

    # Handle parameterized presets (e.g., 'idle:veki' -> 'idle_veki_{caller}')
    if preset_name in PARAMETERIZED_PRESETS:
        if not target:
            print(
                format_error(
                    f"Preset '{preset_name}' requires target: {preset_name}:<instance>"
                ),
                file=sys.stderr,
            )
            return 1
        try:
            caller = caller_name if caller_name else resolve_identity().name
        except Exception:
            print(
                format_error(f"Cannot disable '{sub_id}' without identity."),
                file=sys.stderr,
            )
            print("Run 'hcom start' first, or use --name.", file=sys.stderr)
            return 1
        key = f"events_sub:{preset_name}_{target}_{caller}"
        conn = get_db()
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if not row:
            print(f"{preset_name}:{target} not enabled")
            return 0
        kv_set(key, None)
        print(f"{preset_name}:{target} disabled")
        return 0

    # Handle command presets (cmd, cmd-starts, cmd-exact)
    # Key format: events_sub:{preset}_{hash}_{caller} or events_sub:{preset}_{instance}_{hash}_{caller}
    if preset_name in COMMAND_PRESETS:
        try:
            caller = caller_name if caller_name else resolve_identity().name
        except Exception:
            print(
                format_error(f"Cannot disable '{sub_id}' without identity."),
                file=sys.stderr,
            )
            print("Run 'hcom start' first, or use --name.", file=sys.stderr)
            return 1

        conn = get_db()
        # Search for matching subscriptions (pattern in key suffix matches caller)
        # Keys are like: events_sub:cmd_abc1_caller or events_sub:cmd_instance_abc1_caller
        pattern_prefix = f"events_sub:{preset_name}_%_{caller}"
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE ?",
            (pattern_prefix.replace("_", r"\_").replace("%", "_") + "%",),
        ).fetchall()

        # Simpler: just look for keys starting with preset_name and ending with caller
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE ? AND key LIKE ?",
            (f"events_sub:{preset_name}_%", f"%_{caller}"),
        ).fetchall()

        if not rows:
            print(f"No {preset_name} subscriptions found for {caller}")
            print("Use 'hcom events sub' to list active subscriptions.")
            return 0

        if len(rows) == 1:
            kv_set(rows[0]["key"], None)
            print(f"{preset_name} subscription disabled")
            return 0

        # Multiple matches - show them and ask for specific ID
        print(f"Multiple {preset_name} subscriptions found:")
        for row in rows:
            try:
                sub = json.loads(row["value"])
                # Extract pattern from SQL for display
                sql = sub.get("sql", "")
                print(f"  {sub['id']}: {sql[:50]}...")
            except Exception:
                print(f"  {row['key']}")
        print("\nUse 'hcom events unsub <full-id>' to remove a specific one.")
        return 1

    # Handle prefix match (allow 'a3f2' instead of 'sub-a3f2')
    # But don't add sub- prefix for cmd preset IDs (they use cmd_*, cmd-starts_*, cmd-exact_*)
    is_cmd_preset_id = any(sub_id.startswith(f"{p}_") for p in COMMAND_PRESETS)
    if not sub_id.startswith("sub-") and not is_cmd_preset_id:
        sub_id = f"sub-{sub_id}"

    key = f"events_sub:{sub_id}"

    # Check exists
    conn = get_db()
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if not row:
        print(f"Not found: {sub_id}", file=sys.stderr)
        print("Use 'hcom events sub' to list active subscriptions.", file=sys.stderr)
        return 1

    kv_set(key, None)
    print(f"Removed {sub_id}")
    return 0
