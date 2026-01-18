"""Hook system for HCOM"""

from .dispatcher import handle_hook
from .settings import (
    CLAUDE_HOOK_CONFIGS,
    CLAUDE_HOOK_TYPES,
    CLAUDE_HOOK_COMMANDS,
    CLAUDE_HCOM_HOOK_PATTERNS,
    get_claude_settings_path,
    load_claude_settings,
    _remove_claude_hcom_hooks,
    setup_claude_hooks,
    verify_claude_hooks_installed,
    remove_claude_hooks,
)

__all__ = [
    "handle_hook",
    "CLAUDE_HOOK_CONFIGS",
    "CLAUDE_HOOK_TYPES",
    "CLAUDE_HOOK_COMMANDS",
    "CLAUDE_HCOM_HOOK_PATTERNS",
    "get_claude_settings_path",
    "load_claude_settings",
    "_remove_claude_hcom_hooks",
    "setup_claude_hooks",
    "verify_claude_hooks_installed",
    "remove_claude_hooks",
]
