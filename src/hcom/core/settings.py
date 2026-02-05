"""TOML settings loader for structured config (~/.hcom/settings.toml).

User-defined terminal presets live here. TOML presets merge with built-in
presets (same-name overrides built-in). Missing file is not an error.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .paths import hcom_path

_settings_cache: dict | None = None


def load_settings() -> dict:
    """Load ~/.hcom/settings.toml. Returns empty dict if missing/invalid."""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache

    path = Path(hcom_path("settings.toml"))
    if not path.exists():
        _settings_cache = {}
        return _settings_cache

    try:
        _settings_cache = tomllib.loads(path.read_text())
    except Exception:
        _settings_cache = {}
    return _settings_cache


def invalidate_settings_cache() -> None:
    global _settings_cache
    _settings_cache = None


def get_merged_presets() -> dict[str, dict]:
    """Return built-in presets merged with TOML-defined presets.

    TOML presets override same-name built-in presets.
    TOML preset requires at minimum an 'open' field.
    """
    from ..shared import TERMINAL_PRESETS

    merged = dict(TERMINAL_PRESETS)

    settings = load_settings()
    terminal_section = settings.get("terminal", {})
    if not isinstance(terminal_section, dict):
        return merged

    for name, toml_preset in terminal_section.items():
        if not isinstance(toml_preset, dict):
            continue
        if "open" not in toml_preset:
            continue
        # Start from built-in (preserves all fields like pane_id_env, app_name),
        # then overlay TOML values
        builtin = merged.get(name, {})
        merged[name] = {
            **builtin,
            "binary": toml_preset.get("binary", builtin.get("binary")),
            "open": toml_preset["open"],
            "close": toml_preset.get("close", builtin.get("close")),
            "platforms": toml_preset.get("platforms", builtin.get("platforms", ["Darwin", "Linux"])),
        }

    return merged


def get_merged_preset(name: str) -> dict | None:
    """Get a single preset by name, with TOML overrides applied."""
    return get_merged_presets().get(name)
