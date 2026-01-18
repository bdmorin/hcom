"""Manage mode screen implementation"""

from __future__ import annotations
import re
import time
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from .tui import HcomTUI
    from .types import UIState

# Import rendering utilities
from .rendering import (
    ansi_len,
    ansi_ljust,
    bg_ljust,
    truncate_ansi,
    smart_truncate_name,
    get_terminal_size,
    AnsiTextWrapper,
    get_device_sync_color,
    separator_line,
)

# Import input utilities
from .input import (
    render_text_input,
    calculate_text_input_rows,
    text_input_insert,
    text_input_backspace,
    text_input_move_left,
    text_input_move_right,
)

# Import ANSI codes directly to avoid circular import
from .colors import (
    RESET,
    BOLD,
    DIM,
    FG_WHITE,
    FG_GRAY,
    FG_YELLOW,
    FG_LIGHTGRAY,
    FG_ORANGE,
    FG_DELIVER,
    FG_CYAN,
    FG_BLACK,
    BG_CHARCOAL,
    BG_GRAY,
    BG_GOLD,
)

# Import non-color constants from shared
from ..shared import (
    STATUS_FG,
    format_age,
    shorten_path,
    parse_iso_timestamp,
)
from ..core.instances import get_status_icon

# Import from source modules
from ..core.config import get_config
from ..commands.messaging import cmd_send
from ..commands.lifecycle import cmd_stop

import sys
from contextlib import contextmanager
from io import StringIO


@contextmanager
def _suppress_output():
    """Capture stdout/stderr to prevent CLI output from corrupting TUI display"""
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        yield
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


class ManageScreen:
    """Manage mode: instance list + messages + input"""

    def __init__(self, state: UIState, tui: HcomTUI):
        self.state = state  # Shared state (explicit dependency)
        self.tui = tui  # For commands only (flash, stop_all, etc)
        self._recently_stopped_row_pos = -1  # Track position of recently stopped row

    def _render_instance_row(
        self,
        name: str,
        info: dict,
        display_idx: int,
        name_col_width: int,
        width: int,
        is_remote: bool = False,
        show_tool: bool = False,
        project_tag: str = "",
    ) -> str:
        """Render a single instance row"""
        from ..core.instances import is_launching_placeholder

        # Row exists = participating (no enabled field)
        status = info.get("status", "unknown")

        # Use get_status_icon for adhoc-aware icon selection
        instance_data = info.get("data", {})
        icon = get_status_icon(instance_data, status) if instance_data else "?"

        # Mask name for launching placeholders (temp name that will change during resume)
        if is_launching_placeholder(instance_data):
            name = "· · ·"
        color = STATUS_FG.get(status, FG_WHITE)

        # Get binding status (needed for both tool prefix and timeout warning)
        from ..core.db import get_instance_bindings

        base_name = info.get("base_name", name)
        bindings = get_instance_bindings(base_name)

        # Tool prefix info (only when multiple tool types exist)
        tool_prefix_info = None
        if show_tool:
            tool = info.get("data", {}).get("tool", "claude")
            # Tool colors: brand-aligned, dark/muted for subtlety
            tool_colors = {
                "claude": "\033[48;5;94m",  # Dark rust/brown (Anthropic)
                "gemini": "\033[48;5;54m",  # Dark purple (Google Gemini)
                "codex": "\033[48;5;23m",  # Dark teal (OpenAI/ChatGPT)
            }
            tool_bg = tool_colors.get(tool, BG_GRAY)
            # Display based on binding: UPPER=pty+hooks, lower=hooks, UPPER*=pty only, lower*=none
            if bindings["process_bound"] and bindings["hooks_bound"]:
                tool_display = tool.upper()  # CLAUDE - pty + hooks
            elif bindings["process_bound"]:
                tool_display = tool.upper() + "*"  # CLAUDE* - pty only (unusual)
            elif bindings["hooks_bound"]:
                tool_display = tool.lower()  # claude - hooks only
            elif tool != "adhoc":
                tool_display = tool.lower() + "*"  # claude* - no binding
            else:
                tool_display = "ad-hoc"  # ad-hoc tool type
            tool_label = tool_display[:7].ljust(7)  # Pad to 7 chars (for CLAUDE*)
            tool_prefix_info = (tool_bg, tool_label)

        # Light green coloring for message delivery (active with deliver token)
        status_context = info.get("data", {}).get("status_context", "")
        if status == "active" and status_context.startswith("deliver:"):
            color = FG_DELIVER

        display_text = info.get("description", "")

        # Append "since X" for listening agents idle >= 1 minute
        if status == "listening" and display_text == "listening":
            from ..shared import format_listening_since

            status_time = info.get("data", {}).get("status_time", 0)
            display_text += format_listening_since(status_time)

        # Gold background for tui:* blocking status (gate blocked for 2+ seconds)
        if status == "listening" and status_context.startswith("tui:") and display_text:
            display_text = f"{BG_GOLD}{FG_BLACK} {display_text} {RESET}"

        age_text = info.get("age_text", "")
        # "now" special case (listening status uses age=0)
        age_str = (
            age_text if age_text == "now" else (f"{age_text} ago" if age_text else "")
        )
        age_padded = age_str.rjust(10)

        # Badges
        is_background = info.get("data", {}).get("background", False)
        badges = ""
        if is_background:
            badges += " [headless]"

        # Project tag (shown when instances have different directories)
        project_suffix = ""
        if project_tag:
            project_suffix = f" {DIM}· {project_tag}"

        badge_visible_len = ansi_len(badges) + ansi_len(project_suffix)

        # Unread count - shown as left border indicator (count in detail view)
        unread_count = self.state.unread_counts.get(name, 0)

        # Timeout warning for:
        # 1. Subagents (any tool) - always show, they have limited lifetime
        # 2. Claude hooks-only or headless - show for short timeouts (<1hr)
        timeout_marker = ""
        tool = info.get("data", {}).get("tool", "claude")
        data = info.get("data", {})
        age_seconds = info.get("age_seconds", 0)
        is_subagent = bool(data.get("parent_session_id"))

        # Subagents always show timeout warning (regardless of tool/binding)
        if status == "listening" and is_subagent:
            # Use parent's subagent_timeout if set, else global config
            parent_name = data.get("parent_name")
            timeout = None
            if parent_name:
                from ..core.instances import load_instance_position

                parent_data = load_instance_position(parent_name)
                if parent_data:
                    timeout = parent_data.get("subagent_timeout")
            if timeout is None:
                timeout = get_config().subagent_timeout
            remaining = timeout - age_seconds
            if 0 < remaining < 10:
                timeout_marker = f" {FG_YELLOW}⏱ {int(remaining)}s{RESET}"
        # Non-subagent Claude instances: show countdown for short timeouts (<1hr)
        elif status == "listening" and tool == "claude":
            is_hooks_only = bindings["hooks_bound"] and not bindings["process_bound"]
            is_headless = data.get("background", False)
            if is_hooks_only or is_headless:
                timeout = data.get("wait_timeout", get_config().timeout)
                if timeout < 3600:  # Only show countdown for <1hr timeouts
                    remaining = timeout - age_seconds
                    if 0 < remaining < 60:
                        timeout_marker = f" {FG_YELLOW}⏱ {int(remaining)}s{RESET}"

        max_name_len = name_col_width - badge_visible_len - 2
        display_name = smart_truncate_name(name, max_name_len)

        colored_name = display_name
        name_with_marker = f"{colored_name}{badges}{project_suffix}"
        name_padded = ansi_ljust(name_with_marker, name_col_width)

        desc_sep = ": " if display_text else ""
        weight = BOLD

        # Build tool prefix
        tool_prefix = ""
        if tool_prefix_info:
            tool_bg, tool_label = tool_prefix_info
            tool_prefix = f"{tool_bg}{FG_WHITE} {tool_label}{RESET}"

        # Left border indicators: orange for detail open, yellow for unread
        # Indicator adds 1 char width (pushes content right) - matches original detail behavior
        is_detail_open = self.state.show_instance_detail == name
        has_unread = unread_count > 0
        if display_idx == self.state.cursor:
            # Cursor row with charcoal background
            if is_detail_open:
                border = f"{FG_ORANGE}▐{RESET}{BG_CHARCOAL} "
            elif has_unread:
                border = f"{FG_YELLOW}▐{RESET}{BG_CHARCOAL} "
            else:
                border = f"{BG_CHARCOAL} "
            line = f"{tool_prefix}{border}{color}{icon} {weight}{color}{name_padded}{RESET}{BG_CHARCOAL}{weight}{FG_GRAY}{age_padded}{desc_sep}{display_text}{timeout_marker}{RESET}"
            line = truncate_ansi(line, width)
            line = bg_ljust(line, width, BG_CHARCOAL)
        else:
            if has_unread:
                border = f"{FG_YELLOW}▐{RESET} "
            else:
                border = " "
            line = f"{tool_prefix}{border}{color}{icon}{RESET} {weight}{color}{name_padded}{RESET}{weight}{FG_GRAY}{age_padded}{desc_sep}{display_text}{timeout_marker}{RESET}"
            line = truncate_ansi(line, width)

        return line

    def _render_recently_stopped_row(
        self, recently_stopped: list[str], display_idx: int, width: int
    ) -> str:
        """Render the recently stopped summary row"""
        from ..core.db import RECENTLY_STOPPED_MINUTES

        # Build names list (truncate if too many)
        names = ", ".join(recently_stopped[:5])
        if len(recently_stopped) > 5:
            names += f" +{len(recently_stopped) - 5}"

        # Arrow indicates actionable (navigates to events)
        arrow = "[→]"

        # Format: "  ◌ Recently stopped (10m): luna, nova, kira  [→]"
        content = (
            f"  ◌ Recently stopped ({RECENTLY_STOPPED_MINUTES}m): {names}  {arrow}"
        )

        # Apply styling based on cursor position
        if display_idx == self.state.cursor:
            line = f"{BG_CHARCOAL}{FG_GRAY}{content}{RESET}"
            line = truncate_ansi(line, width)
            line = bg_ljust(line, width, BG_CHARCOAL)
        else:
            line = f"{DIM}{FG_GRAY}{content}{RESET}"
            line = truncate_ansi(line, width)

        return line

    def build(self, height: int, width: int) -> List[str]:
        """Build manage screen layout"""
        # Use minimum height for layout calculation to maintain structure
        layout_height = max(10, height)

        lines = []

        # Calculate layout using shared function
        instance_rows, message_rows, input_rows = self.calculate_layout(
            layout_height, width
        )

        from ..core.instances import is_remote_instance
        from ..core.db import get_recently_stopped

        # Sort instances by creation time (newest first) - stable, no jumping
        all_instances = sorted(
            self.state.instances.items(),
            key=lambda x: -x[1]["data"].get("created_at", 0.0),
        )

        # Separate local vs remote (row exists = participating, no stopped section)
        local_instances = [
            (n, i)
            for n, i in all_instances
            if not is_remote_instance(i.get("data", {}))
        ]
        remote_instances = [
            (n, i) for n, i in all_instances if is_remote_instance(i.get("data", {}))
        ]
        # Sort remote by created_at (all are participating)
        remote_instances.sort(key=lambda x: -x[1]["data"].get("created_at", 0.0))
        remote_count = len(remote_instances)

        # Get recently stopped instances (from events, last 10 min)
        active_names = set(self.state.instances.keys())
        recently_stopped = get_recently_stopped(exclude_active=active_names)

        # Auto-expand remote section if user hasn't explicitly toggled
        # Expand if count <= 3 OR any device synced < 5min ago
        if not self.state.show_remote_user_set and remote_count > 0:
            recent_sync = any(
                (time.time() - sync_time) < 300  # 5 minutes
                for sync_time in self.state.device_sync_times.values()
                if sync_time
            )
            self.state.show_remote = (remote_count <= 3) or recent_sync

        # Restore cursor position by instance name (stable across sorts)
        if self.state.cursor_instance_name:
            found = False
            target_name = self.state.cursor_instance_name
            last_cursor = self.state.cursor

            # Check local instances
            for i, (name, _) in enumerate(local_instances):
                if name == target_name:
                    self.state.cursor = i
                    found = True
                    break

            # Check remote instances (if not found and expanded)
            if not found and self.state.show_remote:
                for i, (name, _) in enumerate(remote_instances):
                    if name == target_name:
                        # Position = local_count + 1 (remote separator) + index
                        self.state.cursor = len(local_instances) + 1 + i
                        found = True
                        break

            if not found:
                # Instance disappeared (likely removed), move cursor to next logical position
                # Calculate total display count first
                temp_display_count = len(local_instances)
                if remote_count > 0:
                    temp_display_count += 1  # remote separator
                    if self.state.show_remote:
                        temp_display_count += remote_count
                if recently_stopped:
                    temp_display_count += 1  # recently stopped row

                # Keep cursor at same position or move up if we were at the end
                if temp_display_count > 0:
                    self.state.cursor = min(last_cursor, temp_display_count - 1)
                else:
                    self.state.cursor = 0

                # Update cursor_instance_name to the instance now at cursor position
                # (Will be set below in the "Update tracked instance name" section)
                self.state.cursor_instance_name = None
                self.sync_scroll_to_cursor()

        # Calculate total display items for cursor bounds (local + remote + recently_stopped)
        display_count = len(local_instances)
        if remote_count > 0:
            display_count += 1  # remote separator row
            if self.state.show_remote:
                display_count += remote_count
        if recently_stopped:
            display_count += 1  # recently stopped summary row

        # Calculate separator position for cursor tracking
        remote_sep = len(local_instances) if remote_count > 0 else -1

        # Ensure cursor is valid
        if display_count > 0:
            self.state.cursor = max(0, min(self.state.cursor, display_count - 1))
            # Update tracked instance name (None if on separator)
            cursor = self.state.cursor
            if cursor < len(local_instances):
                self.state.cursor_instance_name = local_instances[cursor][0]
            elif remote_sep >= 0 and cursor == remote_sep:
                self.state.cursor_instance_name = None  # Remote separator
            elif self.state.show_remote and remote_count > 0:
                remote_idx = cursor - remote_sep - 1
                if remote_idx < remote_count:
                    self.state.cursor_instance_name = remote_instances[remote_idx][0]
                else:
                    self.state.cursor_instance_name = None
            else:
                self.state.cursor_instance_name = None
        else:
            self.state.cursor = 0
            self.state.cursor_instance_name = None

        # Empty state - no instances (neither local nor remote)
        if len(local_instances) == 0 and remote_count == 0:
            lines.append("")
            lines.append(f"{FG_ORANGE}  ╦ ╦╔═╗╔═╗╔╦╗{RESET}")
            lines.append(f"{FG_ORANGE}  ╠═╣║  ║ ║║║║{RESET}")
            lines.append(f"{FG_ORANGE}  ╩ ╩╚═╝╚═╝╩ ╩{RESET}")
            lines.append("")
            lines.append(f"{FG_GRAY}  Realtime messaging for AI coding agents{RESET}")
            lines.append("")
            lines.append(
                f"{FG_WHITE}  Tab → LAUNCH{RESET}          {FG_GRAY}Start agents here{RESET}"
            )
            lines.append(
                f"{FG_WHITE}  hcom 3 claude{RESET}         {FG_GRAY}Quick launch 3 Claudes{RESET}"
            )
            lines.append(
                f"{FG_WHITE}  hcom start{RESET}            {FG_GRAY}Connect hcom from inside any session{RESET}"
            )
            lines.append("")
            lines.append(f"{FG_GRAY}  For all commands: hcom --help{RESET}")
            lines.append(f"{FG_GRAY}  For help: hcom claude 'help me! hcom!'{RESET}")

            lines.append("")
            # Pad to instance_rows
            while len(lines) < instance_rows:
                lines.append("")
        else:
            # Calculate total display items: local + remote section + recently stopped
            display_count = len(local_instances)
            if remote_count > 0:
                display_count += 1  # remote separator row
                if self.state.show_remote:
                    display_count += remote_count
            if recently_stopped:
                display_count += 1  # recently stopped summary row

            # Track recently stopped row position for cursor
            recently_stopped_pos = display_count - 1 if recently_stopped else -1
            self._recently_stopped_row_pos = recently_stopped_pos

            # Calculate visible window
            max_scroll = max(0, display_count - instance_rows)
            self.state.instance_scroll_pos = max(
                0, min(self.state.instance_scroll_pos, max_scroll)
            )

            # Calculate dynamic name column width based on actual names
            all_for_width = list(local_instances)
            if self.state.show_remote:
                all_for_width += remote_instances
            max_instance_name_len = max(
                (len(name) for name, _ in all_for_width), default=0
            )
            # Check if any instance has badges
            has_background = any(
                info.get("data", {}).get("background", False)
                for _, info in all_for_width
            )

            # Check if multiple tool types exist (show tool prefix if so)
            all_instances_for_tool = local_instances + remote_instances
            tool_types = set(
                info.get("data", {}).get("tool", "claude")
                for _, info in all_instances_for_tool
            )
            show_tool = len(tool_types) > 1

            # Check if multiple directories exist (show project tag if so)
            from ..shared import get_project_tag

            directories = set(
                info.get("data", {}).get("directory", "")
                for _, info in all_instances_for_tool
                if info.get("data", {}).get("directory")
            )
            show_project = len(directories) > 1

            # Calculate max badge length for column width
            badge_len = 0
            if has_background:
                badge_len += 11  # " [headless]"
            if show_tool:
                badge_len += 8  # " CLAUDE " prefix
            if show_project:
                # " · " + max project tag length
                max_tag_len = max(
                    (
                        len(get_project_tag(info.get("data", {}).get("directory", "")))
                        for _, info in all_for_width
                        if info.get("data", {}).get("directory")
                    ),
                    default=0,
                )
                badge_len += 3 + max_tag_len  # " · " + tag
            # Unread count now shown as superscript on icon (○³), doesn't affect name column
            # Add 2 for cursor icon/spacing
            name_col_width = max_instance_name_len + badge_len + 2
            # Set bounds: min 20, max based on terminal width
            # Reserve: 2 (icon) + 10 (age) + 2 (sep) + 15 (desc min) = 29
            # Prioritize showing full instance names over description space
            max_name_width = max(20, width - 29)
            name_col_width = max(20, min(name_col_width, max_name_width))

            # Build display rows
            visible_start = self.state.instance_scroll_pos
            visible_end = min(visible_start + instance_rows, display_count)

            # If only 1 item would be hidden, show it instead of scroll indicator
            if visible_start == 1:
                visible_start = 0
            if display_count - visible_end == 1:
                visible_end = display_count

            for display_idx in range(visible_start, visible_end):
                # Determine what this display row represents
                if display_idx < len(local_instances):
                    # Local instance
                    name, info = local_instances[display_idx]
                    project_tag = (
                        get_project_tag(info.get("data", {}).get("directory", ""))
                        if show_project
                        else ""
                    )
                    line = self._render_instance_row(
                        name,
                        info,
                        display_idx,
                        name_col_width,
                        width,
                        show_tool=show_tool,
                        project_tag=project_tag,
                    )
                    lines.append(line)
                elif remote_sep >= 0 and display_idx == remote_sep:
                    # Relay separator row (no dot here - dot is in top bar)
                    is_cursor = display_idx == self.state.cursor
                    arrow = "▼" if self.state.show_remote else "▶"

                    # Build sync status when expanded: relay (BOXE:1m, CATA:2s) ▼
                    if self.state.show_remote and self.state.device_sync_times:
                        # Build device_id -> suffix mapping from remote instances
                        device_suffixes = {}
                        for name, info in remote_instances:
                            origin_device = info.get("data", {}).get(
                                "origin_device_id", ""
                            )
                            if origin_device and ":" in name:
                                suffix = name.rsplit(":", 1)[1]
                                device_suffixes[origin_device] = suffix

                        sync_parts = []
                        for device, sync_time in sorted(
                            self.state.device_sync_times.items()
                        ):
                            if sync_time:
                                sync_age = time.time() - sync_time
                                suffix = device_suffixes.get(device, device[:4].upper())
                                color = get_device_sync_color(sync_age)
                                sync_parts.append(
                                    f"{color}{suffix}:{format_age(sync_age)}{FG_GRAY}"
                                )

                        if sync_parts:
                            sep_text = f" relay ({', '.join(sync_parts)}) {arrow} "
                        else:
                            sep_text = f" relay ({remote_count}) {arrow} "
                    else:
                        sep_text = f" relay ({remote_count}) {arrow} "

                    pad_len = max(0, (width - ansi_len(sep_text) - 2) // 2)
                    sep_line = f"{'─' * pad_len}{sep_text}{'─' * pad_len}"
                    if is_cursor:
                        line = f"{BG_CHARCOAL}{FG_GRAY}{sep_line}{RESET}"
                        line = bg_ljust(line, width, BG_CHARCOAL)
                    else:
                        line = f"{FG_GRAY}{sep_line}{RESET}"
                    lines.append(truncate_ansi(line, width))
                elif self.state.show_remote and remote_count > 0:
                    # Remote instance (only when expanded)
                    remote_idx = display_idx - remote_sep - 1
                    if 0 <= remote_idx < remote_count:
                        name, info = remote_instances[remote_idx]
                        project_tag = (
                            get_project_tag(info.get("data", {}).get("directory", ""))
                            if show_project
                            else ""
                        )
                        line = self._render_instance_row(
                            name,
                            info,
                            display_idx,
                            name_col_width,
                            width,
                            is_remote=True,
                            show_tool=show_tool,
                            project_tag=project_tag,
                        )
                        lines.append(line)
                    elif (
                        recently_stopped_pos >= 0
                        and display_idx == recently_stopped_pos
                    ):
                        # Recently stopped summary row
                        line = self._render_recently_stopped_row(
                            recently_stopped, display_idx, width
                        )
                        lines.append(line)
                elif recently_stopped_pos >= 0 and display_idx == recently_stopped_pos:
                    # Recently stopped summary row (when remote not expanded or no remote)
                    line = self._render_recently_stopped_row(
                        recently_stopped, display_idx, width
                    )
                    lines.append(line)

            # Add scroll indicators if needed
            if display_count > instance_rows:
                # If cursor will conflict with indicator, move cursor line first
                if visible_start > 0 and self.state.cursor == visible_start:
                    # Save cursor line (at position 0), move to position 1
                    cursor_line = lines[0] if lines else ""
                    lines[0] = lines[1] if len(lines) > 1 else ""
                    if len(lines) > 1:
                        lines[1] = cursor_line

                if visible_end < display_count and self.state.cursor == visible_end - 1:
                    # Save cursor line (at position -1), move to position -2
                    cursor_line = lines[-1] if lines else ""
                    lines[-1] = lines[-2] if len(lines) > 1 else ""
                    if len(lines) > 1:
                        lines[-2] = cursor_line

                # Now add indicators at edges (may overwrite moved content, that's fine)
                if visible_start > 0:
                    count_above = visible_start
                    indicator = f"{FG_GRAY}↑ {count_above} more{RESET}"
                    if lines:
                        lines[0] = ansi_ljust(indicator, width)

                if visible_end < display_count:
                    count_below = display_count - visible_end
                    indicator = f"{FG_GRAY}↓ {count_below} more{RESET}"
                    if lines:
                        lines[-1] = ansi_ljust(indicator, width)

            # Pad instances
            while len(lines) < instance_rows:
                lines.append("")

        # Separator
        lines.append(separator_line(width))

        # Instance detail section (if active) - render ABOVE messages
        detail_rows = 0
        if self.state.show_instance_detail:
            detail_lines = self.build_instance_detail(
                self.state.show_instance_detail, width
            )
            lines.extend(detail_lines)
            detail_rows = len(detail_lines)
            # Separator after detail
            lines.append(separator_line(width))
            detail_rows += 1  # Include separator in count

        # Calculate remaining message rows (subtract detail from message budget)
        actual_message_rows = message_rows - detail_rows

        # Messages - Slack-style format with sender on separate line
        if self.state.messages and actual_message_rows > 0:
            all_wrapped_lines = []

            # Get instance read positions for read receipt calculation
            # Keys are full display names to match delivered_to list
            instance_reads = {}
            remote_instance_set = set()
            remote_msg_ts = {}
            try:
                from ..core.db import get_db
                from ..core.instances import get_full_name

                conn = get_db()
                rows = conn.execute(
                    "SELECT name, last_event_id, origin_device_id, tag FROM instances"
                ).fetchall()
                # Track full_name -> base_name mapping for DB queries
                full_to_base = {}
                for row in rows:
                    full_name = (
                        get_full_name({"name": row["name"], "tag": row["tag"]})
                        or row["name"]
                    )
                    full_to_base[full_name] = row["name"]
                    instance_reads[full_name] = row["last_event_id"]
                    if row["origin_device_id"]:
                        remote_instance_set.add(full_name)
                # Get max msg_ts for remote instances from their status events
                for full_name in remote_instance_set:
                    base_name = full_to_base.get(full_name, full_name)
                    row = conn.execute(
                        """
                        SELECT json_extract(data, '$.msg_ts') as msg_ts
                        FROM events WHERE type = 'status' AND instance = ?
                          AND json_extract(data, '$.msg_ts') IS NOT NULL
                        ORDER BY id DESC LIMIT 1
                    """,
                        (base_name,),
                    ).fetchone()
                    if row and row["msg_ts"]:
                        remote_msg_ts[full_name] = row["msg_ts"]
            except Exception:
                pass  # No read receipts if DB query fails

            for (
                time_str,
                sender,
                message,
                delivered_to,
                event_id,
            ) in self.state.messages:
                # Format timestamp (convert UTC to local time)
                dt = parse_iso_timestamp(time_str) if "T" in time_str else None
                display_time = (
                    dt.astimezone().strftime("%H:%M")
                    if dt
                    else (time_str[:5] if len(time_str) >= 5 else time_str)
                )

                # Build recipient list with read receipts (width-aware truncation)
                recipient_str = ""
                if delivered_to:
                    # Calculate available width for recipients
                    # Format: "HH:MM sender → recipients"
                    base_len = (
                        len(display_time) + 1 + len(sender) + 3
                    )  # +1 space, +3 for " → "
                    available = width - base_len - 5  # Reserve for "+N more"

                    recipient_parts = []
                    current_len = 0
                    shown = 0

                    for recipient in delivered_to:
                        # Check if recipient has read this message
                        if recipient in remote_instance_set:
                            has_read = remote_msg_ts.get(recipient, "") >= time_str
                        else:
                            has_read = instance_reads.get(recipient, 0) >= event_id
                        tick = " ✓" if has_read else ""
                        part = f"{recipient}{tick}"

                        # Calculate length with separator
                        part_len = ansi_len(part) + (
                            2 if shown > 0 else 0
                        )  # +2 for ", "

                        if current_len + part_len <= available:
                            recipient_parts.append(part)
                            current_len += part_len
                            shown += 1
                        else:
                            break

                    if recipient_parts:
                        recipient_str = ", ".join(recipient_parts)
                        remaining = len(delivered_to) - shown
                        if remaining > 0:
                            recipient_str += f" {FG_GRAY}+{remaining} more{RESET}"

                    if recipient_str:
                        recipient_str = f" {FG_GRAY}→{RESET} {recipient_str}"

                # Header line: timestamp + sender + recipients (truncated to width)
                header = f"{FG_GRAY}{display_time}{RESET} {BOLD}{sender}{RESET}{recipient_str}"
                header = truncate_ansi(header, width)
                all_wrapped_lines.append(header)

                # Replace literal newlines with space for preview
                display_message = message.replace("\n", " ")

                # Bold @mentions in message (e.g., @name or @name:DEVICE)
                if "@" in display_message:
                    display_message = re.sub(
                        r"(@[\w\-_:]+)",
                        f"{BOLD}\\1{RESET}{FG_LIGHTGRAY}",
                        display_message,
                    )

                # Message lines with indent (6 spaces for visual balance)
                indent = "      "
                max_msg_len = width - len(indent)

                # Wrap message text
                if max_msg_len > 0:
                    wrapper = AnsiTextWrapper(width=max_msg_len)
                    wrapped = wrapper.wrap(display_message)

                    # All message lines indented uniformly
                    # Truncate to max_msg_len to prevent terminal wrapping on long unbreakable sequences
                    for wrapped_line in wrapped:
                        truncated = truncate_ansi(wrapped_line, max_msg_len)
                        line = f"{indent}{FG_LIGHTGRAY}{truncated}{RESET}"
                        all_wrapped_lines.append(line)
                else:
                    # Fallback if width too small
                    all_wrapped_lines.append(
                        f"{indent}{FG_LIGHTGRAY}{display_message[: width - len(indent)]}{RESET}"
                    )

                # Blank line after each message (for separation)
                all_wrapped_lines.append("")

            # Take last N lines to fit available space (mid-message truncation)
            visible_lines = (
                all_wrapped_lines[-actual_message_rows:]
                if len(all_wrapped_lines) > actual_message_rows
                else all_wrapped_lines
            )
            lines.extend(visible_lines)
        else:
            # No messages - show hint only if instances exist (empty state shows logo instead)
            if self.state.instances:
                lines.append(
                    f"{FG_GRAY}No messages yet - type to compose | @ to mention{RESET}"
                )

        # Calculate how many lines are used before input (instances + detail + messages + separators)
        lines_before_input = len(lines)

        # Reserve space for input at bottom: input_rows + 2 separators (before + after)
        input_section_height = input_rows + 2
        max_lines_before_input = height - input_section_height

        # Truncate message/detail area if it overflows, keeping input visible
        if lines_before_input > max_lines_before_input:
            lines = lines[:max_lines_before_input]

        # Pad to fill space before input
        while len(lines) < max_lines_before_input:
            lines.append("")

        # Separator before input
        lines.append(separator_line(width))

        # Input area (auto-wrapped) - at bottom, always visible
        input_lines = self.render_wrapped_input(width, input_rows)
        lines.extend(input_lines)

        # Separator after input
        lines.append(separator_line(width))

        return lines

    def _get_display_lists(self):
        """Build local/remote instance lists for cursor navigation"""
        from ..core.instances import is_remote_instance
        from ..core.db import get_recently_stopped

        all_instances = sorted(
            self.state.instances.items(),
            key=lambda x: -x[1]["data"].get("created_at", 0.0),
        )

        # Separate local vs remote (row exists = participating)
        local_instances = [
            (n, i)
            for n, i in all_instances
            if not is_remote_instance(i.get("data", {}))
        ]
        remote_instances = [
            (n, i) for n, i in all_instances if is_remote_instance(i.get("data", {}))
        ]
        # Sort remote by created_at (must match build())
        remote_instances.sort(key=lambda x: -x[1]["data"].get("created_at", 0.0))

        remote_count = len(remote_instances)

        # Get recently stopped names (excluding currently active)
        active_names = set(self.state.instances.keys())
        recently_stopped = get_recently_stopped(exclude_active=active_names)

        # Calculate display count: local + remote section + recently stopped
        display_count = len(local_instances)
        if remote_count > 0:
            display_count += 1  # remote separator
            if self.state.show_remote:
                display_count += remote_count
        if recently_stopped:
            display_count += 1  # recently stopped row

        return local_instances, remote_instances, display_count, recently_stopped

    def _get_instance_at_cursor(self, local, remote, recently_stopped=None):
        """Get (instance, is_remote, row_type) at cursor.

        Returns:
            (instance_tuple, is_remote, row_type) where row_type is:
            - 'instance': normal instance row
            - 'remote_sep': remote separator row
            - 'recently_stopped': recently stopped summary row
            - None: unknown/empty
        """
        remote_count = len(remote)

        # Calculate section boundaries
        local_end = len(local)
        remote_sep_pos = local_end if remote_count > 0 else -1

        # Calculate recently stopped row position (after all instances)
        recently_stopped_pos = -1
        if recently_stopped:
            recently_stopped_pos = len(local)
            if remote_count > 0:
                recently_stopped_pos += 1  # remote separator
                if self.state.show_remote:
                    recently_stopped_pos += remote_count

        cursor = self.state.cursor

        # Local section
        if cursor < local_end:
            return local[cursor], False, "instance"

        # Remote separator
        if remote_count > 0 and cursor == remote_sep_pos:
            return None, False, "remote_sep"

        # Remote instances (if expanded)
        if remote_count > 0 and self.state.show_remote:
            remote_start = remote_sep_pos + 1
            if remote_start <= cursor < remote_start + remote_count:
                return remote[cursor - remote_start], True, "instance"

        # Recently stopped row
        if recently_stopped_pos >= 0 and cursor == recently_stopped_pos:
            return None, False, "recently_stopped"

        return None, False, None

    def _get_separator_positions(self, local, remote):
        """Calculate separator position for remote section"""
        remote_count = len(remote)

        # Remote separator is right after local instances
        remote_sep = len(local) if remote_count > 0 else -1

        return remote_sep

    def handle_key(self, key: str):
        """Handle keys in Manage mode"""
        # Build display lists
        local, remote, display_count, recently_stopped = self._get_display_lists()
        self._get_separator_positions(local, remote)

        if key == "UP":
            if display_count > 0 and self.state.cursor > 0:
                self.state.cursor -= 1
                inst, is_remote, row_type = self._get_instance_at_cursor(
                    local, remote, recently_stopped
                )
                self.state.cursor_instance_name = inst[0] if inst else None
                self.tui.clear_all_pending_confirmations()
                self.state.show_instance_detail = None
                self.sync_scroll_to_cursor()
        elif key == "DOWN":
            if display_count > 0 and self.state.cursor < display_count - 1:
                self.state.cursor += 1
                inst, is_remote, row_type = self._get_instance_at_cursor(
                    local, remote, recently_stopped
                )
                self.state.cursor_instance_name = inst[0] if inst else None
                self.tui.clear_all_pending_confirmations()
                self.state.show_instance_detail = None
                self.sync_scroll_to_cursor()
        elif key == "@":
            self.tui.clear_all_pending_confirmations()
            inst, is_remote, row_type = self._get_instance_at_cursor(
                local, remote, recently_stopped
            )
            if inst:
                name, _ = inst
                mention = f"@{name} "  # Name already includes :DEVICE suffix for remote
                if mention not in self.state.message_buffer:
                    self.state.message_buffer, self.state.message_cursor_pos = (
                        text_input_insert(
                            self.state.message_buffer,
                            self.state.message_cursor_pos,
                            mention,
                        )
                    )
        elif key == "SPACE":
            self.tui.clear_all_pending_confirmations()
            # Add space to message buffer at cursor position
            self.state.message_buffer, self.state.message_cursor_pos = (
                text_input_insert(
                    self.state.message_buffer, self.state.message_cursor_pos, " "
                )
            )
        elif key == "LEFT":
            self.tui.clear_all_pending_confirmations()
            # Move cursor left in message buffer
            self.state.message_cursor_pos = text_input_move_left(
                self.state.message_cursor_pos
            )
        elif key == "RIGHT":
            self.tui.clear_all_pending_confirmations()
            # Move cursor right in message buffer
            self.state.message_cursor_pos = text_input_move_right(
                self.state.message_buffer, self.state.message_cursor_pos
            )
        elif key == "ESC":
            # Clear everything: message buffer, detail view, and confirmations
            self.state.message_buffer = ""
            self.state.message_cursor_pos = 0
            self.state.show_instance_detail = None
            self.tui.clear_all_pending_confirmations()
        elif key == "BACKSPACE":
            self.tui.clear_all_pending_confirmations()
            # Delete character before cursor in message buffer
            self.state.message_buffer, self.state.message_cursor_pos = (
                text_input_backspace(
                    self.state.message_buffer, self.state.message_cursor_pos
                )
            )
        elif key == "ENTER":
            # Clear stop all and reset confirmations (stop handled separately below)
            self.tui.clear_pending_confirmations_except("stop")

            # Smart Enter: send message if text exists, otherwise toggle instances
            if self.state.message_buffer.strip():
                # Send message using cmd_send for consistent validation and error handling
                # Dim input during send (visual feedback without flash)
                self.state.send_state = "sending"
                self.state.frame_dirty = True
                self.tui.render()  # Force immediate display
                try:
                    message = self.state.message_buffer.strip()
                    # Pass --from bigboss for TUI (external human sender)
                    result = cmd_send(["--from", "bigboss", message])
                    if result == 0:
                        # Brief bright prefix, then clear
                        self.state.send_state = "sent"
                        self.state.send_state_until = time.time() + 0.1
                        # Clear message buffer and cursor
                        self.state.message_buffer = ""
                        self.state.message_cursor_pos = 0
                    else:
                        self.state.send_state = None
                        self.tui.flash_error("Send failed")
                except Exception as e:
                    self.state.send_state = None
                    self.tui.flash_error(f"Error: {str(e)}")
            else:
                # Get what's at cursor
                inst, is_remote, row_type = self._get_instance_at_cursor(
                    local, remote, recently_stopped
                )

                # Handle special rows
                if row_type == "remote_sep":
                    self.state.show_remote = not self.state.show_remote
                    self.state.show_remote_user_set = True
                    return

                if row_type == "recently_stopped":
                    # Switch to EVENTS mode with instances view
                    return ("switch_events", {"view": "instances"})

                if not inst:
                    return

                name, info = inst

                # Remote instances: stop via control event (can only stop, not start)
                if is_remote:
                    from ..relay import send_control

                    # Extract device short ID from name (format: name:DEVICE)
                    if ":" in name:
                        base_name, device_short = name.rsplit(":", 1)

                        # Get status color for flash message
                        status = info.get("status", "unknown")
                        color = STATUS_FG.get(status, FG_WHITE)

                        # Two-step confirmation for remote stop
                        if (
                            self.state.pending_stop == name
                            and (time.time() - self.state.pending_stop_time)
                            <= self.tui.CONFIRMATION_TIMEOUT
                        ):
                            # Execute remote stop (row exists = active, can only stop)
                            if send_control("stop", base_name, device_short):
                                self.tui.flash(f"Stopped hcom for {color}{name}{RESET}")
                                self.tui.load_status()
                            else:
                                self.tui.flash_error("Failed to stop remote instance")
                            self.state.pending_stop = None
                            self.state.show_instance_detail = None
                        else:
                            # First press - request confirmation + show detail
                            self.state.pending_stop = name
                            self.state.pending_stop_time = time.time()
                            self.state.show_instance_detail = name
                    else:
                        # Can't parse device ID, just show detail
                        self.state.show_instance_detail = name
                    return

                # Local instance - stop with two-step confirmation (row exists = active, can only stop)
                # Get status color for name
                status = info.get("status", "unknown")
                color = STATUS_FG.get(status, FG_WHITE)

                # Light green coloring for message delivery (active with deliver token)
                status_context = info.get("data", {}).get("status_context", "")
                if status == "active" and status_context.startswith("deliver:"):
                    color = FG_DELIVER

                # Check if confirming previous stop
                if (
                    self.state.pending_stop == name
                    and (time.time() - self.state.pending_stop_time)
                    <= self.tui.CONFIRMATION_TIMEOUT
                ):
                    # Execute stop (confirmation received) - row exists = participating, stop deletes
                    # Use base_name for DB operations, display name for UI
                    base_name = info.get("base_name", name)
                    try:
                        # Suppress CLI output to prevent TUI corruption
                        with _suppress_output():
                            cmd_stop([base_name])
                        self.tui.flash(f"Stopped hcom for {color}{name}{RESET}")
                        self.tui.load_status()
                    except Exception as e:
                        self.tui.flash_error(f"Error: {str(e)}")
                    finally:
                        self.state.pending_stop = None
                        self.state.show_instance_detail = None
                else:
                    # Show confirmation (first press) + show detail view
                    self.state.pending_stop = name
                    self.state.pending_stop_time = time.time()
                    self.state.show_instance_detail = name  # Also show detail

        elif key == "CTRL_K":
            # Check state before clearing
            is_confirming = (
                self.state.pending_stop_all
                and (time.time() - self.state.pending_stop_all_time)
                <= self.tui.CONFIRMATION_TIMEOUT
            )
            self.tui.clear_pending_confirmations_except("stop_all")

            # Two-step confirmation for stop all
            if is_confirming:
                # Execute stop all (confirmation received)
                self.tui.stop_all_instances()
                self.state.pending_stop_all = False
            else:
                # Show confirmation (first press) - 10s duration
                self.state.pending_stop_all = True
                self.state.pending_stop_all_time = time.time()
                self.tui.flash(
                    f"{FG_WHITE}Confirm stop all instances? (press Ctrl+K again){RESET}",
                    duration=self.tui.CONFIRMATION_FLASH_DURATION,
                    color="white",
                )

        elif key == "CTRL_R":
            # Check state before clearing
            is_confirming = (
                self.state.pending_reset
                and (time.time() - self.state.pending_reset_time)
                <= self.tui.CONFIRMATION_TIMEOUT
            )
            self.tui.clear_pending_confirmations_except("reset")

            # Two-step confirmation for reset
            if is_confirming:
                # Execute reset (confirmation received)
                self.tui.reset_events()
                self.state.pending_reset = False
            else:
                # Show confirmation (first press)
                self.state.pending_reset = True
                self.state.pending_reset_time = time.time()
                self.tui.flash(
                    f"{FG_WHITE}Confirm clear & archive (conversation + instance list)? (press Ctrl+R again){RESET}",
                    duration=self.tui.CONFIRMATION_FLASH_DURATION,
                    color="white",
                )

        elif key == "\n":
            # Handle pasted newlines - insert literally
            self.tui.clear_all_pending_confirmations()
            self.state.message_buffer, self.state.message_cursor_pos = (
                text_input_insert(
                    self.state.message_buffer, self.state.message_cursor_pos, "\n"
                )
            )

        elif key and len(key) == 1 and key.isprintable():
            self.tui.clear_all_pending_confirmations()
            # Insert printable characters at cursor position
            self.state.message_buffer, self.state.message_cursor_pos = (
                text_input_insert(
                    self.state.message_buffer, self.state.message_cursor_pos, key
                )
            )

    def calculate_layout(self, height: int, width: int) -> tuple[int, int, int]:
        """Calculate instance/message/input row allocation"""
        from ..core.instances import is_remote_instance
        from ..core.db import get_recently_stopped

        # Dynamic input area based on buffer size
        input_rows = calculate_text_input_rows(self.state.message_buffer, width)
        # Space budget
        separator_rows = 3  # One separator between instances and messages, one before input, one after input
        min_instance_rows = 3

        available = height - input_rows - separator_rows

        # Calculate display count based on current collapse state
        all_instances = list(self.state.instances.values())
        local_instances = [
            i for i in all_instances if not is_remote_instance(i.get("data", {}))
        ]
        remote_instances = [
            i for i in all_instances if is_remote_instance(i.get("data", {}))
        ]

        local_count = len(local_instances)
        remote_count = len(remote_instances)

        # Get recently stopped for display count
        active_names = set(self.state.instances.keys())
        recently_stopped = get_recently_stopped(exclude_active=active_names)

        # Build display count: local + remote section + recently_stopped
        display_count = local_count
        if remote_count > 0:
            display_count += 1  # remote separator
            if self.state.show_remote:
                display_count += remote_count
        if recently_stopped:
            display_count += 1  # recently stopped row

        max_instance_rows = int(available * 0.6)
        instance_rows = max(min_instance_rows, min(display_count, max_instance_rows))
        message_rows = available - instance_rows

        return instance_rows, message_rows, input_rows

    def sync_scroll_to_cursor(self):
        """Sync scroll position to cursor"""
        # Calculate visible rows using shared layout function
        width, rows = get_terminal_size()
        body_height = max(10, rows - 3)  # Header, flash, footer
        instance_rows, _, _ = self.calculate_layout(body_height, width)
        visible_instance_rows = instance_rows  # Full instance section is visible

        # Scroll up if cursor moved above visible window
        if self.state.cursor < self.state.instance_scroll_pos:
            self.state.instance_scroll_pos = self.state.cursor
        # Scroll down if cursor moved below visible window
        elif (
            self.state.cursor >= self.state.instance_scroll_pos + visible_instance_rows
        ):
            self.state.instance_scroll_pos = (
                self.state.cursor - visible_instance_rows + 1
            )

    def render_wrapped_input(self, width: int, input_rows: int) -> List[str]:
        """Render message input (delegates to shared helper)"""
        return render_text_input(
            self.state.message_buffer,
            self.state.message_cursor_pos,
            width,
            input_rows,
            prefix="> ",
            send_state=self.state.send_state,
        )

    def build_instance_detail(self, name: str, width: int) -> List[str]:
        """Build instance metadata display (similar to hcom list --verbose)"""
        import time
        from ..core.instances import is_remote_instance

        lines = []

        # Get instance data
        if name not in self.state.instances:
            return [f"{FG_GRAY}Instance not found{RESET}"]

        info = self.state.instances[name]
        data = info["data"]

        # Get status color for name (same as flash message)
        status = info.get("status", "unknown")
        color = STATUS_FG.get(status, FG_WHITE)

        # Light green coloring for message delivery (active with deliver token)
        status_context = data.get("status_context", "")
        if status == "active" and status_context.startswith("deliver:"):
            color = FG_DELIVER

        # Header: bold colored name (badges already shown in instance list)
        header = f"{BOLD}{color}{name}{RESET}"
        lines.append(truncate_ansi(header, width))

        # Unread message count (aligned with other fields at column 16)
        unread_count = self.state.unread_counts.get(name, 0)
        if unread_count > 0:
            lines.append(
                truncate_ansi(
                    f"  {FG_YELLOW}unread:       {unread_count} message{'s' if unread_count != 1 else ''}{RESET}",
                    width,
                )
            )

        if is_remote_instance(data):
            # Remote instance: show device/sync info plus available details
            origin_device = data.get("origin_device_id", "")
            device_short = origin_device[:8] if origin_device else "(unknown)"

            # Get device sync time
            sync_time = self.state.device_sync_times.get(origin_device, 0)
            sync_str = (
                f"{format_age(time.time() - sync_time)} ago" if sync_time else "never"
            )

            lines.append(truncate_ansi(f"  device:     {device_short}", width))
            lines.append(truncate_ansi(f"  last_sync:  {sync_str}", width))

            # Show available remote instance details
            session_id = data.get("session_id") or "(none)"
            tool = data.get("tool", "claude")
            lines.append(truncate_ansi(f"  session_id: {session_id}", width))
            lines.append(truncate_ansi(f"  tool:       {tool}", width))

            parent = data.get("parent_name")
            if parent:
                lines.append(truncate_ansi(f"  parent:     {parent}", width))

            directory = data.get("directory")
            if directory:
                lines.append(
                    truncate_ansi(f"  directory:  {shorten_path(directory)}", width)
                )

            # Format status_time
            status_time = data.get("status_time", 0)
            if status_time:
                lines.append(
                    truncate_ansi(
                        f"  status_at:  {format_age(time.time() - status_time)} ago",
                        width,
                    )
                )

            last_stop = data.get("last_stop", 0)
            if last_stop:
                lines.append(
                    truncate_ansi(
                        f"  last_stop:  {format_age(time.time() - last_stop)} ago",
                        width,
                    )
                )
        else:
            # Local instance: show full details
            session_id = data.get("session_id") or "None"
            directory = data.get("directory") or "(none)"
            parent = data.get("parent_name") or None

            # Format paths (shorten with ~)
            directory = shorten_path(directory) if directory != "(none)" else directory
            log_file = shorten_path(data.get("background_log_file"))
            transcript = shorten_path(data.get("transcript_path")) or "(none)"

            # Format created_at timestamp
            created_ts = data.get("created_at")
            created = (
                f"{format_age(time.time() - created_ts)} ago"
                if created_ts
                else "(unknown)"
            )

            # Tool type
            tool = data.get("tool", "claude")

            # Build detail lines (truncated to terminal width)
            lines.append(truncate_ansi(f"  session_id:   {session_id}", width))
            lines.append(truncate_ansi(f"  tool:         {tool}", width))

            # Show status_detail if present
            status_detail = data.get("status_detail", "")
            if status_detail:
                lines.append(truncate_ansi(f"  detail:       {status_detail}", width))

            lines.append(truncate_ansi(f"  created:      {created}", width))
            lines.append(truncate_ansi(f"  directory:    {directory}", width))

            if parent:
                agent_id = data.get("agent_id") or "(none)"
                lines.append(truncate_ansi(f"  parent:       {parent}", width))
                lines.append(truncate_ansi(f"  agent_id:     {agent_id}", width))

            # Show binding status (integration tier): pty/hooks/none
            from ..core.db import get_instance_bindings, format_binding_status

            base_name = info.get("base_name", name)
            bindings = get_instance_bindings(base_name)
            bind_str = format_binding_status(bindings)
            lines.append(truncate_ansi(f"  bindings:     {bind_str}", width))

            if log_file:
                lines.append(truncate_ansi(f"  headless log: {log_file}", width))

            lines.append(truncate_ansi(f"  transcript:   {transcript}", width))

        # Show available action
        lines.append("")
        lines.append(truncate_ansi(f"{FG_CYAN}[Enter]{RESET} Stop hcom", width))

        return lines
