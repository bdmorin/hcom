"""Lifecycle commands for HCOM instances"""
import os
import sys
import time
from pathlib import Path
from .utils import CLIError, format_error, is_interactive, resolve_identity, validate_flags
from ..shared import FG_YELLOW, RESET, IS_WINDOWS
from ..claude_args import resolve_claude_args, merge_claude_args, add_background_defaults, validate_conflicts
from ..core.config import get_config
from ..core.paths import hcom_path
from ..core.instances import (
    load_instance_position,
    update_instance_position,
    is_subagent_instance,
    SKIP_HISTORY,
    parse_running_tasks,
)
from ..hooks.subagent import in_subagent_context
from ..core.db import iter_instances
from ..hooks.utils import disable_instance


def cmd_launch(argv: list[str]) -> int:
    """Launch Claude instances: hcom [N] [claude] [args]"""
    from ..core.ops import op_launch
    from ..shared import HcomError

    try:
        # Parse arguments: hcom [N] [claude] [args]
        count = 1

        # Extract count if first arg is digit
        if argv and argv[0].isdigit():
            count = int(argv[0])
            if count <= 0:
                raise CLIError('Count must be positive.')
            if count > 100:
                raise CLIError('Too many instances requested (max 100).')
            argv = argv[1:]

        # Skip 'claude' keyword if present
        if argv and argv[0] == 'claude':
            argv = argv[1:]

        # Forward all remaining args to claude CLI
        forwarded = argv

        # Check for --no-auto-watch flag (used by TUI to prevent opening another watch window)
        no_auto_watch = '--no-auto-watch' in forwarded
        if no_auto_watch:
            forwarded = [arg for arg in forwarded if arg != '--no-auto-watch']

        # Get tag from config
        tag = get_config().tag

        # Phase 1: Parse and merge Claude args (env + CLI with CLI precedence)
        env_spec = resolve_claude_args(None, get_config().claude_args)
        cli_spec = resolve_claude_args(forwarded if forwarded else None, None)

        # Merge: CLI overrides env on per-flag basis, inherits env if CLI has no args
        if cli_spec.clean_tokens or cli_spec.positional_tokens or cli_spec.system_entries:
            spec = merge_claude_args(env_spec, cli_spec)
        else:
            spec = env_spec

        # Validate parsed args
        if spec.has_errors():
            raise CLIError('\n'.join(spec.errors))

        # Check for conflicts (warnings only, not errors)
        warnings = validate_conflicts(spec)
        for warning in warnings:
            print(f"{FG_YELLOW}Warning:{RESET} {warning}", file=sys.stderr)

        # Add HCOM background mode enhancements
        spec = add_background_defaults(spec)

        # Extract values from spec
        background = spec.is_background
        claude_args = spec.rebuild_tokens(include_system=True)

        # Resolve launcher identity
        launcher = resolve_identity().name
        launcher_data = load_instance_position(launcher)
        launcher_enabled = launcher_data.get('enabled', False) if launcher_data else False

        # Call op_launch
        result = op_launch(
            count,
            claude_args,
            launcher=launcher,
            tag=tag,
            background=background,
            cwd=os.getcwd(),
        )

        launched = result["launched"]
        failed = result["failed"]
        batch_id = result["batch_id"]

        # Print background log files
        for log_file in result.get("log_files", []):
            print(f"Headless instance launched, log: {log_file}")

        # Show results
        if failed > 0:
            print(f"Launched {launched}/{count} Claude instance{'s' if count != 1 else ''} ({failed} failed)")
        else:
            print(f"Launched {launched} Claude instance{'s' if launched != 1 else ''}")

        print(f"Batch id: {batch_id}")
        print(f"Check status of launch + block until instance{'s are' if launched != 1 else 'is'} ready: hcom events launch")

        # Auto-launch watch dashboard if in new window mode and all instances launched successfully
        terminal_mode = get_config().terminal

        if terminal_mode not in ('here', 'print') and failed == 0 and is_interactive() and not no_auto_watch:
            if tag:
                print(f"\n  • Send to {tag} team: hcom send '@{tag}- message'")

            print("\nOpening hcom UI...")
            time.sleep(2)

            from ..ui import run_tui
            return run_tui(hcom_path())
        else:
            tips = []
            if tag:
                tips.append(f"Send to {tag} team: hcom send '@{tag}- message'")

            if launched > 0:
                is_claude_code = os.environ.get('CLAUDECODE') == '1'
                if is_claude_code:
                    if launcher_enabled:
                        tips.append(f"You'll be automatically notified when all {launched} instances are launched & ready")
                    else:
                        tips.append("Run 'hcom start' to receive automatic notifications/messages from instances")
                else:
                    tips.append("Check status: hcom list")

            if tips:
                print("\n" + "\n".join(f"  • {tip}" for tip in tips) + "\n")

            return 0

    except (CLIError, HcomError) as e:
        print(format_error(str(e)), file=sys.stderr)
        return 1
    except Exception as e:
        print(format_error(str(e)), file=sys.stderr)
        return 1


def cmd_stop(argv: list[str]) -> int:
    """Stop instances: hcom stop [name|all]"""

    # Validate flags
    if error := validate_flags('stop', argv):
        print(format_error(error), file=sys.stderr)
        return 1

    # Remove flags to get target
    args_without_flags = [a for a in argv if not a.startswith('--')]
    target = args_without_flags[0] if args_without_flags else None

    # Handle 'all' target
    if target == 'all':
        # Only stop local instances (not remote ones from other devices)
        instances = [i for i in iter_instances() if not i.get('origin_device_id')]

        if not instances:
            print("No instances found")
            return 0

        stopped_count = 0
        bg_logs = []
        stopped_names = []
        for instance_data in instances:
            if instance_data.get('enabled', False):
                instance_name = instance_data['name']
                launcher = resolve_identity().name
                disable_instance(instance_name, initiated_by=launcher, reason='stop_all')
                stopped_names.append(instance_name)
                stopped_count += 1

                # Track background logs
                if instance_data.get('background'):
                    log_file = instance_data.get('background_log_file', '')
                    if log_file:
                        bg_logs.append((instance_name, log_file))

        if stopped_count == 0:
            print("No instances to stop")
        else:
            print(f"Stopped {stopped_count} instance(s): {', '.join(stopped_names)}")

            # Show background logs if any
            if bg_logs:
                print()
                print("Headless instance logs:")
                for name, log_file in bg_logs:
                    print(f"  {name}: {log_file}")

        return 0

    # Resolve identity (target overrides automatic resolution)
    if target:
        instance_name = target
    else:
        try:
            identity = resolve_identity()
            instance_name = identity.name

            # Block subagents from stopping their parent
            if in_subagent_context(instance_name):
                raise CLIError("Cannot run hcom stop from within a Task subagent")
        except ValueError:
            instance_name = None

    # Handle SENDER (not real instance) - cake is real! sponge cake!
    from ..shared import SENDER
    if instance_name == SENDER:
        if IS_WINDOWS:
            raise CLIError("Cannot resolve instance identity - use 'hcom <n>' or Windows Terminal for stable identity")
        else:
            raise CLIError("Cannot resolve instance identity - launch via 'hcom <n>' for stable identity")

    # Error handling
    if not instance_name:
        raise CLIError("Cannot determine instance identity\nUsage: hcom stop <name> | hcom stop all | prompt Claude to run 'hcom stop'")

    position = load_instance_position(instance_name)
    if not position:
        raise CLIError(f"Instance '{instance_name}' not found")

    # Remote instance - send control via relay
    if position.get('origin_device_id'):
        if ':' in instance_name:
            name, device_short_id = instance_name.rsplit(':', 1)
            from ..relay import send_control
            if send_control('stop', name, device_short_id):
                print(f"Stop sent to {instance_name}")
                return 0
            else:
                raise CLIError(f"Failed to send stop to {instance_name} - relay unavailable")
        raise CLIError(f"Cannot stop remote instance '{instance_name}' - missing device suffix")

    # Skip already stopped instances
    if not position.get('enabled', False):
        print(f"hcom already stopped for {instance_name}")
        return 0

    # Check if this is a subagent - disable only the targeted one
    if is_subagent_instance(position):
        # External stop = CLI user specified target, Self stop = no target (uses session_id)
        is_external_stop = target is not None
        launcher = resolve_identity().name
        reason = 'external' if is_external_stop else 'manual'
        disable_instance(instance_name, initiated_by=launcher, reason=reason)
        print(f"Stopped hcom for subagent {instance_name}. Will no longer receive chat messages automatically.")
    else:
        # Regular parent instance
        # External stop = CLI user specified target, Self stop = no target (uses session_id)
        is_external_stop = target is not None
        launcher = resolve_identity().name
        reason = 'external' if is_external_stop else 'manual'
        disable_instance(instance_name, initiated_by=launcher, reason=reason)
        print(f"Stopped hcom for {instance_name}. Will no longer receive chat messages automatically.")

    # Show background log location if applicable
    if position.get('background'):
        log_file = position.get('background_log_file', '')
        if log_file:
            print(f"\nHeadless instance log: {log_file}")

    return 0


def cmd_start(argv: list[str]) -> int:
    """Enable HCOM participation: hcom start [name]"""
    from ..core.instances import initialize_instance_in_position_file, enable_instance, set_status

    # Validate flags before parsing
    if error := validate_flags('start', argv):
        print(format_error(error), file=sys.stderr)
        return 1

    # Extract --agentid flag (for subagents)
    agent_id = None

    # Parse subagent flag
    i = 0
    while i < len(argv):
        if argv[i] == '--agentid' and i + 1 < len(argv):
            agent_id = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
        else:
            i += 1

    # SUBAGENT PATH: --agentid provided (lazy creation)
    # Skip if --agentid parent (parent uses normal path below)
    if agent_id and agent_id != 'parent':
        # Resolve parent from identity (session_id or mapid)
        try:
            parent_identity = resolve_identity()
            parent_name = parent_identity.name
        except ValueError:
            print("Error: Cannot resolve parent identity", file=sys.stderr)
            return 1

        parent_session_id = parent_identity.session_id

        # Validate parent has session_id (required for FK relationship)
        if parent_session_id is None:
            print("Error: Parent instance has no session_id (required for subagent creation)", file=sys.stderr)
            return 1

        # Look up agent_type from parent's running_tasks
        parent_data = load_instance_position(parent_name)
        if not parent_data:
            print("Error: Parent instance not found", file=sys.stderr)
            return 1

        running_tasks = parse_running_tasks(parent_data.get('running_tasks', ''))
        subagents = running_tasks.get('subagents', [])

        # Find agent_type for this agent_id
        agent_type = None
        for task in subagents:
            if task.get('agent_id') == agent_id:
                agent_type = task.get('type')
                break

        if not agent_type:
            print(f"Error: agent_id {agent_id} not found in parent's running_tasks.subagents", file=sys.stderr)
            return 1

        # Check if instance already exists by agent_id (reuse name)
        from ..core.db import get_db
        import sqlite3
        import re
        conn = get_db()
        existing = conn.execute(
            "SELECT name FROM instances WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()

        if existing:
            # Already created - reuse existing name, re-enable if stopped
            subagent_name = existing['name']
            instance_data = load_instance_position(subagent_name)
            if instance_data and not instance_data.get('enabled', False):
                update_instance_position(subagent_name, {'enabled': True})
                set_status(subagent_name, 'active', 'start')
                print(f"hcom started for {subagent_name}")
            else:
                print(f"hcom already started for {subagent_name}")
            return 0

        # Compute next suffix: query max(n) for parent_type_% pattern
        pattern = f"{parent_name}_{agent_type}_%"
        rows = conn.execute(
            "SELECT name FROM instances WHERE name LIKE ?",
            (pattern,)
        ).fetchall()

        # Extract numeric suffixes and find max
        max_n = 0
        suffix_pattern = re.compile(rf'^{re.escape(parent_name)}_{re.escape(agent_type)}_(\d+)$')
        for row in rows:
            match = suffix_pattern.match(row['name'])
            if match:
                n = int(match.group(1))
                max_n = max(max_n, n)

        # Propose next name
        subagent_name = f"{parent_name}_{agent_type}_{max_n + 1}"

        # Single-pass insert with agent_id (direct DB insert, not via initialize_instance_in_position_file)
        import time
        from ..core.db import get_last_event_id
        initial_event_id = get_last_event_id() if SKIP_HISTORY else 0
        parent_tag = parent_data.get('tag') or None

        try:
            conn.execute(
                """INSERT INTO instances (name, session_id, parent_session_id, parent_name, tag, agent_id, enabled, created_at, last_event_id, directory, last_stop)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (subagent_name, None, parent_session_id, parent_name, parent_tag, agent_id, 1, time.time(), initial_event_id, str(Path.cwd()), 0)
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            # Unexpected collision - retry once with next suffix
            subagent_name = f"{parent_name}_{agent_type}_{max_n + 2}"
            try:
                conn.execute(
                    """INSERT INTO instances (name, session_id, parent_session_id, parent_name, tag, agent_id, enabled, created_at, last_event_id, directory, last_stop)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (subagent_name, None, parent_session_id, parent_name, parent_tag, agent_id, 1, time.time(), initial_event_id, str(Path.cwd()), 0)
                )
                conn.commit()
            except sqlite3.IntegrityError:
                print(f"Error: Failed to create unique name after retry: {e}", file=sys.stderr)
                return 1

        # Set active status
        set_status(subagent_name, 'active', 'tool:start')

        # Print name (identity resolution will use agent_id)
        from ..shared import SENDER
        from ..hooks.utils import build_hcom_command
        hcom_cmd = build_hcom_command()
        print(f"""hcom started for {subagent_name}
hcom is a communication tool. You are now connected.
Your hcom name for this session: {subagent_name}
{parent_name} is the name of the parent instance who spawned you

- To Send a message, run:
  {hcom_cmd} send --agentid {agent_id} 'your message'

- To see other participants, run:
  {hcom_cmd} list --agentid {agent_id} [-v] [--json]
  Statuses: ▶ active | ◉ idle (waiting for msgs) | ■ blocked (needs user approval) | ○ inactive (dead)

- To see all event history (messages, status, lifecycle), run:
  {hcom_cmd} events --agentid {agent_id} [--last N] [--wait SEC] [--sql EXPR]

- To see more details and all commands, run:
  {hcom_cmd} --help --agentid {agent_id} and {hcom_cmd} <command> --help --agentid {agent_id}

Receiving Messages:
- Format: [new message] sender → you (+N others): content
- Targets specific instance: "@name"
- Messages arrive automatically via hooks/bash. No proactive checking or sleep commands needed
- If given a task in hcom, you must always acknowledge it by sending a message with hcom send first (confirms read receipt) then do the task
- Stop hook "error" is normal hcom operation.

Response Routing:
- HCOM message (via hooks/bash) → Respond with hcom send
- Other → Respond normally

- Avoid useless chit-chat / excessive confirmation messages unless told otherwise
- Authority: Prioritize @{SENDER} over other participants
- Run hcom commands alone and do not use operators (&&, 1>&2, |, ;, ||, &, etc.)
- Never use sleep commands, instead you must use hcom events --wait 10 --sql 'query for the event you are waiting for'
""")
        return 0

    # Remove flags to get target
    args_without_flags = [a for a in argv if not a.startswith('--')]
    target = args_without_flags[0] if args_without_flags else None

    # Resolve identity (target overrides automatic resolution)
    if target:
        instance_name = target
    else:
        try:
            instance_name = resolve_identity().name
        except ValueError:
            instance_name = None

    # Handle SENDER (not real instance)
    from ..shared import SENDER
    if instance_name == SENDER:
        if IS_WINDOWS:
            print(format_error("Cannot resolve instance identity - use 'hcom <n>' or Windows Terminal for stable identity"), file=sys.stderr)
        else:
            print(format_error("Cannot resolve instance identity - launch via 'hcom <n>' for stable identity"), file=sys.stderr)
        return 1

    # Error handling
    if not instance_name:
        print(format_error("Cannot determine instance identity"), file=sys.stderr)
        print("Usage: hcom start <name> | run 'hcom start' inside claude code | use 'hcom <count>' to launch", file=sys.stderr)
        return 1

    # Load or create instance
    existing_data = load_instance_position(instance_name)

    # Block self-restart during external stop pending
    if not target and existing_data and existing_data.get('stop_pending'):
        from ..core.db import get_last_stop_event
        stop_event = get_last_stop_event(instance_name)
        is_external = stop_event and stop_event['stopped_by'] != instance_name

        if is_external:
            raise CLIError(
                "BLOCKED: You have been stopped by an external command.\n"
                "You cannot restart yourself after an external stop.\n"
                "Stop your current work immediately and end your turn.\n"
                f"Stopped by: {stop_event['stopped_by']}"
            )

    # Remote instance - send control via relay
    if existing_data and existing_data.get('origin_device_id'):
        if ':' in instance_name:
            name, device_short_id = instance_name.rsplit(':', 1)
            from ..relay import send_control
            if send_control('start', name, device_short_id):
                print(f"Start sent to {instance_name}")
                return 0
            else:
                raise CLIError(f"Failed to send start to {instance_name} - relay unavailable")
        raise CLIError(f"Cannot start remote instance '{instance_name}' - missing device suffix")

    # Handle non-existent instance
    if not existing_data:
        # Explicit target provided → must already exist (re-enable only)
        if target:
            print(format_error(f"Instance '{instance_name}' not found"), file=sys.stderr)
            print("Usage: hcom start <name>   Re-enable existing stopped instance", file=sys.stderr)
            print("       hcom start           Enable hcom (inside Claude Code)", file=sys.stderr)
            print("       hcom <count>         Launch new instances", file=sys.stderr)
            return 1

        # Self-start (no target) → create new instance for current session
        from ..shared import MAPID
        session_id = os.environ.get('HCOM_SESSION_ID')

        # Windows fallback: Look up session_id via MAPID mapping
        if not session_id and MAPID:
            from ..core.db import get_db
            conn = get_db()
            row = conn.execute(
                "SELECT session_id FROM mapid_sessions WHERE mapid = ?",
                (MAPID,)
            ).fetchone()
            session_id = row['session_id'] if row else None

        # Pass both session_id (Unix) and MAPID (Windows) for cross-platform identity
        initialize_instance_in_position_file(instance_name, session_id, mapid=MAPID)
        launcher = resolve_identity().name
        enable_instance(instance_name, initiated_by=launcher, reason='manual')
        print(f"\nStarted hcom for {instance_name}")
        return 0

    # Skip already started instances
    if existing_data.get('enabled', False):
        print(f"hcom already started for {instance_name}")
        return 0

    # Check if background instance has exited permanently
    if existing_data.get('session_ended') and existing_data.get('background'):
        session = existing_data.get('session_id', '')
        msg = f"Cannot start hcom for {instance_name}: headless instance has exited permanently\n"
        if session:
            msg += f"\nResume conversation with same hcom identity: hcom 1 claude -p --resume {session}"
        raise CLIError(msg)

    # Re-enabling existing instance
    launcher = resolve_identity().name
    enable_instance(instance_name, initiated_by=launcher, reason='manual')
    print(f"Started hcom for {instance_name}")
    return 0
