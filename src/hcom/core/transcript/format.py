"""Formatters for transcript thread and timeline data."""

from __future__ import annotations

from .entries import summarize_action


# =============================================================================
# Formatters
# =============================================================================


def format_thread(thread_data: dict, instance: str = "", full: bool = False) -> str:
    """Format thread data for human-readable output."""
    exchanges = thread_data.get("exchanges", [])
    total = thread_data.get("total", len(exchanges))
    error = thread_data.get("error")

    if error:
        return f"Error: {error}"
    if not exchanges:
        return "No conversation exchanges found."

    # Build header with position info
    lines = []
    first_pos = exchanges[0].get("position", 1)
    last_pos = exchanges[-1].get("position", len(exchanges))
    header = f"Recent conversation ({len(exchanges)} exchanges, {first_pos}-{last_pos} of {total})"
    if instance:
        header += f" - @{instance}"
    lines.append(header + ":")
    lines.append("")

    for ex in exchanges:
        pos = ex.get("position", "?")
        user = ex["user"]
        if len(user) > 300:
            user = user[:297] + "..."
        lines.append(f"[{pos}] USER: {user}")

        action = ex["action"]
        if full:
            lines.append(f"ASSISTANT: {action}")
        else:
            lines.append(f"ASSISTANT: {summarize_action(action)}")

        if ex["files"]:
            lines.append(f"FILES: {', '.join(ex['files'])}")
        lines.append("")

    # Add hints
    if not full:
        lines.append("Note: Output truncated. Use --full for full text.")
    else:
        lines.append("Note: Tool outputs & file edits hidden. Use --detailed for full details.")

    return "\n".join(lines).rstrip()


def format_thread_detailed(thread_data: dict, instance: str = "") -> str:
    """Format detailed thread data for watcher-style review."""
    exchanges = thread_data.get("exchanges", [])
    total = thread_data.get("total", len(exchanges))
    error = thread_data.get("error")
    ended_on_error = thread_data.get("ended_on_error", False)

    if error:
        return f"Error: {error}"
    if not exchanges:
        return "No conversation exchanges found."

    # Build header with position info
    lines = []
    first_pos = exchanges[0].get("position", 1)
    last_pos = exchanges[-1].get("position", len(exchanges))
    header = f"Detailed review ({len(exchanges)} exchanges, {first_pos}-{last_pos} of {total})"
    if instance:
        header += f" - @{instance}"
    if ended_on_error:
        header += " [ENDED ON ERROR]"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")

    for ex in exchanges:
        pos = ex.get("position", "?")
        user = ex["user"]
        if len(user) > 100:
            user = user[:97] + "..."
        lines.append(f'[{pos}] "{user}"')

        # Tools executed
        for tool in ex.get("tools", []):
            _format_tool_line(lines, tool)

        # Edits with diffs
        for edit in ex.get("edits", []):
            _format_edit_lines(lines, edit)

        # Errors
        for err in ex.get("errors", []):
            _format_error_lines(lines, err)

        if ex.get("ended_on_error"):
            lines.append("  └─ [ENDED ON ERROR]")
        lines.append("")

    return "\n".join(lines).rstrip()


def _format_tool_line(lines: list[str], tool: dict) -> None:
    """Format a single tool execution line."""
    prefix = "  ✗" if tool.get("is_error") else "  ├─"
    name = tool.get("name", "unknown")

    if name == "Bash":
        cmd = tool.get("command", "")[:60]
        suffix = " → ERROR" if tool.get("is_error") else ""
        lines.append(f"{prefix} Bash: {cmd}{suffix}")
    elif name == "Edit":
        lines.append(f"{prefix} Edit: {tool.get('file', '')}")
    elif name in ("Read", "Glob", "Grep"):
        target = tool.get("target", "")
        if len(target) > 50:
            target = "..." + target[-47:]
        lines.append(f"{prefix} {name}: {target}")
    else:
        lines.append(f"{prefix} {name}")


def _format_edit_lines(lines: list[str], edit: dict) -> None:
    """Format edit diff lines."""
    lines.append(f"  │ Edit {edit.get('file', '')}:")
    diff = edit.get("diff", "")
    diff_split = diff.split("\n")
    for diff_line in diff_split[:10]:
        lines.append(f"  │   {diff_line}")
    if len(diff_split) > 10:
        lines.append(f"  │   ... +{len(diff_split) - 10} more lines")


def _format_error_lines(lines: list[str], err: dict) -> None:
    """Format error lines."""
    lines.append(f"  ✗ ERROR ({err.get('tool', 'unknown')}):")
    content = err.get("content", "")[:200]
    for err_line in content.split("\n")[:3]:
        lines.append(f"  ✗   {err_line}")


def format_timeline(timeline_data: dict, full: bool = False) -> str:
    """Format timeline data for human-readable output."""
    entries = timeline_data.get("entries", [])
    error = timeline_data.get("error")

    if error:
        return f"Error: {error}"
    if not entries:
        return "No conversation exchanges found."

    lines = [f"Timeline ({len(entries)} exchanges):", ""]

    for entry in entries:
        # Parse timestamp for display
        ts = entry.get("timestamp", "")
        if ts:
            # Extract time portion (HH:MM) from ISO timestamp
            try:
                time_part = ts.split("T")[1][:5] if "T" in ts else ts[:5]
            except (IndexError, TypeError):
                time_part = "??:??"
        else:
            time_part = "??:??"

        user = entry.get("user", "")
        if len(user) > 80:
            user = user[:77] + "..."

        lines.append(f'[{time_part}] "{user}"')

        action = entry.get("action", "")
        if full:
            # Show full action
            for action_line in action.split("\n")[:10]:
                lines.append(f"  {action_line}")
            if action.count("\n") > 10:
                lines.append(f"  ... (+{action.count(chr(10)) - 10} lines)")
        else:
            # Summarized action
            lines.append(f"  → {summarize_action(action, max_len=100)}")

        if entry.get("files"):
            lines.append(f"  Files: {', '.join(entry['files'][:5])}")

        lines.append(f"  {entry.get('command', '')}")
        lines.append("")

    return "\n".join(lines).rstrip()


def format_timeline_detailed(timeline_data: dict) -> str:
    """Format timeline data with tool details."""
    entries = timeline_data.get("entries", [])
    error = timeline_data.get("error")

    if error:
        return f"Error: {error}"
    if not entries:
        return "No conversation exchanges found."

    lines = [f"Timeline ({len(entries)} exchanges) [detailed]", "=" * 40, ""]

    for entry in entries:
        # Parse timestamp
        ts = entry.get("timestamp", "")
        try:
            time_part = ts.split("T")[1][:5] if "T" in ts else ts[:5]
        except (IndexError, TypeError):
            time_part = "??:??"

        user = entry.get("user", "")
        if len(user) > 100:
            user = user[:97] + "..."

        lines.append(f'[{time_part}] "{user}"')

        # Tools executed
        for tool in entry.get("tools", []):
            _format_tool_line(lines, tool)

        # Edits
        for edit in entry.get("edits", []):
            _format_edit_lines(lines, edit)

        # Errors
        for err in entry.get("errors", []):
            _format_error_lines(lines, err)

        lines.append(f"  {entry.get('command', '')}")
        lines.append("")

    return "\n".join(lines).rstrip()
