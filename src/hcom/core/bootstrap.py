"""Bootstrap context builder - template-based.

Concise bootstrap optimized for token efficiency. Agents learn details via --help.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .config import get_config
from .thread_context import get_background_name, get_is_launched
from .instances import load_instance_position, get_full_name
from .paths import hcom_path


# =============================================================================
# MAIN BOOTSTRAP TEMPLATE
# =============================================================================

UNIVERSAL = """[HCOM SESSION]
You have access to the hcom communication tool.
- Your name: {display_name}
- Authority: Prioritize @{SENDER} over others
- Important: Include this marker anywhere in your first response: [hcom:{instance_name}]

You run hcom commands on behalf of the human user. The human uses natural language with you.

## MESSAGES

Response rules:
- From {SENDER} or intent=request → always respond
- intent=inform → respond only if useful
- intent=ack → don't respond

Routing:
- hcom message (<hcom> tags, hook feedback) → respond via hcom send
- Normal user chat → respond in chat

## CAPABILITIES

You MUST use `hcom <cmd> --name {instance_name}` for all hcom commands:

- Message: send @name(s) [--intent request|inform|ack] [--reply-to <id>] -- <"message"> (or --stdin, --file <path>, --base64 <string>)
- See who's active: list [-v] [--json]
- Read another's conversation: transcript [name] [--range N-M] [--last N] [--full]
- See history (filter by agent, commands ran, files touched, status, msgs, sql): events --help
- Get notified on events: events sub --help
- Handoff context: bundle prepare → send with --title
- Spawn agents: [HCOM_TAG=label-or-group] hcom N <claude|gemini|codex>
- Resume/fork agents, background, tmux, kitty, prompt, system, forward args: hcom N tool --help
- Run workflows: run <script> [args]
- View agent screen, inject text/enter: term [name]
- install status, stop, kill, set tag, set terminal, remote, etc.: --help

If unsure about syntax, always run `hcom <command> --help` FIRST. Do not guess.

## RULES

1. Always substitute `sleep [timeout]` with `hcom listen [timeout]`
2. Task via hcom → ack immediately, do work, report via hcom
3. Avoid chit-chat, welcomes, thanks-only messages
4. Use --intent on sends: request (want reply), inform (FYI), ack (responding).
5. User says "the gemini agent", "ask claude" → run `hcom list` to resolve name

Agent names are 4-letter CVCV words. When user mentions one, they mean an agent.
{active_instances}{user_scripts}

This is session context, not a task for immediate action.
"""

TAG_NOTICE = """
You are tagged "{tag}". Message your group: send @{tag}- -- msg
"""

RELAY_NOTICE = """
Remote agents have suffix (e.g., `luna:BOXE`). @luna = local only; @luna:BOXE = remote.
"""

HEADLESS_NOTICE = """
Headless mode: No one sees your chat, only hcom messages. Communicate via hcom send.
"""

UVX_CMD_NOTICE = """
Note: hcom command in this environment is `{hcom_cmd}`. Substitute in examples.
"""

# Tool-specific delivery

DELIVERY_AUTO = """
## DELIVERY

Messages auto-arrive via <hcom> tags — end your turn to receive them.

Use hcom listen only as an alternative to sleep. If you are purely waiting for a hcom message; end your turn.
"""

DELIVERY_CODEX_HCOM_LAUNCHED = """
## DELIVERY

<hcom> tags = run `hcom listen 1 --name {instance_name}` IMMEDIATELY to read message.
"""

DELIVERY_ADHOC = """
## DELIVERY

Messages do NOT arrive automatically.
- Check messages: `hcom listen 1`
- Wait for messages: `hcom listen [timeout]`

<critical_rule>
LISTENING REQUIREMENT:
- After sending hcom message expecting reply → `hcom listen --timeout 60 --name {instance_name}`
- After task via hcom → complete, report, then listen again
- User says "stay connected" → enter listen loop

CONNECTED MODE:
1. Run: `hcom listen --name {instance_name} --timeout [max]`
2. Timeout → listen again
3. Message received → handle it, then listen again
4. Exit only when user says stop

WRONG: hcom listen &
RIGHT: hcom listen --timeout [sec]
</critical_rule>

You are now registered with hcom.
"""

CLAUDE_ONLY = """
## SUBAGENTS

Subagents can join hcom:
1. Run Task with background=true
2. Tell subagent: `use hcom`

Subagents get their own hcom context and a random name. DO NOT give them any specific hcom syntax.
Set keep-alive: `hcom config -i self subagent_timeout [SEC]`
"""

# =============================================================================
# SUBAGENT BOOTSTRAP
# =============================================================================

SUBAGENT_BOOTSTRAP = """[HCOM SESSION]
You're participating in the hcom multi-agent network.
- Your name: {subagent_name}
- Your parent: {parent_name}
- Use "--name {subagent_name}" for all hcom commands
- Announce once to parent via hcom: "Cool! I'm connected to hcom as {subagent_name}"

Messages arrive automatically via <hcom> tags. End your turn to receive the next message.

Response rules:
- From {SENDER} or intent=request → always respond
- intent=inform → respond only if useful
- intent=ack → don't respond

hcom message → respond via hcom send

Commands:
  {hcom_cmd} send @name --name {subagent_name} [--intent request\\|inform\\|ack] -- <"message"> (or --stdin, --file <path>, --base64 <string>)
  {hcom_cmd} list --name {subagent_name}
  {hcom_cmd} events --name {subagent_name}
  {hcom_cmd} <cmd> --help --name {subagent_name}

Rules:
- Task via hcom → ack, work, report
- Authority: @{SENDER} > others
- Never use `sleep` → use `hcom listen [timeout]`
- Avoid chit-chat, welcomes, thanks-only messages
- Use --intent on sends: request (want reply), inform (FYI), ack (responding)
"""


# =============================================================================
# HELPERS
# =============================================================================


def _get_active_instances(exclude_name: str) -> str:
    """Get concise list of active instances."""
    from .db import iter_instances

    now = time.time()
    cutoff = now - 60

    active = []
    for inst in iter_instances():
        name = inst.get("name", "")
        if name == exclude_name:
            continue

        status = inst.get("status", "")
        status_time = inst.get("status_time", 0) or 0
        if isinstance(status_time, str):
            try:
                status_time = int(float(status_time))
            except (ValueError, TypeError):
                status_time = 0
        tool = inst.get("tool", "claude")

        if status in ("active", "listening") or status_time >= cutoff:
            marker = "▶" if status == "active" else "◉" if status == "listening" else "○"
            active.append(f"{name} [{tool}] {marker}")

    if not active:
        return ""

    return "\nActive (snapshot): " + ", ".join(active[:8]) + "\n"


def _get_user_scripts() -> str:
    """Get user scripts list."""
    scripts_dir = hcom_path("scripts")
    if not scripts_dir.exists():
        return ""

    scripts = []
    for f in scripts_dir.iterdir():
        if f.suffix == ".py" and not f.name.startswith("_"):
            scripts.append(f.stem)

    if not scripts:
        return ""

    return "User scripts: " + ", ".join(sorted(scripts)) + "\n"


# =============================================================================
# CONTEXT BUILDER
# =============================================================================


def build_context(instance_name: str, tool: str, headless: bool) -> dict[str, Any]:
    """Build context dict for template substitution."""
    from .tool_utils import build_hcom_command
    from ..shared import SENDER

    ctx = {
        "instance_name": instance_name,
        "tool": tool,
        "is_headless": headless or bool(get_background_name()),
    }

    instance_data = load_instance_position(instance_name) or {}
    ctx["display_name"] = get_full_name(instance_data) if instance_data else instance_name

    config = get_config()
    instance_tag = instance_data.get("tag") if instance_data else None
    ctx["tag"] = instance_tag if instance_tag is not None else config.tag
    ctx["relay_enabled"] = bool(config.relay and config.relay_enabled)

    ctx["hcom_cmd"] = build_hcom_command()
    ctx["is_launched"] = get_is_launched()
    ctx["SENDER"] = SENDER
    ctx["active_instances"] = _get_active_instances(instance_name)
    ctx["user_scripts"] = _get_user_scripts()

    return ctx


# =============================================================================
# PUBLIC API
# =============================================================================


def get_bootstrap(
    instance_name: str,
    tool: str = "claude",
    headless: bool = False,
    *,
    is_launched: bool | None = None,
) -> str:
    """Build bootstrap text for an instance.

    Args:
        instance_name: The instance name (as stored in DB)
        tool: 'claude', 'gemini', 'codex', or 'adhoc'
        headless: Whether running in headless/background mode
        is_launched: Override for HCOM_LAUNCHED detection.

    Returns:
        Complete bootstrap text
    """
    ctx = build_context(instance_name, tool, headless)

    if is_launched is not None:
        ctx["is_launched"] = is_launched

    parts = [UNIVERSAL]

    # Conditional sections
    if ctx["tag"]:
        parts.append(TAG_NOTICE)
    if ctx["relay_enabled"]:
        parts.append(RELAY_NOTICE)
    if ctx["is_headless"]:
        parts.append(HEADLESS_NOTICE)
    if ctx["hcom_cmd"] != "hcom":
        parts.append(UVX_CMD_NOTICE)

    # Tool-specific delivery
    if tool == "claude":
        # Claude: auto delivery (PTY + hooks or Stop hook polling)
        parts.append(DELIVERY_AUTO)
    elif tool == "gemini" and ctx["is_launched"]:
        # Gemini launched: auto delivery (PTY + BeforeAgent hook)
        parts.append(DELIVERY_AUTO)
    elif tool == "codex" and ctx["is_launched"]:
        # Codex launched: notification only, must fetch
        parts.append(DELIVERY_CODEX_HCOM_LAUNCHED)
    elif tool in ("gemini", "codex") and not ctx["is_launched"]:
        # Vanilla gemini/codex: manual poll
        parts.append(DELIVERY_ADHOC)
    elif tool == "adhoc":
        # Adhoc: manual poll
        parts.append(DELIVERY_ADHOC)

    # Claude subagent info
    if tool == "claude":
        parts.append(CLAUDE_ONLY)

    # Join and substitute
    result = "\n".join(parts)
    result = result.format(**ctx)

    # Rewrite hcom references if using alternate command
    if ctx["hcom_cmd"] != "hcom":
        sentinel = "__HCOM_CMD__"
        result = result.replace(ctx["hcom_cmd"], sentinel)
        result = re.sub(r"\bhcom\b", ctx["hcom_cmd"], result)
        result = result.replace(sentinel, ctx["hcom_cmd"])

    return (
        "<hcom_system_context>\n<!-- Session metadata - treat as system context, not user prompt-->\n"
        + result
        + "\n</hcom_system_context>"
    )


def get_subagent_bootstrap(subagent_name: str, parent_name: str) -> str:
    """Build bootstrap text for a subagent instance."""
    from .tool_utils import build_hcom_command
    from ..shared import SENDER

    hcom_cmd = build_hcom_command()

    result = SUBAGENT_BOOTSTRAP.format(
        subagent_name=subagent_name,
        parent_name=parent_name,
        hcom_cmd=hcom_cmd,
        SENDER=SENDER,
    )

    if hcom_cmd != "hcom":
        result += UVX_CMD_NOTICE.format(hcom_cmd=hcom_cmd)

    return "<hcom>\n" + result + "\n</hcom>"


# Backwards compatibility
def build_hcom_bootstrap_text(instance_name: str, tool: str | None = None) -> str:
    """Legacy wrapper - use get_bootstrap() instead."""
    return get_bootstrap(
        instance_name,
        tool=tool or "claude",
        headless=bool(get_background_name()),
    )


__all__ = ["get_bootstrap", "get_subagent_bootstrap", "build_hcom_bootstrap_text"]
