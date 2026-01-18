"""Claude Code integration for hcom.

Contains Claude-specific argument parsing and exports.
Claude Code hooks remain in hooks/ package.
"""

from .args import (
    ClaudeArgsSpec,
    resolve_claude_args,
    merge_claude_args,
    add_background_defaults,
    validate_conflicts,
)

__all__ = [
    "ClaudeArgsSpec",
    "resolve_claude_args",
    "merge_claude_args",
    "add_background_defaults",
    "validate_conflicts",
]
