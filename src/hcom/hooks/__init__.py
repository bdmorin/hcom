"""Shared hook infrastructure for all tools (Claude, Gemini, Codex).

Tool-specific hook implementations live in their respective tools/ packages:
    tools/claude/  - dispatcher, hooks (handlers), subagent, settings
    tools/gemini/  - hooks, settings
    tools/codex/   - hooks, settings

Shared modules in this package:
    common.py  - Shared handler logic (deliver_pending_messages, finalize_session, etc.)
    family.py  - Message polling, TCP notification, extract_tool_detail
    utils.py   - Context initialization, bootstrap injection, utility re-exports
"""
