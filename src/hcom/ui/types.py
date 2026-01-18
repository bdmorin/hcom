"""UI type definitions"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Literal


@dataclass
class Field:
    """Field representation for rendering expandable sections"""

    key: str
    display_name: str
    field_type: Literal["checkbox", "text", "cycle", "numeric"]
    value: str | bool
    options: List[str] | None = None
    hint: str = ""


class Mode(Enum):
    MANAGE = "manage"
    LAUNCH = "launch"


class LaunchField(Enum):
    TOOL = 0
    COUNT = 1
    LAUNCH_BTN = 2
    CLAUDE_SECTION = 3
    HCOM_SECTION = 4
    CUSTOM_ENV_SECTION = 5
    OPEN_EDITOR = 6


@dataclass
class UIState:
    """Shared state accessed by screen classes and HcomTUI orchestrator"""

    # Manage screen state
    cursor: int = 0
    cursor_instance_name: Optional[str] = None
    instances: dict = field(default_factory=dict)
    status_counts: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    message_buffer: str = ""
    message_cursor_pos: int = 0
    instance_scroll_pos: int = 0
    show_instance_detail: Optional[str] = (
        None  # Instance name to show detail for (cleared on cursor move/ESC)
    )
    show_stopped: bool = False  # Whether to show stopped (disabled) instances in list
    show_remote: bool = (
        False  # Whether to show remote (synced from other devices) instances
    )
    show_stopped_user_set: bool = False  # User explicitly toggled stopped section
    show_remote_user_set: bool = False  # User explicitly toggled remote section
    device_sync_times: dict = field(
        default_factory=dict
    )  # device_id -> last_import_time (for sync pulse)

    # Rendering optimization
    frame_dirty: bool = True  # Frame needs rebuild (set when data/cursor/input changes)

    # Launch screen state
    launch_tool: str = "claude"  # claude | gemini | codex
    launch_count: str = "1"
    launch_prompt: str = ""
    launch_system_prompt: str = ""
    launch_append_system_prompt: str = ""
    launch_background: bool = False
    launch_gemini_prompt: str = ""  # Gemini: prompt (persisted to HCOM_GEMINI_ARGS)
    launch_gemini_prompt_cursor: int = 0
    launch_codex_prompt: str = ""  # Codex: prompt (persisted to HCOM_CODEX_ARGS)
    launch_codex_prompt_cursor: int = 0
    launch_codex_system_prompt: str = ""  # Codex: system prompt (written to temp file)
    launch_codex_system_prompt_cursor: int = 0
    launch_codex_sandbox_mode: str = (
        "workspace"  # Codex sandbox: workspace, untrusted, danger-full-access, none
    )
    launch_gemini_system_prompt: str = ""  # Gemini: system prompt (persistent file)
    launch_gemini_system_prompt_cursor: int = 0
    launch_field: LaunchField = LaunchField.COUNT
    launch_prompt_cursor: int = 0
    launch_system_prompt_cursor: int = 0
    launch_append_system_prompt_cursor: int = 0
    config_field_cursors: dict = field(default_factory=dict)

    # Config state
    config_edit: dict = field(default_factory=dict)
    config_mtime: float = 0.0

    # Section expansion
    claude_expanded: bool = False
    hcom_expanded: bool = False
    custom_env_expanded: bool = False
    claude_cursor: int = -1
    hcom_cursor: int = -1
    custom_env_cursor: int = -1

    # Confirmation state
    pending_stop: Optional[str] = None  # Two-step stop confirmation
    pending_stop_time: float = 0.0
    pending_stop_all: bool = False
    pending_stop_all_time: float = 0.0
    pending_reset: bool = False
    pending_reset_time: float = 0.0

    # Launch scrolling
    launch_scroll_pos: int = 0

    # Flash notifications
    flash_message: Optional[str] = None
    flash_until: float = 0.0
    flash_color: str = "orange"

    # Validation
    validation_errors: dict = field(default_factory=dict)

    # Rendering cache
    last_event_id: int = 0
    last_message_time: float = 0.0
    unread_counts: dict = field(default_factory=dict)  # {instance_name: unread_count}

    # EVENTS filtering
    event_filter: str = ""  # Current filter query (empty = inactive)
    event_filter_cursor: int = 0  # Cursor position in filter input
    event_type_filter: str = (
        "all"  # Event type filter: "all", "message", "status", "life"
    )
    event_instances_view: bool = (
        False  # Toggle: show instances summary instead of events
    )
    event_instances_cursor: int = 0  # Cursor position in instances list
    event_instances_list: list = field(
        default_factory=list
    )  # Cached instance names for cursor nav
    event_instances_data: list = field(
        default_factory=list
    )  # Full instance data (name, started, stopped)

    # Archive viewing state (Ctrl+G in events view)
    event_archive_mode: bool = False  # True = viewing archive events
    event_archive_index: int = 0  # 1-based archive index (0 = none selected)
    event_archive_picker: bool = False  # True = showing archive picker overlay
    event_archive_list: list = field(
        default_factory=list
    )  # Cached archive list for picker
    event_archive_cursor: int = 0  # Cursor position in archive picker

    # Send state (for inline feedback)
    send_state: Optional[str] = None  # None, 'sending', 'sent'
    send_state_until: float = 0.0  # When to clear 'sent' state

    # Relay status (for status bar indicator)
    relay_configured: bool = False  # Relay URL is set
    relay_enabled: bool = True  # Relay sync enabled
    relay_status: Optional[str] = None  # 'ok' | 'error' | None
    relay_error: Optional[str] = None  # Last error message

    # Archive count (shown when no instances)
    archive_count: int = 0
