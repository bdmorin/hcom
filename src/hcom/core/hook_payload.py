"""Normalized hook input type for Claude/Gemini/Codex.

HookPayload provides a unified interface for hook payloads across tools,
abstracting away format differences:
- Claude: JSON on stdin
- Gemini: JSON on stdin (different key names)
- Codex: JSON in argv[2]

Design:
- Mutable dataclass (may need modification during processing)
- from_*() factory methods normalize tool-specific formats
- raw dict preserved for tool-specific fields
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookPayload:
    """Normalized hook input - same structure for Claude/Gemini/Codex.

    Provides unified access to common fields while preserving raw payload
    for tool-specific data.

    Attributes:
        session_id: Session/thread identifier (Claude session_id, Gemini sessionId, Codex thread-id).
        transcript_path: Path to conversation transcript file.
        hook_type: The hook type being processed (pre, post, sessionstart, etc.).
        tool_name: Name of tool being called (for pre/post tool hooks).
        tool_input: Input parameters to the tool (dict).
        tool_result: Result from tool execution (for post-tool hooks).
        event_type: Event type for Codex (e.g., "agent-turn-complete").
        thread_id: Codex thread ID (also stored in session_id for consistency).
        agent_id: Subagent ID for subagent-start/stop hooks.
        agent_type: Subagent type for subagent-start hook.
        notification_type: Type of notification (for notify hooks).
        raw: Original payload dict for tool-specific access.
    """

    session_id: str | None = None
    transcript_path: str | None = None
    hook_type: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: str | None = None
    event_type: str | None = None  # Codex
    thread_id: str | None = None  # Codex
    agent_id: str | None = None  # Subagent hooks
    agent_type: str | None = None  # Subagent hooks
    notification_type: str | None = None  # Notification hooks
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_claude(cls, stdin_json: dict[str, Any], hook_type: str) -> "HookPayload":
        """Create payload from Claude Code hook stdin.

        Claude hook stdin format:
            {
                "session_id": "...",
                "transcript_path": "...",
                "tool_name": "...",
                "tool_input": {...},
                "tool_response": "...",  # post hooks only
                "agent_id": "...",  # subagent hooks
                "agent_type": "..."  # subagent-start
            }

        Args:
            stdin_json: Parsed JSON from stdin.
            hook_type: Hook type (pre, post, sessionstart, etc.).

        Returns:
            Normalized HookPayload.
        """
        # Tool result can be string or dict with stdout/stderr
        tool_result = stdin_json.get("tool_response")
        if isinstance(tool_result, dict):
            tool_result = tool_result.get("stdout", "")

        return cls(
            session_id=stdin_json.get("session_id") or stdin_json.get("sessionId"),
            transcript_path=stdin_json.get("transcript_path"),
            hook_type=hook_type,
            tool_name=stdin_json.get("tool_name"),
            tool_input=stdin_json.get("tool_input"),
            tool_result=tool_result,
            agent_id=stdin_json.get("agent_id"),
            agent_type=stdin_json.get("agent_type"),
            raw=stdin_json,
        )

    @classmethod
    def from_gemini(cls, stdin_json: dict[str, Any], hook_type: str) -> "HookPayload":
        """Create payload from Gemini CLI hook stdin.

        Gemini hook stdin format (varies by hook):
            {
                "session_id": "..." or "sessionId": "...",
                "transcript_path": "..." or "session_path": "...",
                "tool_name": "..." or "toolName": "...",
                "tool_input": {...},
                "tool_response": {...},  # AfterTool
                "notification_type": "..."  # Notification
            }

        Args:
            stdin_json: Parsed JSON from stdin.
            hook_type: Hook type (gemini-sessionstart, gemini-beforeagent, etc.).

        Returns:
            Normalized HookPayload.
        """
        # Gemini uses alternative key names
        session_id = stdin_json.get("session_id") or stdin_json.get("sessionId")
        transcript_path = stdin_json.get("transcript_path") or stdin_json.get("session_path")
        tool_name = stdin_json.get("tool_name") or stdin_json.get("toolName")

        # Tool response format varies
        tool_result_raw = stdin_json.get("tool_response")
        tool_result = None
        if tool_result_raw:
            if isinstance(tool_result_raw, dict):
                # Gemini format: {"llmContent": "..."} or {"output": "..."}
                tool_result = (
                    tool_result_raw.get("llmContent", "")
                    or tool_result_raw.get("output", "")
                    or tool_result_raw.get("response", {}).get("output", "")
                )
            else:
                tool_result = str(tool_result_raw)

        # Gemini may use toolInput or tool_input
        tool_input = stdin_json.get("tool_input") or stdin_json.get("toolInput")

        return cls(
            session_id=session_id,
            transcript_path=transcript_path,
            hook_type=hook_type,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            notification_type=stdin_json.get("notification_type"),
            raw=stdin_json,
        )

    @classmethod
    def from_codex(cls, argv_json: dict[str, Any], hook_type: str) -> "HookPayload":
        """Create payload from Codex CLI notify hook argv.

        Codex notify payload (passed as argv[2]):
            {
                "type": "agent-turn-complete",
                "thread-id": "uuid",
                "turn-id": "12345",
                "cwd": "/path/to/project",
                "input-messages": ["user prompt"],
                "last-assistant-message": "response text"
            }

        Args:
            argv_json: Parsed JSON from sys.argv[2].
            hook_type: Hook type (codex-notify).

        Returns:
            Normalized HookPayload.
        """
        thread_id = argv_json.get("thread-id")

        return cls(
            session_id=thread_id,  # Use thread-id as session_id for consistency
            transcript_path=argv_json.get("transcript_path") or argv_json.get("session_path"),
            hook_type=hook_type,
            event_type=argv_json.get("type"),
            thread_id=thread_id,
            raw=argv_json,
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Get value from raw payload for tool-specific fields.

        Provides dict-like access to raw payload for fields not in common schema.
        """
        return self.raw.get(key, default)


__all__ = ["HookPayload"]
