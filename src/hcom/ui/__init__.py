"""HCOM TUI - Interactive Menu Interface"""

from pathlib import Path
from .types import Field, Mode, LaunchField, UIState
from .rendering import (
    ANSI_RE,
    MAX_INPUT_ROWS,
    ansi_len,
    ansi_ljust,
    bg_ljust,
    truncate_ansi,
    get_terminal_size,
    get_message_pulse_colors,
    smart_truncate_name,
    AnsiTextWrapper,
    ease_out_quad,
    interpolate_color_index,
    separator_line,
)
from .input import (
    KeyboardInput,
    text_input_insert,
    text_input_backspace,
    text_input_move_left,
    text_input_move_right,
    calculate_text_input_rows,
    render_text_input,
    IS_WINDOWS,
)

# UI-specific colors
FG_CLAUDE_ORANGE = "\033[38;5;214m"  # Light orange for Claude section
FG_CUSTOM_ENV = "\033[38;5;141m"  # Light purple for Custom Env section

# Parse config defaults lazily to avoid circular import
# (shared.py imports ui/colors.py, so ui/__init__.py can't import from shared at module level)
_CONFIG_DEFAULTS_CACHE: dict[str, str] | None = None


def _get_config_defaults() -> dict[str, str]:
    """Lazy-load config defaults from shared."""
    global _CONFIG_DEFAULTS_CACHE
    if _CONFIG_DEFAULTS_CACHE is None:
        from ..shared import DEFAULT_CONFIG_DEFAULTS

        _CONFIG_DEFAULTS_CACHE = {}
        for line in DEFAULT_CONFIG_DEFAULTS:
            if "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                # Remove only outer layer of quotes
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                _CONFIG_DEFAULTS_CACHE[key.strip()] = value
    return _CONFIG_DEFAULTS_CACHE


# Use module __getattr__ for lazy access to CONFIG_DEFAULTS
def __getattr__(name: str):
    if name == "CONFIG_DEFAULTS":
        return _get_config_defaults()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Config field special handlers
def _get_terminal_options():
    """Get terminal preset options (cached). Returns list of preset names."""
    from ..terminal import get_available_presets

    return [name for name, available in get_available_presets()]


CONFIG_FIELD_OVERRIDES = {
    # HCOM_TIMEOUT: hidden from TUI (internal, headless/vanilla only)
    "HCOM_SUBAGENT_TIMEOUT": {
        "type": "numeric",
        "min": 1,
        "max": 86400,
        "hint": "1-86400 seconds",
    },
    "HCOM_TERMINAL": {
        "type": "cycle",
        "options": _get_terminal_options,  # Callable, evaluated lazily
        "hint": "←→ cycle presets",
    },
    "HCOM_HINTS": {
        "type": "text",
        "hint": "text string",
    },
    "HCOM_TAG": {
        "type": "text",
        "allowed_chars": r"^[a-zA-Z0-9-]*$",
        "hint": "letters/numbers/hyphens only",
    },
    "HCOM_RELAY": {
        "type": "text",
        "hint": "relay server URL",
    },
    "HCOM_RELAY_TOKEN": {
        "type": "text",
        "hint": "auth token for relay",
    },
    "HCOM_CODEX_SANDBOX_MODE": {
        "type": "cycle",
        "options": ["workspace", "untrusted", "danger-full-access", "none"],
        "hint": "←→ cycle Codex sandbox mode",
    },
    "HCOM_RELAY_ENABLED": {
        "type": "checkbox",
        "hint": "enter to toggle",
    },
    "HCOM_AUTO_APPROVE": {
        "type": "checkbox",
        "hint": "auto-approve safe hcom commands",
    },
}


def run_tui(hcom_dir: Path) -> int:
    """Public API: run TUI application (lazy import to avoid circular dependency)"""
    from .tui import HcomTUI

    tui = HcomTUI(hcom_dir)
    return tui.run()


__all__ = [
    # Types
    "Field",
    "Mode",
    "LaunchField",
    "UIState",
    # Colors
    "FG_CLAUDE_ORANGE",
    "FG_CUSTOM_ENV",
    # Config
    "CONFIG_DEFAULTS",
    "CONFIG_FIELD_OVERRIDES",
    # Rendering
    "ANSI_RE",
    "MAX_INPUT_ROWS",
    "ansi_len",
    "ansi_ljust",
    "bg_ljust",
    "truncate_ansi",
    "get_terminal_size",
    "get_message_pulse_colors",
    "smart_truncate_name",
    "AnsiTextWrapper",
    "ease_out_quad",
    "interpolate_color_index",
    "separator_line",
    # Input
    "KeyboardInput",
    "text_input_insert",
    "text_input_backspace",
    "text_input_move_left",
    "text_input_move_right",
    "calculate_text_input_rows",
    "render_text_input",
    "IS_WINDOWS",
    # Public API
    "run_tui",
]
