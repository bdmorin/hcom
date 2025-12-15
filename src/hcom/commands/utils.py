"""Command utilities for HCOM"""
import sys
import re
import os
from ..shared import __version__, MAX_MESSAGE_SIZE, SenderIdentity, SENDER, HcomError


class CLIError(Exception):
    """Raised when arguments cannot be mapped to command semantics."""


# Command registry - single source of truth for CLI help
# Format: list of (usage, description) tuples per command
COMMAND_HELP: dict[str, list[tuple[str, str]]] = {
    'events': [
        ('', 'Query the event stream (messages, status changes, file edits, lifecycle)'),
        ('', ''),
        ('Query:', ''),
        ('  events', 'Recent events as JSON'),
        ('  --last N', 'Limit count (default: 20)'),
        ('  --sql EXPR', 'SQL WHERE filter'),
        ('  --wait [SEC]', 'Block until match (default: 60s)'),
        ('', ''),
        ('Subscribe:', ''),
        ('  events sub', 'List subscriptions'),
        ('  events sub "sql"', 'Push notification when event matches SQL'),
        ('  events sub collision', 'Alert when instances edit same file'),
        ('    --once', 'Auto-remove after first match'),
        ('    --for <name>', 'Subscribe for another instance'),
        ('  events unsub <id>', 'Remove subscription by ID'),
        ('  events unsub collision', 'Disable collision alerts'),
        ('', ''),
        ('SQL columns (events_v view):', ''),
        ('  Base', 'id, timestamp, type, instance'),
        ('  msg_*', 'from, text, scope, sender_kind, delivered_to, mentions, intent, thread, reply_to'),
        ('  status_*', 'val, context, detail'),
        ('  life_*', 'action, by, batch_id, reason'),
        ('', ''),
        ('', 'Example: msg_from = \'alice\' AND type = \'message\''),
        ('', 'Use <> instead of != for SQL negation'),
    ],
    'list': [
        ('list', 'All instances'),
        (' -v', 'Verbose output of all instances'),
        (' --json', 'Verbose JSON output of all instances'),
        ('',''),
        ('list [self|<name>]', 'Instance details'),
        ('  [field]', 'Print specific field (status, directory, session_id, etc)'),
        ('  --json', 'Output as JSON'),
        ('  --sh', 'Shell exports: eval "$(hcom list self --sh)"'),
    ],
    'send': [
        ('send "msg"', 'Send message to all your best buddies'),
        ('send "@name msg"', 'Send to specific instance/group'),
        ('  --from <name>', 'Identity for non-Claude tools (Gemini, scripts)'),
        ('  --wait', 'Poll for @mentions (use with --from)'),
        ('Envelope (optional):', ''),
        ('  --intent <type>', 'request|inform|ack|error'),
        ('  --reply-to <id>', 'Link to event (42 or 42:BOXE for remote)'),
        ('  --thread <name>', 'Group related messages'),
    ],
    'stop': [
        ('stop', 'Disable hcom for current instance'),
        ('stop <name>', 'Disable hcom for specific instance'),
        ('stop all', 'Disable hcom for all instances'),
    ],
    'start': [
        ('start', 'Enable hcom for current instance'),
        ('start <name>', 'Re-enable stopped instance'),
    ],
    'reset': [
        ('reset', 'Clear database (archive conversation)'),
        ('reset hooks', 'Remove hooks only'),
        ('reset all', 'Stop all + clear db + remove hooks + reset config'),
    ],
    'config': [
        ('config', 'Show all config values'),
        ('config <key>', 'Get single config value'),
        ('config <key> <val>', 'Set config value'),
        ('  --json', 'JSON output'),
        ('  --edit', 'Open config in $EDITOR'),
        ('  --reset', 'Reset config to defaults'),
        ('Instance config:', ''),
        ('config -i <name>', 'Show instance config'),
        ('config -i <name> <key>', 'Get instance config value'),
        ('config -i <name> <key> <val>', 'Set instance config value'),
        ('  -i self', 'Current instance (requires Claude context)'),
        ('  keys: tag, timeout, hints, subagent_timeout', ''),
        ('Global settings:', ''),
        ('  HCOM_TAG', 'Group tag (creates tag-* instances)'),
        ('  HCOM_TERMINAL', 'Terminal: new|here|"custom {script}"'),
        ('  HCOM_HINTS', 'Text appended to messages received by instance'),
        ('  HCOM_TIMEOUT', 'Idle timeout in seconds (default: 1800)'),
        ('  HCOM_SUBAGENT_TIMEOUT', 'Subagent timeout in seconds (default: 30)'),
        ('  HCOM_CLAUDE_ARGS', 'Default claude args (e.g. "-p --model opus")'),
        ('  HCOM_RELAY', 'Relay server URL'),
        ('  HCOM_RELAY_TOKEN', 'Relay auth token'),
        ('  HCOM_RELAY_ENABLED', 'Enable relay sync (1|0)'),
        ('  HCOM_NAME_EXPORT', 'Also export instance name to this var'),
        ('', ''),
        ('', 'Non-HCOM_* vars in config.env pass through to Claude Code'),
        ('', 'e.g. ANTHROPIC_MODEL=opus'),
        ('', ''),
        ('Precedence:', 'HCOM defaults < config.env < shell env vars'),
        ('', 'Each resolves independently'),
    ],
    'relay': [
        ('relay', 'Show relay status'),
        ('relay on', 'Enable cross-device chat'),
        ('relay off', 'Disable cross-device chat'),
        ('relay pull', 'Fetch from other devices now'),
        ('relay hf [token]', 'Connect to relay server on HuggingFace'),
        ('', '(finds or creates a free private space on your HuggingFace account'),
        ('', 'provide HF_TOKEN or login with hf cli first)'),
    ],
    'transcript': [
        ('transcript', 'Show your conversation transcript (last 10)'),
        ('transcript N', 'Show exchange N (absolute position)'),
        ('transcript N-M', 'Show exchanges N through M'),
        ('transcript @instance', 'See another instance\'s transcript'),
        ('transcript @instance N', 'Exchange N of another instance'),
        ('  --last N', 'Limit to last N exchanges'),
        ('  --full', 'Show full assistant responses'),
        ('  --detailed', 'Show tool I/O, edits, errors'),
        ('  --json', 'JSON output'),
    ],
    'archive': [
        ('archive', 'List archived sessions (numbered)'),
        ('archive <N>', 'Query events from archive (1 = most recent)'),
        ('archive <N> instances', 'Query instances from archive'),
        ('archive <name>', 'Query by stable name (prefix match works)'),
        ('  --here', 'Filter to archives with current directory'),
        ('  --sql "expr"', 'SQL WHERE filter'),
        ('  --last N', 'Limit to last N events (default: 20)'),
        ('  --json', 'JSON output'),
    ],
}


def get_command_help(name: str) -> str:
    """Get formatted help for a single command."""
    if name not in COMMAND_HELP:
        return f"Usage: hcom {name}"
    lines = ['Usage:']
    for usage, desc in COMMAND_HELP[name]:
        if not usage:  # Empty line or plain text
            lines.append(f"  {desc}" if desc else '')
        elif usage.startswith('  '):  # Option/setting line
            lines.append(f"  {usage:<20} {desc}")
        elif usage.endswith(':'):  # Section header
            lines.append(f"\n{usage} {desc}" if desc else f"\n{usage}")
        else:  # Command line
            lines.append(f"  hcom {usage:<18} {desc}")
    return '\n'.join(lines)


def _format_commands_section() -> str:
    """Generate Commands section from registry."""
    lines = []
    for name, entries in COMMAND_HELP.items():
        for usage, desc in entries:
            if usage.startswith('  '):  # Option
                lines.append(f"  {usage:<18} {desc}")
            else:  # Command
                lines.append(f"  {usage:<18} {desc}")
        lines.append('')  # Blank line between commands
    return '\n'.join(lines).rstrip()


def get_help_text() -> str:
    """Generate help text with current version"""
    return f"""hcom v{__version__} - Hook-based communication for Claude Code instances

Usage:
  hcom                               TUI dashboard
  [env vars] hcom <N> [claude ...]   Launch instances
  hcom <command>                     Run command

Commands:
  send      Send message to your buddies
  list      Show participants, status, read receipts
  start     Enable hcom participation
  stop      Disable hcom participation
  events    Query events / subscribe for push notifications
  transcript View conversation transcript
  config    Get/set config environment variables
  relay     Cross-device live chat
  archive   Query archived sessions
  reset     Archive and clear database or hooks

Run 'hcom <command> --help' for details.
"""


# Known flags per command - for validation against hallucinated flags
# --agentid required for subagents to identify themselves
KNOWN_FLAGS: dict[str, set[str]] = {
    'send': {'--agentid', '--from', '--wait', '--intent', '--reply-to', '--thread'},
    'events': {'--last', '--wait', '--sql', '--agentid'},
    'events sub': {'--once', '--for'},
    'events unsub': set(),
    'events launch': set(),
    'list': {'--json', '-v', '--verbose', '--sh', '--agentid'},
    'start': {'--agentid'},
    'stop': {'--agentid'},
    'transcript': {'--last', '--range', '--json', '--full', '--detailed', '--agentid'},
    'config': {'--json', '--edit', '--reset', '-i'},
    'reset': set(),  # Just subcommands
    'relay': {'--name', '--update'},  # For relay hf
    'archive': {'--json', '--here', '--sql', '--last', '--agentid'},
}


def validate_flags(cmd: str, argv: list[str]) -> str | None:
    """Validate flags against known flags for command.

    Returns error message with help if unknown flag found, None if valid.
    """
    known = KNOWN_FLAGS.get(cmd, set())
    for arg in argv:
        if arg.startswith('-') and arg not in known:
            help_text = get_command_help(cmd)
            return f"Unknown flag '{arg}'\n\n{help_text}"
    return None


def format_error(message: str, suggestion: str | None = None) -> str:
    """Format error message consistently"""
    base = f"Error: {message}"
    if suggestion:
        base += f". {suggestion}"
    return base


def is_interactive() -> bool:
    """Check if running in interactive mode"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_identity(subagent_id: str | None = None, custom_from: str | None = None, system_sender: str | None = None) -> SenderIdentity:
    """Resolve identity in CLI/hook context.

    Args:
        subagent_id: Explicit subagent ID (from Task tool context)
        custom_from: Custom display name (--from flag)
        system_sender: System notification sender name (e.g., 'hcom-launcher')

    Returns:
        SenderIdentity with kind, name, and instance_data

    Identity kind:
        - 'external': Custom sender or CLI (--from or bigboss)
        - 'instance': Real instance (Claude Code with session)
        - 'system': System notifications (launcher, watchdog, etc)
    """
    import os
    from ..shared import MAPID
    from ..core.instances import load_instance_position, resolve_instance_name
    from ..core.config import get_config

    # System sender (internal notifications) - always system
    if system_sender:
        return SenderIdentity(kind='system', name=system_sender, instance_data=None)

    # Custom sender (--from) - always external
    if custom_from:
        return SenderIdentity(kind='external', name=custom_from, instance_data=None)

    # Subagent explicit (Task tool)
    if subagent_id:
        data = load_instance_position(subagent_id)
        if not data:
            # This shouldn't happen - cmd_send validates before calling
            raise HcomError(f"Subagent '{subagent_id}' position data missing")
        return SenderIdentity(kind='instance', name=subagent_id, instance_data=data, session_id=data.get('session_id'))

    # CLI context (not in Claude Code)
    if os.environ.get('CLAUDECODE') != '1':
        return SenderIdentity(kind='external', name=SENDER, instance_data=None)

    # Inside Claude: try session_id (Unix only - CLAUDE_ENV_FILE doesn't work on Windows)
    session_id = os.environ.get('HCOM_SESSION_ID')
    if session_id:
        name, data = resolve_instance_name(session_id, get_config().tag)
        # Return instance identity (data may be None if not opted in yet)
        return SenderIdentity(kind='instance', name=name, instance_data=data, session_id=session_id)

    # Try MAPID (Windows fallback - terminal session ID like WT_SESSION)
    if MAPID:
        from ..core.db import get_instance_by_mapid, get_db
        from ..core.instances import resolve_instance_name

        # First try to find existing instance by MAPID
        data = get_instance_by_mapid(MAPID)
        if data:
            return SenderIdentity(kind='instance', name=data['name'], instance_data=data, session_id=data.get('session_id'))

        # No instance for this MAPID - look up session_id from mapping
        # This handles Windows resume in different terminal (MAPID changes but session_id stays same)
        conn = get_db()
        row = conn.execute(
            "SELECT session_id FROM mapid_sessions WHERE mapid = ?",
            (MAPID,)
        ).fetchone()

        if row:
            # Found session_id mapping - use it for consistent naming across terminals
            session_id = row['session_id']
            name, data = resolve_instance_name(session_id, get_config().tag)
            # Return instance identity (data may be None if not opted in yet)
            return SenderIdentity(kind='instance', name=name, instance_data=data, session_id=session_id)

    # No identity available - fail with error directing to --from
    raise HcomError("Cannot resolve identity - use: hcom send --from <yourname> \"message\"")


def validate_message(message: str) -> str | None:
    """Validate message size and content. Returns error message or None if valid."""
    if not message or not message.strip():
        return format_error("Message required")

    # Reject control characters (except \n, \r, \t)
    if re.search(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\u0080-\u009F]', message):
        return format_error("Message contains control characters")

    if len(message) > MAX_MESSAGE_SIZE:
        return format_error(f"Message too large (max {MAX_MESSAGE_SIZE} chars)")

    return None


def parse_agentid_flag(argv: list[str]) -> tuple[str | None, list[str], str | None]:
    """Parse --agentid flag and return (instance_name, remaining_argv, agent_id_value)

    Looks up instance by agent_id and returns the instance name.
    Special case: --agentid parent returns parent's identity.

    Returns:
        (instance_name, argv, agent_id_value): Instance name if found (else None), argv with flag removed, agent_id value if flag was provided (else None).

    Raises:
        CLIError: If --agentid is provided without a value.
    """
    if '--agentid' not in argv:
        return None, argv, None

    idx = argv.index('--agentid')
    if idx + 1 >= len(argv) or argv[idx + 1].startswith('-'):
        raise CLIError("--agentid requires a value")

    agent_id = argv[idx + 1]
    argv = argv[:idx] + argv[idx + 2:]

    # Special case: --agentid parent means use parent's identity
    if agent_id == 'parent':
        identity = resolve_identity()
        # Warn if not in subagent context (subagents are dead)
        from ..hooks.subagent import in_subagent_context
        if identity.name and not in_subagent_context(identity.name):
            import sys
            print("Warning: --agentid parent not needed (no active subagents)", file=sys.stderr)
        return identity.name, argv, 'parent'

    # Look up instance by agent_id
    from ..core.db import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM instances WHERE agent_id = ?",
        (agent_id,)
    ).fetchone()

    return (row['name'] if row else None), argv, agent_id
