"""Hook dispatcher - single entry point with clean parent/subagent separation"""

from __future__ import annotations
from typing import Any
import sys
import os
import json
import re
from pathlib import Path

from ..core.paths import ensure_hcom_directories
from ..core.instances import load_instance_position
from ..core.db import get_db
from . import subagent, parent
from .utils import init_hook_context
from ..core.log import log_error, log_info


def handle_hook(hook_type: str) -> None:
    """Hook dispatcher with clean parent/subagent separation

    Error handling strategy:
    - Non-participants (no instance row): exit 0 silently to avoid leaking errors
      into normal Claude Code usage when user has hcom installed but not using it
    - Participants (row exists): errors surface normally
    """

    # catches pre-gate errors (before we know if instance exists).
    try:
        _handle_hook_impl(hook_type)
    except Exception as e:
        # Pre-gate error (before instance context resolved) - must be silent
        # because we don't know if user is even using hcom
        log_error("hooks", "hook.error", e, hook=hook_type)
        sys.exit(0)


def _handle_hook_impl(hook_type: str) -> None:
    """Hook dispatcher implementation"""

    # ============ SETUP, LOAD, SYNC (BOTH CONTEXTS) ============
    # Note: Permission approval now handled via settings.json permissions.allow
    # (see hooks/settings.py CLAUDE_HCOM_PERMISSIONS)

    hook_data = json.load(sys.stdin)
    tool_name = hook_data.get("tool_name", "")

    # Debug: log all hook invocations to trace Task handling
    log_info("hooks", "dispatcher.entry", hook=hook_type, tool=tool_name or "(none)")

    # Get real session_id from CLAUDE_ENV_FILE path (workaround for CC fork bug)
    # CC passes wrong session_id in hook_data for --fork-session scenarios
    from .parent import get_real_session_id

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    session_id = get_real_session_id(hook_data, env_file)

    # Store corrected session_id back into hook_data for downstream functions
    hook_data["session_id"] = session_id

    if not ensure_hcom_directories():
        log_error("hooks", "hook.error", "failed to create directories")
        sys.exit(0)

    get_db()

    # ============ TASK TRANSITIONS (PARENT CONTEXT) ============

    # Task start - enter subagent context and inject hcom hint into prompt
    if hook_type == "pre" and tool_name == "Task":
        log_info("hooks", "dispatcher.task_pre", session_id=session_id)
        updated_input = parent.start_task(session_id, hook_data)
        if updated_input:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": updated_input,
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # Task end - deliver freeze messages (SubagentStop handles cleanup)
    if hook_type == "post" and tool_name == "Task":
        parent.end_task(session_id, hook_data, interrupted=False)
        sys.exit(0)

    # ============ SUBAGENT CONTEXT HOOKS ============

    is_in_subagent_ctx = subagent.in_subagent_context(session_id)
    log_info(
        "hooks",
        "dispatcher.subagent_check",
        hook=hook_type,
        session_id=session_id,
        in_subagent_context=is_in_subagent_ctx,
    )

    # Log when SubagentStart is skipped due to no subagent context
    if hook_type == "subagent-start" and not is_in_subagent_ctx:
        log_info(
            "hooks",
            "dispatcher.subagent_start_skipped",
            session_id=session_id,
            reason="not_in_subagent_context",
        )

    if is_in_subagent_ctx:
        # UserPromptSubmit: check for dead subagents (interrupt detection)
        if hook_type == "userpromptsubmit":
            transcript_path = hook_data.get("transcript_path", "")
            subagent.cleanup_dead_subagents(session_id, transcript_path)
            # Fall through to parent handling

        # SubagentStart/SubagentStop: have agent_id in payload
        match hook_type:
            case "subagent-start":
                agent_id = hook_data.get("agent_id")
                agent_type = hook_data.get("agent_type")
                log_info(
                    "hooks",
                    "dispatcher.subagent_start",
                    agent_id=agent_id,
                    agent_type=agent_type,
                    session_id=session_id,
                    is_in_ctx=is_in_subagent_ctx,
                )
                subagent.track_subagent(session_id, agent_id, agent_type)
                subagent.subagent_start(hook_data)
                sys.exit(0)
            case "subagent-stop":
                subagent.subagent_stop(hook_data)
                sys.exit(0)

        # Pre/Post: require explicit --name
        if hook_type in ("pre", "post") and tool_name == "Bash":
            tool_input = hook_data.get("tool_input", {})
            command = tool_input.get("command", "")
            name_value = _extract_name(command)

            if name_value:
                # Identified subagent
                if hook_type == "post":
                    subagent.posttooluse(hook_data, "", None)
                sys.exit(0)
            else:
                # No identity - skip silently
                sys.exit(0)
        elif hook_type in ("pre", "post"):
            # Non-Bash pre/post during subagent context: skip
            sys.exit(0)

        # Other hooks (poll, notify, sessionend) fall through to parent

    # ============  PARENT INSTANCE HOOKS ============

    if hook_type == "sessionstart":
        parent.sessionstart(hook_data)
        sys.exit(0)

    # Resolve instance for parent hooks
    instance_name, updates, is_matched_resume = init_hook_context(hook_data, hook_type)

    # Vanilla binding: parse [HCOM:BIND:X] marker from PostToolUse Bash output
    if hook_type == "post" and tool_name == "Bash":
        bound_name = _bind_vanilla_from_marker(hook_data, session_id, instance_name)
        if bound_name:
            instance_name = bound_name
            updates = updates or {}
            updates.setdefault("directory", str(Path.cwd()))
            transcript_path = hook_data.get("transcript_path", "")
            if transcript_path:
                updates.setdefault("transcript_path", transcript_path)

    if not instance_name:
        sys.exit(0)
    instance_data = load_instance_position(instance_name)

    # Participation gate: row exists = participating
    if not instance_data:
        sys.exit(0)

    match hook_type:
        case "pre":
            parent.pretooluse(hook_data, instance_name, tool_name)
        case "post":
            parent.posttooluse(hook_data, instance_name, instance_data, updates)
        case "poll":
            parent.stop(instance_name, instance_data)
        case "notify":
            parent.notify(hook_data, instance_name, updates, instance_data)
        case "userpromptsubmit":
            parent.userpromptsubmit(
                hook_data, instance_name, updates, is_matched_resume, instance_data
            )
        case "sessionend":
            parent.sessionend(hook_data, instance_name, updates)

    sys.exit(0)


def _extract_name(command: str) -> str | None:
    """Extract --name value from command string

    Returns:
        identity string if --name <name_or_uuid>
        None if no --name flag
    """
    match = re.search(r"--name\s+(\S+)", command)
    if match:
        return match.group(1)
    return None


def _bind_vanilla_from_marker(
    hook_data: dict[str, Any], session_id: str, current_instance: str | None
) -> str | None:
    """Parse [HCOM:BIND:X] marker from Bash tool_response and create session binding.

    Called when PostToolUse has no session binding but may have just run hcom start.
    Returns instance name if bound, None otherwise.
    """
    from ..shared import BIND_MARKER_RE
    from ..core.db import get_pending_instances

    # Skip if no pending instances (optimization)
    if not get_pending_instances():
        return None

    tool_response = hook_data.get("tool_response", "")
    if not tool_response:
        return None

    # tool_response can be dict with stdout/stderr or string
    if isinstance(tool_response, dict):
        tool_response = tool_response.get("stdout", "")
    if not tool_response:
        return None

    # Search for binding marker in tool output
    match = BIND_MARKER_RE.search(tool_response)
    if not match:
        return None

    instance_name = match.group(1)

    if not session_id:
        return current_instance or instance_name

    try:
        from ..core.db import rebind_instance_session, get_instance
        from ..core.instances import update_instance_position

        # Verify instance exists
        if not get_instance(instance_name):
            log_error(
                "hooks", "bind.fail", "instance not found", instance=instance_name
            )
            return None

        rebind_instance_session(instance_name, session_id)
        log_info("hooks", "bind.session", instance=instance_name, session_id=session_id)

        # Update instance with session_id and mark as Claude (vanilla Claude binding)
        update_instance_position(
            instance_name, {"session_id": session_id, "tool": "claude"}
        )

        return instance_name
    except Exception as e:
        log_error("hooks", "bind.fail", e)
        return None
