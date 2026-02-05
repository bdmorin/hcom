"""Resume and fork commands."""

from ..utils import CLIError, resolve_identity
from ...shared import IS_WINDOWS
from ...core.thread_context import get_cwd
from ...core.instances import load_instance_position


def _load_stopped_snapshot(name: str) -> dict | None:
    """Load instance snapshot from stopped events."""
    from ...core.db import get_db
    import json

    row = get_db().execute(
        "SELECT json_extract(data, '$.snapshot') FROM events"
        " WHERE type='life' AND instance=? AND json_extract(data, '$.action')='stopped'"
        " ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row and row[0]:
        return json.loads(row[0])
    return None


def _do_resume(name: str, prompt: str | None = None, *, run_here: bool | None = None, fork: bool = False) -> int:
    """Resume or fork an instance by launching tool with --resume and session_id.

    Used by TUI [R] key, `hcom r NAME`, and `hcom f NAME`.

    Args:
        name: Instance name to resume/fork.
        prompt: Optional prompt to pass to instance.
        run_here: If False, force new terminal window. If None, use default logic.
        fork: If True, fork the session (new instance) instead of resuming.
    """
    from ...launcher import launch as unified_launch

    # Look up instance data — fork allows active, resume requires stopped
    active_data = load_instance_position(name)
    if fork:
        # Fork works on active or stopped
        if active_data:
            instance_data = dict(active_data)
        else:
            stopped_data = _load_stopped_snapshot(name)
            if not stopped_data:
                raise CLIError(f"'{name}' not found (not active or stopped)")
            instance_data = stopped_data
    else:
        # Resume requires stopped
        if active_data:
            raise CLIError(f"'{name}' is still active — run hcom stop {name} first")
        stopped_data = _load_stopped_snapshot(name)
        if not stopped_data:
            raise CLIError(f"'{name}' not found in stopped instances")
        instance_data = stopped_data

    session_id = instance_data.get("session_id")
    if not session_id:
        raise CLIError(f"'{name}' has no session_id (cannot {'fork' if fork else 'resume'})")

    tool = instance_data.get("tool", "claude")
    if fork and tool not in ("claude", "codex"):
        raise CLIError(f"Fork not supported for {tool}")

    is_headless = bool(instance_data.get("background", False))
    original_dir = instance_data.get("directory") or str(get_cwd())

    # System prompt
    if fork:
        system_prompt = (
            f"YOU ARE A FORK of agent '{name}'. "
            f"You have the same session history but are a NEW agent. "
            f"Run hcom start to get your own identity."
        )
    else:
        # For resume, we pass the original name to launch() so no reclaim needed
        system_prompt = f"YOUR SESSION HAS BEEN RESUMED! You are still '{name}'."

    # Build args
    if tool == "claude":
        args = ["--resume", session_id]
        if fork:
            args.append("--fork-session")
        if is_headless:
            args.append("-p")
        if prompt:
            args.append(prompt)
    elif tool == "gemini":
        args = ["--resume", session_id]
    elif tool == "codex":
        args = ["fork" if fork else "resume", session_id]
    else:
        raise CLIError(f"{'Fork' if fork else 'Resume'} not supported for tool: {tool}")

    # Get launcher name
    try:
        launcher_name = resolve_identity().name
    except Exception:
        launcher_name = "user"

    # PTY mode: use PTY wrapper for interactive Claude (not headless, not Windows)
    use_pty = tool == "claude" and not is_headless and not IS_WINDOWS

    # Launch in original directory
    # For resume (not fork), reuse the original instance name
    result = unified_launch(
        tool,
        1,
        args,
        launcher=launcher_name,
        background=is_headless,
        system_prompt=system_prompt,
        prompt=prompt if is_headless else None,
        run_here=run_here,
        cwd=original_dir,
        pty=use_pty,
        name=name if not fork else None,
    )

    launched = result["launched"]
    if launched == 1:
        print(f"{'Forked' if fork else 'Resumed'} {name} ({tool})")
        return 0
    else:
        return 1


def cmd_resume(argv: list[str], *, ctx=None) -> int:
    """Resume a stopped instance: hcom r NAME"""
    if not argv or argv[0] in ("--help", "-h"):
        print("Usage: hcom r NAME")
        print("Resume a stopped agent session")
        return 0
    name = argv[0]
    return _do_resume(name)


def cmd_fork(argv: list[str], *, ctx=None) -> int:
    """Fork an agent session: hcom f NAME"""
    if not argv or argv[0] in ("--help", "-h"):
        print("Usage: hcom f NAME")
        print("Fork an agent session (active or stopped) into a new instance")
        return 0
    name = argv[0]
    return _do_resume(name, fork=True)
