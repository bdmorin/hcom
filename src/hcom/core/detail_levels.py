"""Transcript detail level definitions and validation for HCOM.

Detail levels control how transcript content is displayed:
- normal: Truncated output (default behavior)
- full: Complete text with --full flag
- detailed: Complete text with tool I/O using --detailed flag
"""

from __future__ import annotations

from typing import Literal

# Type for valid detail levels
DetailLevel = Literal["normal", "full", "detailed"]

# Valid detail levels (single source of truth)
VALID_DETAIL_LEVELS: set[str] = {"normal", "full", "detailed"}

# Mapping from detail level to transcript command flags
DETAIL_TO_FLAGS: dict[str, str] = {
    "normal": "",  # Default, no flag
    "full": "--full",  # Complete text
    "detailed": "--detailed",  # Complete text with tool I/O
}

# Mapping from detail level to description
DETAIL_DESCRIPTIONS: dict[str, str] = {
    "normal": "truncated (default)",
    "full": "complete text",
    "detailed": "complete text with tool I/O and edits",
}


def validate_detail_level(detail: str) -> None:
    """Validate a detail level string.

    Args:
        detail: Detail level to validate

    Raises:
        ValueError: If detail level is invalid with helpful message
    """
    if detail not in VALID_DETAIL_LEVELS:
        valid_list = ", ".join(sorted(VALID_DETAIL_LEVELS))
        flag_mappings = " | ".join(
            f"{level}={DETAIL_TO_FLAGS[level] or 'default'}" for level in sorted(VALID_DETAIL_LEVELS)
        )
        raise ValueError(
            f"Invalid detail level '{detail}'. "
            f"Must be one of: {valid_list} "
            f"(maps to hcom transcript flags: {flag_mappings})"
        )


def is_full_output_detail(detail: str) -> bool:
    """Check if detail level requires full output (not truncated).

    Args:
        detail: Detail level (should be validated first)

    Returns:
        True if detail level is 'full' or 'detailed'
    """
    return detail in ("full", "detailed")


def get_detail_help_text() -> str:
    """Get help text describing detail levels.

    Returns:
        Formatted help text for CLI usage
    """
    return "Format: range:detail (e.g., 3-14:normal,6:full,22-30:detailed)"


def get_detail_mapping_text() -> str:
    """Get mapping description for help text.

    Returns:
        Text explaining detail level to flag mapping
    """
    return "normal = truncated | full = --full flag | detailed = --detailed flag"


def get_detail_json_example() -> str:
    """Get JSON example for help text.

    Returns:
        JSON array example string
    """
    return '["10-15:normal", "20:full", "30-35:detailed"]'


__all__ = [
    "DetailLevel",
    "VALID_DETAIL_LEVELS",
    "DETAIL_TO_FLAGS",
    "DETAIL_DESCRIPTIONS",
    "validate_detail_level",
    "is_full_output_detail",
    "get_detail_help_text",
    "get_detail_mapping_text",
    "get_detail_json_example",
]
