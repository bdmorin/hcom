"""Context injection utilities for daemon bridge pattern.

Provides context managers that temporarily inject HcomContext into the environment
for handlers that still read from os.environ. This is a bridge for Phase 1 - handlers
will be refactored to use ctx directly in Phase 2+.

Usage:
    with inject_context(ctx):
        # Handlers can read os.environ and get ctx values
        handler(payload)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hcom_context import HcomContext


# Env vars to inject from HcomContext
# Only HCOM's own vars - NOT tool detection vars (CLAUDECODE, GEMINI_CLI, CODEX_SANDBOX*)
# Tool detection vars have real meaning and shouldn't be faked
_CONTEXT_ENV_VARS = {
    "HCOM_PROCESS_ID": lambda ctx: ctx.process_id or "",
    "HCOM_LAUNCHED": lambda ctx: "1" if ctx.is_launched else "",
    "HCOM_PTY_MODE": lambda ctx: "1" if ctx.is_pty_mode else "",
    "HCOM_BACKGROUND": lambda ctx: ctx.background_name or "",
    "HCOM_DIR": lambda ctx: str(ctx.hcom_dir),
    "CLAUDE_ENV_FILE": lambda ctx: ctx.claude_env_file or "",
}


@contextmanager
def inject_context(ctx: "HcomContext"):
    """Temporarily inject HcomContext values into os.environ.

    This is a bridge pattern for Phase 1 - existing handlers still read from
    os.environ, so we inject ctx values temporarily. Phase 2+ will refactor
    handlers to use ctx directly.

    Args:
        ctx: Immutable execution context to inject.

    Yields:
        None - handlers can read os.environ during the context.

    Example:
        with inject_context(ctx):
            existing_handler()  # Can read os.environ.get("HCOM_PROCESS_ID")
    """
    old_env: dict[str, str | None] = {}

    try:
        # Save and inject
        for key, getter in _CONTEXT_ENV_VARS.items():
            old_env[key] = os.environ.get(key)
            value = getter(ctx)
            if value:
                os.environ[key] = value
            elif key in os.environ:
                del os.environ[key]
        yield
    finally:
        # Restore
        for key, old_value in old_env.items():
            if old_value is not None:
                os.environ[key] = old_value
            elif key in os.environ:
                del os.environ[key]


__all__ = ["inject_context"]
