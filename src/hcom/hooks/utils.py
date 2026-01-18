"""Hook utility functions"""

from __future__ import annotations
from typing import Any
from pathlib import Path
import os
import sys
import socket  # noqa: F401 (re-export)

from ..core.paths import hcom_path, LOGS_DIR
from ..core.log import log_info
from ..core.instances import (
    load_instance_position,  # noqa: F401 (re-export)
)
from ..core.runtime import (
    build_claude_env,  # noqa: F401 (re-export)
    build_hcom_bootstrap_text,  # noqa: F401 (re-export)
    notify_all_instances,  # noqa: F401 (re-export)
    notify_instance,  # noqa: F401 (re-export)
)

# Re-export from core.tool_utils
from ..core.tool_utils import (
    build_hcom_command,  # noqa: F401 (re-export)
    build_claude_command,  # noqa: F401 (re-export)
    stop_instance,  # noqa: F401 (re-export)
    _detect_hcom_command_type,  # noqa: F401 (re-export)
    _build_quoted_invocation,  # noqa: F401 (re-export)
)

# Platform detection
IS_WINDOWS = sys.platform == "win32"


def _try_bind_from_transcript(session_id: str, transcript_path: str) -> str | None:
    """Check transcript for [HCOM:BIND:X] marker, create binding if found.

    Handles vanilla instances that ran `!hcom start` (bash shortcut bypasses hooks).
    """
    log_info(
        "hooks",
        "transcript.bind.start",
        session_id=session_id,
        transcript_path=transcript_path,
    )

    if not transcript_path or not session_id:
        log_info(
            "hooks",
            "transcript.bind.skip",
            reason="missing session_id or transcript_path",
        )
        return None

    # Optimization: skip file I/O if no pending instances
    from ..core.db import get_pending_instances

    pending = get_pending_instances()
    if not pending:
        log_info("hooks", "transcript.bind.skip", reason="no pending instances")
        return None

    try:
        content = Path(transcript_path).read_text()
        log_info(
            "hooks",
            "transcript.bind.read",
            content_len=len(content),
            has_hcom_bind="HCOM:BIND" in content,
        )
    except Exception as e:
        log_info("hooks", "transcript.bind.read_error", error=str(e))
        return None

    from ..shared import BIND_MARKER_RE

    matches = BIND_MARKER_RE.findall(content)
    log_info("hooks", "transcript.bind.search", matches=matches)

    if not matches:
        log_info("hooks", "transcript.bind.skip", reason="no marker matches")
        return None

    instance_name = matches[-1]  # Last match = most recent

    # Only bind if instance is in pending list (avoids binding to wrong session)
    if instance_name not in pending:
        log_info(
            "hooks",
            "transcript.bind.skip",
            reason="instance not pending",
            instance=instance_name,
            pending=pending,
        )
        return None

    from ..core.db import rebind_instance_session, get_instance
    from ..core.instances import update_instance_position

    instance = get_instance(instance_name)
    if not instance:
        log_info(
            "hooks",
            "transcript.bind.skip",
            reason="instance not found",
            instance=instance_name,
        )
        return None

    rebind_instance_session(instance_name, session_id)
    update_instance_position(instance_name, {"session_id": session_id})
    log_info("hooks", "transcript.bind.success", instance=instance_name)
    return instance_name


def init_hook_context(
    hook_data: dict[str, Any], hook_type: str | None = None
) -> tuple[str | None, dict[str, Any], bool]:
    """Initialize instance context by binding lookup.

    Uses session_bindings table as sole gate for hook participation.
    No binding = hooks skip (unless transcript has binding marker).
    """
    from ..core.db import get_session_binding, get_instance

    session_id = hook_data.get("session_id", "")
    transcript_path = hook_data.get("transcript_path", "")

    # Session binding is the sole gate for hook participation
    instance_name = get_session_binding(session_id)
    if not instance_name:
        # Fallback: check transcript for binding marker (handles !hcom start)
        instance_name = _try_bind_from_transcript(session_id, transcript_path)
        if not instance_name:
            return None, {}, False

    instance_data = get_instance(instance_name)

    updates: dict[str, Any] = {
        "directory": str(Path.cwd()),
    }

    if transcript_path:
        updates["transcript_path"] = transcript_path

    bg_env = os.environ.get("HCOM_BACKGROUND")
    if bg_env:
        updates["background"] = True
        updates["background_log_file"] = str(hcom_path(LOGS_DIR, bg_env))

    is_matched_resume = bool(
        instance_data and instance_data.get("session_id") == session_id
    )

    return instance_name, updates, is_matched_resume
