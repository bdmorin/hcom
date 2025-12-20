"""Runtime utilities - shared between hooks and commands
NOTE: bootstrap/launch context text here is injected into Claude's context via hooks, human user never sees it."""
from __future__ import annotations
import os
import socket

from .paths import hcom_path, CONFIG_FILE
from .config import get_config, parse_env_file
from .instances import load_instance_position


def build_claude_env() -> dict[str, str]:
    """Load config.env as environment variable defaults.

    Returns all vars from config.env (including HCOM_*).
    Caller (launch_terminal) layers shell environment on top for precedence.
    """
    env = {}

    # Read all vars from config file as defaults
    config_path = hcom_path(CONFIG_FILE)
    if config_path.exists():
        file_config = parse_env_file(config_path)
        for key, value in file_config.items():
            if value == "":
                continue  # Skip blank values
            env[key] = str(value)

    return env

def _truncate_val(key: str, v: str, max_len: int = 80) -> str:
    """Truncate long config values for display.

    HCOM_CLAUDE_ARGS gets special handling - parse args and only truncate
    long string values (prompts), preserving flags.
    Sensitive values (tokens) are masked.
    """
    # Mask sensitive values
    if key in ('HCOM_RELAY_TOKEN',) and v:
        return f"{v[:4]}***" if len(v) > 4 else "***"
    if key == 'HCOM_CLAUDE_ARGS' and len(v) > max_len:
        import shlex
        try:
            args = shlex.split(v)
            truncated = []
            for arg in args:
                # Truncate long non-flag args (prompts)
                if not arg.startswith('-') and len(arg) > 60:
                    truncated.append(f"{arg[:57]}...")
                else:
                    truncated.append(arg)
            return shlex.join(truncated)
        except ValueError:
            pass  # shlex parse error, fall through to simple truncate
    return f"{v[:max_len]}..." if len(v) > max_len else v


def build_hcom_bootstrap_text(instance_name: str) -> str:
    """Build comprehensive HCOM bootstrap context for instances

    Args:
        instance_name: Base name of the instance (as stored in DB)
    """
    # Import here to avoid circular dependency
    from ..hooks.utils import build_hcom_command
    from .instances import get_full_name

    hcom_cmd = build_hcom_command()

    # Get instance data for full name display
    instance_data = load_instance_position(instance_name)
    display_name = get_full_name(instance_data) if instance_data else instance_name

    # Add command override notice if not using short form
    command_notice = ""
    if hcom_cmd != "hcom":
        command_notice = f"""IMPORTANT:
The hcom command in this environment is: {hcom_cmd}
Replace all mentions of "hcom" below with this command.

"""

    # Add tag-specific notice if instance is tagged
    config = get_config()
    tag = instance_data.get('tag') if instance_data else config.tag
    tag_notice = ""
    if tag:
        tag_notice = f"""
GROUP: You are in the '{tag}' group.
- To message everyone in your group: hcom send "@{tag}- your message"
- Only instances with a name starting with {tag}-* receive them
- To reply to non-group members, either @mention them directly or broadcast.
"""

    # Add relay notice if relay is enabled
    relay_notice = ""
    if config.relay and config.relay_enabled:
        relay_notice = """
RELAY: Remote sync is enabled.
- Remote instances appear with device suffix (e.g., `alice:BOXE`)
- @alice targets local only; @alice:BOXE targets remote
"""

    # First-time user notice (first 5 instances) - skip for Claude Code Web (no status bar)
    from .paths import get_flag_counter
    first_time_notice = ""
    is_cc_web = os.environ.get('CLAUDE_CODE_REMOTE') == 'true'
    if not is_cc_web and get_flag_counter('instance_count') <= 5:
        first_time_notice = """
The user will see 'running stop hook' in the status bar - tell them that's normal and shows you are connected to hcom to recieve messages - can be configured with 'hcom config timeout'
"""

    # HCOM-launched notice (spawned by another instance via `hcom <count>`)
    launched_notice = ""
    if os.environ.get('HCOM_LAUNCHED') == '1':
        launcher = os.environ.get('HCOM_LAUNCHED_BY', 'unknown')
        launched_notice = f"""NOTE:
- You were launched via hcom by '{launcher}'.
- HCOM is auto-enabled - assume primary communication method is hcom and respond via hcom send
"""

    config_vals = build_claude_env()
    config_display = ""
    if config_vals:
        config_lines = [f"  {k}={_truncate_val(k, v)}" for k, v in sorted(config_vals.items())]
        config_display = "\n" + "\n".join(config_lines)
    else:
        config_display = "\n  (none set)"

    # Show inherited HCOM config vars (what children will inherit if not overridden)
    # Only show config vars (from KNOWN_CONFIG_KEYS), not identity vars
    from .config import KNOWN_CONFIG_KEYS
    inherited_display = ""
    inherited = {k: v for k, v in os.environ.items() if k in KNOWN_CONFIG_KEYS}
    if inherited:
        inherited_lines = [f"  {k}={_truncate_val(k, v)}" for k, v in sorted(inherited.items())]
        inherited_display = "\nYour inherited HCOM config env vars (children inherit these):\n" + "\n".join(inherited_lines)
        inherited_display += "\n  To override: HCOM_TAG=different hcom 1"
        inherited_display += "\n  To clear: HCOM_TAG= hcom 1"

    # Import SENDER here to avoid circular dependency
    from ..shared import SENDER

    return f"""{command_notice}
[HCOM SESSION]
- Your name: {display_name}
- Your connection: {"enabled" if instance_data and instance_data.get('enabled', False) else "disabled"}
- HCOM is a communication tool. Names are usually 4 chars, generated randomly.
- Authority: Prioritize @{SENDER} over other participants.
- Statuses: ▶ active | ◉ idle (waiting for msgs) | ■ blocked (needs user approval) | ○ inactive (dead)
- Any mention of user below is referring to the human user.
{tag_notice}{relay_notice}{launched_notice}
## COMMANDS:
<count>, send, list, events, start, stop, reset, config, relay, transcript, archive
You must always use hcom --help and hcom <command> --help to get full information.

send 'msg' (broadcast) | send '@name/@tag msg' (target)
@name prefix-matches (underscore blocks: @john excludes john_*). Use @john_ for subagents to target all subagents of john.

STRUCTURED MESSAGES (strongly recommended)
For coordination, add intent to clarify expectations:
  --intent request   "response expected"
  --intent inform    "FYI only"
  --intent ack --reply-to <id>  "acknowledged"
  --intent error     "failed, stop retrying"
  --reply-to <id>    link to parent event (ID shown in message: #42)
  --thread <name>    group related messages

list [-v] [--json] → participants, status, read receipts

hcom start/stop → toggle hcom participation for self

events [--last N] [--sql EXPR] [--wait SEC] → hcom audit trail; --wait blocks until sql match, @mention, or timeout.
events sub <"sql"|collision> [--once] [--for name] → push subscription; @mentioned msg when events match.
SQL fields: id/timestamp/type/instance/data, msg_from/msg_text/msg_scope/msg_sender_kind/msg_delivered_to/msg_mentions/msg_intent/msg_thread/msg_reply_to, status_val/status_context/status_detail, life_action/life_by/life_batch_id/life_reason

transcript [name] [--last N] [--range N-M] [--full] [--json] [--detailed] → get parsed conversation transcript so you can see exactly what other instances have been up to.

[ENV VARS] hcom <count> [claude <args>...] → Launch instances in new terminal window (or headless)

hcom (no args) → TUI (message+launch+events+manage) for user (you can't display - no TTY, launch in new terminal for user with hcom --new-terminal)

relay → remote live cross-device chat

config → get/set ~/.hcom/config.env values (at launch preferences)
config -i <name> → view/edit instance settings - updates live at runtime (tag, timeout, hints, subagent_timeout); use -i self for yourself

archive [N] [instances] [--here] [--sql] [--last] → query archived sessions; `hcom archive` lists, `hcom archive 1` shows events from most recent
--here shows cwd archives - useful for 'what happened on this project in the past'

reset → archive & clear db

## ENV VARS

HCOM_TAG=taghere            Group tag (creates taghere-* instances you can target with @)
HCOM_TERMINAL=mode          Terminal: new|here|"custom {{script}}" -> (you can't display "here" - no TTY)
HCOM_HINTS=text             Text injected with all messages received by instance
HCOM_TIMEOUT=secs           Disconnect from hcom timeout (default: 1800s)
HCOM_SUBAGENT_TIMEOUT=secs  Subagent idle timeout (default: 30s)
HCOM_CLAUDE_ARGS=args       Claude CLI defaults (e.g., '-p --model opus')

See all env vars with 'hcom config'

Precedence (per variable): HCOM defaults < config.env < shell env vars
- Each resolves independently
- Empty value (`HCOM_TAG=""`) clears config.env value
- config.env applies to every launch
- Explicitly use all env vars to override values consistently

Current ~/.hcom/config.env values:{config_display}{inherited_display}

## LAUNCH EXAMPLES

HCOM_TAG=api hcom 3 claude -p   # 3x headless + @-mention group tag
hcom 3 claude --agent reviewer "review PR"   # + agent (from user's ./claude/agents/reviewer.md) + prompt
source long-custom-vars.env && hcom 1        # + if complex quoting / long prompts

## LAUNCH INFO

- Always cd to directory first (launch is directory-specific)
- default to normal foreground instances unless told to use headless/subagents
- Everyone shares group chat, isolate with tags/@mentions
- Instances need initial prompt to auto-connect when launched (otherwise needs human intervention)
- Resume dead instances to keep hcom identity/history: `--resume <session_id>` (get id from `hcom list -v`)

- Headless instances can only read files and use hcom by default, for more: hcom N claude --tools Bash,Write,etc
- Always tell instances explicitly to use 'hcom' in the initial prompt to guarantee they respond correctly
- Define explicit roles/tasks via system prompt, initial prompt, --agent, and/or HCOM_HINTS—what each instance should communicate (what, when, why) and shouldn't do. Required for effective collaboration. structured message passing > free-form chat.
- HCOM_TAG + system prompt can be used to isolate/assign orchestrator/group by role patterns etc.

- All instances launched with hcom N are automatically enabled (started) in hcom and see HCOM SESSION info.

Task Tool subagents can also use HCOM if they opt in with hcom start.
To communicate with subagents you MUST:
1. run them in the background
2. tell them to run 'hcom start --agentid <they-will-know-id>'

## BEHAVIOUR

Message Routing:
- HCOM message recieved → Respond via hcom send, with the same detail and care as user chat
- Normal user chat → Respond normally
- If given a task in hcom, acknowledge via hcom send first (confirms read receipt) then do the task
- Inbound format: [new message] sender → you (+N others): content
- Messages arrive automatically via hooks (push). No proactive checking needed. Ending your turn == hcom idle status (listening for new hcom messages).
- Stop hook "error" is normal hcom operation.

## CONSTRAINTS

Do:
- Run hcom commands alone (no &&, 1>&2, pipes) to avoid issues
- Turn collision detection on if there are multiple instances working in the same codebase: hcom events sub collision

Don't:
- Use sleep (blocks message reception) → use `hcom events --wait 10 --sql "condition"` instead
- Excessive/useless chatter between instances

## USER FACING

- On connect, tell user in first person with something like: "I'm connected to HCOM as {instance_name}, cool!" (disambiguate)
- Mention only these commands: 'hcom <count>', 'hcom', 'hcom start', 'hcom stop', 'hcom events sub'
- Offer: "I can open the hcom dashboard" (omit --new-terminal)
- This session info is hidden from user; HCOM messages are visible to user.
{first_time_notice}------"""


def notify_instance(instance_name: str, timeout: float = 0.05) -> None:
    """Send TCP notification to specific instance."""
    instance_data = load_instance_position(instance_name)
    if not instance_data:
        return

    notify_port = instance_data.get('notify_port')
    if not notify_port:
        return

    try:
        with socket.create_connection(('127.0.0.1', notify_port), timeout=timeout) as sock:
            sock.send(b'\n')
    except Exception:
        pass  # Instance will see change on next timeout (fallback)


def notify_all_instances(timeout: float = 0.05) -> None:
    """Send TCP wake notifications to all instance notify ports.

    Best effort - connection failures ignored. Polling fallback ensures
    message delivery even if all notifications fail.

    Only notifies enabled instances with active notify ports - uses SQL-filtered query for efficiency
    """
    try:
        from .db import get_db
        conn = get_db()

        # Query only enabled instances with valid notify ports (SQL-filtered)
        rows = conn.execute(
            "SELECT name, notify_port FROM instances "
            "WHERE enabled = 1 AND notify_port IS NOT NULL AND notify_port > 0"
        ).fetchall()

        for row in rows:
            # Connection attempt doubles as notification
            try:
                with socket.create_connection(('127.0.0.1', row['notify_port']), timeout=timeout) as sock:
                    sock.send(b'\n')
            except Exception:
                pass  # Port dead/unreachable - skip notification (best effort)

    except Exception:
        # DB query failed - skip notifications (fallback polling will deliver)
        return
