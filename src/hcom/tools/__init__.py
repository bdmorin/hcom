"""Tool-specific integrations for hcom.

Each tool has its own subpackage with hooks and settings specific to that tool.

Structure:
    tools/
    ├── gemini/     # Gemini CLI hooks + settings
    └── codex/      # Codex CLI hooks + settings

Claude Code hooks remain in hooks/ package.
"""

__all__ = ["gemini", "codex"]
