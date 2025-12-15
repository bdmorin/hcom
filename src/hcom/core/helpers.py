"""Helper functions for scope-based message routing"""
from __future__ import annotations

from typing import Any

# Valid scope values for message routing
# - broadcast: No @mentions → everyone
# - mentions: Has @targets → explicit targets only
VALID_SCOPES = {'broadcast', 'mentions'}

# Valid intent values for message envelope
# - request: Response expected from recipient
# - inform: FYI, no action needed
# - ack: Explicit acknowledgment (requires reply_to)
# - error: Terminal failure, stop retrying
VALID_INTENTS = {'request', 'inform', 'ack', 'error'}

def get_group_session_id(instance_data: dict[str, Any] | None) -> str | None:
    """Get the session_id that defines this instance's group.
    For parents: their own session_id, for subagents: parent_session_id
    """
    if not instance_data:
        return None
    # Subagent - use parent_session_id
    if parent_sid := instance_data.get('parent_session_id'):
        return parent_sid
    # Parent - use own session_id
    return instance_data.get('session_id')

def in_same_group_by_id(group_id: str | None, receiver_data: dict[str, Any] | None) -> bool:
    """Check if receiver is in the same group as the given group_id.

    Args:
        group_id: The sender's group ID (session_id)
        receiver_data: Receiver instance data

    Returns:
        True if receiver is in same group, False otherwise
    """
    if not group_id or not receiver_data:
        return False
    receiver_group = get_group_session_id(receiver_data)
    if not receiver_group:
        return False
    return group_id == receiver_group

def validate_scope(scope: str) -> None:
    """Validate that scope is a valid value.

    Args:
        scope: Scope value to validate

    Raises:
        ValueError: If scope is not in VALID_SCOPES
    """
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"Invalid scope '{scope}'. Must be one of: {', '.join(sorted(VALID_SCOPES))}"
        )

def validate_intent(intent: str) -> None:
    """Validate that intent is a valid value.

    Args:
        intent: Intent value to validate

    Raises:
        ValueError: If intent is not in VALID_INTENTS
    """
    if intent not in VALID_INTENTS:
        raise ValueError(
            f"Invalid intent '{intent}'. Must be one of: {', '.join(sorted(VALID_INTENTS))}"
        )

def is_mentioned(text: str, name: str, tag: str | None = None) -> bool:
    """Check if instance is @-mentioned in text using prefix matching on full name.

    Uses same prefix matching logic as compute_scope() for consistency.
    Full name is '{tag}-{name}' if tag exists, else just '{name}'.

    Matching rules:
    - @api-alice matches full name "api-alice" (exact or prefix)
    - @api- matches all instances with tag "api" (full name starts with "api-")
    - @alice matches base name "alice" (when no tag, or as base name match)
    - Underscore blocks prefix expansion (reserved for subagent hierarchy)
    - Bare mentions exclude remote instances (no : in name)

    Args:
        text: Text to search in
        name: Instance base name to check (without @ prefix)
        tag: Optional tag (if present, full name is '{tag}-{name}')

    Returns:
        True if @mention prefix-matches full name, False otherwise

    Examples:
        >>> is_mentioned("Hey @api-alice", "alice", "api")
        True
        >>> is_mentioned("Hey @api-", "alice", "api")  # prefix match
        True
        >>> is_mentioned("Hey @alice", "alice", "api")  # base name match
        True
        >>> is_mentioned("Hey @alice", "alice")  # no tag
        True
        >>> is_mentioned("Hey @review-alice", "alice", "api")  # wrong tag
        False
    """
    from ..shared import MENTION_PATTERN

    # Build full name
    full_name = f"{tag}-{name}" if tag else name

    # Extract all @mentions from text
    mentions = MENTION_PATTERN.findall(text)

    # Check if any mention prefix-matches the full name (case-insensitive)
    # Respects local/remote distinction: bare mentions only match local instances
    for mention in mentions:
        if ':' in mention:
            # Remote mention - match any instance with prefix
            if full_name.lower().startswith(mention.lower()):
                return True
        else:
            # Bare mention - only match local instances (no : in full name)
            # Don't match across underscore boundary (reserved for subagent hierarchy)
            if ':' not in full_name and full_name.lower().startswith(mention.lower()):
                if len(full_name) == len(mention) or full_name[len(mention)] != '_':
                    return True
            # Also check base name match (e.g., @alice matches api-alice)
            if ':' not in name and name.lower().startswith(mention.lower()):
                if len(name) == len(mention) or name[len(mention)] != '_':
                    return True

    return False

__all__ = [
    'VALID_SCOPES',
    'VALID_INTENTS',
    'get_group_session_id',
    'in_same_group_by_id',
    'validate_scope',
    'validate_intent',
    'is_mentioned',
]
