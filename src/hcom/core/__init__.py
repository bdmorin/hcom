"""Core package - fundamental components without circular dependencies.

Extracted modules here avoid circular imports between config and commands.
"""

from .identity import resolve_identity

__all__ = [
    "resolve_identity",
]
