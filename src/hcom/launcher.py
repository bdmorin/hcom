"""Unified launcher for Claude, Gemini, and Codex.

Goal: feature parity across API, TUI, and CLI where possible.
Maintains existing CLI UX (hcom N, hcom N gemini, hcom N codex) while ensuring
launch batch tracking works consistently via life events and ready events.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal


LaunchTool = Literal["claude", "claude-pty", "gemini", "codex"]


def _normalize_tool(tool: str, *, pty: bool | None) -> LaunchTool:
    if tool == "claude" and pty:
        return "claude-pty"
    if tool in ("claude", "claude-pty", "gemini", "codex"):
        return tool  # type: ignore[return-value]
    raise ValueError(f"Unknown tool: {tool}")


def _default_max_count(tool: LaunchTool) -> int:
    # Keep existing CLI limits for Gemini/Codex.
    # Claude native supports 1-100; PTY mode is intentionally capped.
    match tool:
        case "claude":
            return 100
        case "claude-pty":
            return 20
        case "gemini" | "codex":
            return 10


def _default_tag() -> str:
    """Get configured tag from env/config.env/defaults."""
    try:
        from .core.config import get_config

        return get_config().tag
    except Exception:
        return ""


def _resolve_launcher_name(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        from .commands.utils import resolve_identity

        return resolve_identity().name
    except Exception:
        return "api"


def _is_inside_ai_tool() -> bool:
    """Detect if running inside Claude Code, Gemini CLI, or Codex."""
    from .shared import is_inside_ai_tool

    return is_inside_ai_tool()


def will_run_in_current_terminal(
    count: int,
    background: bool,
    run_here: bool | None = None,
) -> bool:
    """Predict if launch will block current terminal (run in same window).

    This is the single source of truth for the run_here decision logic.
    Used by both launcher.py (actual behavior) and lifecycle.py (TUI auto-open prediction).

    Args:
        count: Number of instances to launch
        background: Whether launching in headless/background mode
        run_here: Explicit override (None = use default logic)

    Returns:
        True if launch will run in current terminal (blocking), False if new window
    """
    if run_here is not None:
        return run_here
    # HCOM_TERMINAL=here: internal/debug - forces current terminal
    from .core.config import get_config

    if get_config().terminal == "here":
        return True
    # CRITICAL: AI tool check must come BEFORE shim check!
    # Shim env var is inherited by Claude instances; without this order,
    # agents would try to launch in current terminal (hijacking AI session).
    if _is_inside_ai_tool():
        return False  # Always new window when inside AI tool
    # Via shim: user typed 'claude' in their terminal, run there
    if os.environ.get("HCOM_VIA_SHIM"):
        return True
    if background:
        return False  # Background mode never blocks terminal
    return count == 1  # Single instance runs in current terminal


def _parse_codex_resume(args: list[str]) -> tuple[list[str], str | None, str | None]:
    """Parse codex resume/fork command to extract session_id and subcommand.

    Handles: codex resume <id>, codex fork <id>, codex --flags resume <id>, etc.
    Returns: (args, session_id or None, subcommand or None)
    """
    if not args:
        return [], None, None

    # Find "resume" or "fork" anywhere in args (may have flags before it)
    subcommand_idx = None
    subcommand = None
    for subcmd in ("resume", "fork"):
        try:
            idx = args.index(subcmd)
            subcommand_idx = idx
            subcommand = subcmd
            break
        except ValueError:
            continue

    if subcommand_idx is None:
        return args, None, None  # No resume/fork subcommand

    # Need session_id after subcommand
    if subcommand_idx + 1 >= len(args):
        raise ValueError(f"'codex {subcommand}' requires explicit thread-id (interactive picker not supported)")

    next_arg = args[subcommand_idx + 1]
    if next_arg == "--last":
        raise ValueError(f"'codex {subcommand} --last' not supported - use explicit thread-id")
    if next_arg.startswith("-"):
        raise ValueError(f"'codex {subcommand}' requires explicit thread-id (interactive picker not supported)")

    return args, next_arg, subcommand


def _get_system_prompt_path(tool: str) -> "Path":
    """Get stable path for system prompt file.

    Path: ~/.hcom/system-prompts/{tool}.md
    """
    from .core.paths import hcom_path

    prompts_dir = hcom_path("system-prompts")
    prompts_dir.mkdir(parents=True, exist_ok=True)

    return prompts_dir / f"{tool}.md"


def _write_system_prompt_file(system_prompt: str, tool: str) -> str:
    """Write system prompt to persistent file, return path.

    Only rewrites if content differs (avoids unnecessary disk writes).
    Files persist across restarts - no more temp files lost on restart.
    """
    filepath = _get_system_prompt_path(tool)

    # Only write if content differs
    try:
        existing = filepath.read_text(encoding="utf-8")
        if existing == system_prompt:
            return str(filepath)
    except FileNotFoundError:
        pass

    filepath.write_text(system_prompt, encoding="utf-8")
    return str(filepath)


def _ensure_hooks_installed(tool: str, include_permissions: bool) -> tuple[bool, str | None]:
    """Verify hooks are installed, setup if needed. Returns (success, error_message).

    This is the single source of truth for hook setup across all launch paths.
    Uses verify-first pattern: read-only check first, only write if needed.

    STRICT: Never launch if hooks aren't installed. If verify fails and setup
    fails (permission error, etc), return error - don't attempt launch.
    """
    if tool in ("claude", "claude-pty"):
        from .hooks.settings import verify_claude_hooks_installed, setup_claude_hooks

        try:
            if verify_claude_hooks_installed(check_permissions=include_permissions):
                return True, None
        except Exception as e:
            return False, f"Failed to verify Claude hooks: {e}. Run: hcom hooks add claude"

        # Not installed - try to setup
        try:
            if setup_claude_hooks(include_permissions=include_permissions):
                return True, None
            return False, f"Failed to setup Claude hooks. Run: hcom hooks add claude"
        except Exception as e:
            return False, f"Failed to setup Claude hooks: {e}. Run: hcom hooks add claude"

    elif tool == "gemini":
        from .tools.gemini.settings import (
            verify_gemini_hooks_installed,
            setup_gemini_hooks,
            get_gemini_version,
            GEMINI_MIN_VERSION,
        )

        # Version check (moved from callers)
        version = get_gemini_version()
        if version is not None and version < GEMINI_MIN_VERSION:
            min_str = ".".join(map(str, GEMINI_MIN_VERSION))
            cur_str = ".".join(map(str, version))
            return False, f"Gemini CLI {cur_str} is too old (requires {min_str}+). Update: npm i -g @google/gemini-cli@latest"

        try:
            if verify_gemini_hooks_installed(check_permissions=include_permissions):
                return True, None
        except Exception as e:
            return False, f"Failed to verify Gemini hooks: {e}. Run: hcom hooks add gemini"

        # Not installed - try to setup
        try:
            if setup_gemini_hooks(include_permissions=include_permissions):
                return True, None
            return False, f"Failed to setup Gemini hooks. Run: hcom hooks add gemini"
        except Exception as e:
            return False, f"Failed to setup Gemini hooks: {e}. Run: hcom hooks add gemini"

    elif tool == "codex":
        from .tools.codex.settings import verify_codex_hooks_installed, setup_codex_hooks

        try:
            if verify_codex_hooks_installed(check_permissions=include_permissions):
                return True, None
        except Exception as e:
            return False, f"Failed to verify Codex hooks: {e}. Run: hcom hooks add codex"

        # Not installed - try to setup
        try:
            if setup_codex_hooks(include_permissions=include_permissions):
                return True, None
            return False, f"Failed to setup Codex hooks. Run: hcom hooks add codex"
        except Exception as e:
            return False, f"Failed to setup Codex hooks: {e}. Run: hcom hooks add codex"

    return True, None  # Unknown tool - don't block


def launch(
    tool: str = "claude",
    count: int = 1,
    args: list[str] | None = None,
    *,
    tag: str | None = None,
    prompt: str | None = None,
    system_prompt: str | None = None,
    pty: bool | None = None,
    background: bool = False,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    launcher: str | None = None,
    run_here: bool | None = None,
) -> dict[str, Any]:
    """Launch tool instances with consistent batch tracking.

    Returns a dict compatible with the existing Claude-only launcher:
        {"batch_id", "launched", "failed", "background", "log_files"}
    Plus additional fields:
        {"tool", "handles"}
    """
    import os
    import uuid
    from .shared import (
        HcomError,
        RELEASED_TOOLS,
        RELEASED_BACKGROUND,
        skip_tool_args_validation,
        HCOM_SKIP_TOOL_ARGS_VALIDATION_ENV,
    )
    from .core.config import get_config
    from .core.db import init_db, get_last_event_id, log_event
    from .core.runtime import build_claude_env
    from .core.paths import hcom_path
    from .core.tool_utils import is_tool_installed

    normalized = _normalize_tool(tool, pty=pty)
    args = args or []
    skip_validation = skip_tool_args_validation()

    # Check release gates (safety net - UI should filter these)
    base_tool = normalized.replace("-pty", "")
    if base_tool not in RELEASED_TOOLS:
        raise HcomError(f"Unknown tool: {base_tool}")
    if background and base_tool not in RELEASED_BACKGROUND:
        raise HcomError(f"Background mode not available for {base_tool}")

    # Check if tool CLI is installed (includes fallback paths for claude)
    if not is_tool_installed(base_tool):
        raise HcomError(f"{base_tool} CLI not found")

    if count <= 0:
        raise HcomError("Count must be positive")

    max_count = _default_max_count(normalized)
    if count > max_count:
        raise HcomError(f"Too many {normalized} instances requested (max {max_count})")

    # Headless/background mode validation
    # Supported: claude, gemini, codex
    # Not supported: claude-pty (requires interactive terminal)
    if background and normalized == "claude-pty":
        raise HcomError("Claude PTY does not support headless/background mode")

    # Platform check - PTY modes require Unix-only APIs (pty, termios, fcntl)
    from .shared import IS_WINDOWS

    if IS_WINDOWS and normalized in ("gemini", "codex", "claude-pty"):
        tool_name = "Gemini" if normalized == "gemini" else "Codex" if normalized == "codex" else "Claude PTY"
        raise HcomError(
            f"{tool_name} integration requires PTY (pseudo-terminal) which is not available on Windows.\n"
            "Use 'hcom N claude' for Claude Code on Windows (hooks-based, no PTY required)."
        )

    init_db()

    # Ensure hooks are installed for the target tool (verify-first, write if needed)
    try:
        include_permissions = get_config().auto_approve
    except Exception:
        include_permissions = True

    hooks_ok, hooks_error = _ensure_hooks_installed(normalized, include_permissions)
    if not hooks_ok:
        # Strict: never launch without working hooks
        raise HcomError(hooks_error)

    working_dir = cwd or os.getcwd()
    launcher_name = _resolve_launcher_name(launcher)
    batch_id = str(uuid.uuid4()).split("-")[0]

    # Build base environment from config.env defaults (+ user overrides via caller-provided env).
    base_env: dict[str, str] = {}
    base_env.update(build_claude_env())
    if env:
        base_env.update(env)

    if tag is not None:
        # Explicit tag (including empty string) overrides any configured/default tag.
        effective_tag = tag
        base_env["HCOM_TAG"] = tag
    else:
        # Preserve any pre-configured HCOM_TAG from config.env or caller-provided env.
        if "HCOM_TAG" in base_env:
            effective_tag = base_env["HCOM_TAG"]
        else:
            effective_tag = _default_tag()
            if effective_tag:
                base_env["HCOM_TAG"] = effective_tag

    codex_args: list[str] | None = None
    codex_resume_thread_id: str | None = None
    codex_subcommand: str | None = None
    if normalized == "codex":
        codex_args, codex_resume_thread_id, codex_subcommand = _parse_codex_resume(args)
        if codex_subcommand == "fork":
            codex_resume_thread_id = None
        if codex_resume_thread_id and count > 1:
            raise HcomError(f"Cannot resume the same thread-id with multiple instances (count={count})")

    # Tool args validation (strict by default, overridable).
    # IMPORTANT: validation only; does not alter args/hcom behavior.
    if not skip_validation:
        validation_errors: list[str] = []
        if normalized in ("claude", "claude-pty"):
            from .tools.claude.args import resolve_claude_args

            spec = resolve_claude_args(args, None)
            validation_errors = list(spec.errors or ())
        elif normalized == "gemini":
            from .tools.gemini.args import resolve_gemini_args, validate_conflicts

            spec = resolve_gemini_args(args, None)
            validation_errors = list(spec.errors or ())
            # Check for headless mode rejection and other conflicts
            validation_errors.extend(validate_conflicts(spec))
        elif normalized == "codex":
            from .tools.codex.args import resolve_codex_args, validate_conflicts

            spec = resolve_codex_args(codex_args or args, None)
            validation_errors = list(spec.errors or ())
            # Check for exec mode rejection and other conflicts
            validation_errors.extend(validate_conflicts(spec))

        if validation_errors:
            tip = f"Tip: set {HCOM_SKIP_TOOL_ARGS_VALIDATION_ENV}=1 to bypass hcom validation and let {normalized.replace('-pty', '')} handle args."
            raise HcomError("\n".join([*validation_errors, tip]))

    # System prompt file for Gemini/Codex (Claude uses --system-prompt flag via args)
    # Uses persistent global paths that survive restarts
    system_prompt_file: str | None = None
    if system_prompt and normalized in ("gemini", "codex"):
        system_prompt_file = _write_system_prompt_file(system_prompt, normalized)
        if normalized == "gemini":
            base_env["GEMINI_SYSTEM_MD"] = system_prompt_file
        # Codex uses -c flag, handled in launch call

    import uuid
    from .core.instances import (
        generate_unique_name,
        initialize_instance_in_position_file,
        update_instance_position,
    )
    import json
    from .core.db import set_process_binding, delete_process_binding
    from .core.db import delete_instance

    launched = 0
    log_files: list[str] = []
    handles: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def _cleanup_instance(name: str, proc_id: str) -> None:
        try:
            delete_instance(name)
        except Exception:
            pass
        try:
            delete_process_binding(proc_id)
        except Exception:
            pass

    for _ in range(count):
        instance_env = base_env.copy()
        instance_env["HCOM_LAUNCHED"] = "1"
        instance_env["HCOM_LAUNCH_EVENT_ID"] = str(get_last_event_id())
        instance_env["HCOM_LAUNCHED_BY"] = launcher_name
        instance_env["HCOM_LAUNCH_BATCH_ID"] = batch_id
        # Propagate resolved HCOM_DIR to children for root consistency
        instance_env["HCOM_DIR"] = str(hcom_path())
        process_id = str(uuid.uuid4())
        instance_env["HCOM_PROCESS_ID"] = process_id
        instance_name = generate_unique_name()
        process_export_var = os.environ.get("HCOM_PROCESS_ID_EXPORT")
        if process_export_var:
            instance_env[process_export_var] = process_id
        # Name export: check env first, then config (env > file > defaults)
        name_export_var = os.environ.get("HCOM_NAME_EXPORT") or get_config().name_export
        if name_export_var:
            instance_env[name_export_var] = instance_name

        tool_type = "claude" if normalized in ("claude", "claude-pty") else normalized
        try:
            initialize_instance_in_position_file(
                instance_name,
                session_id=None,
                tool=tool_type,
                background=background,
            )
            set_process_binding(process_id, None, instance_name)
            if effective_tag:
                try:
                    update_instance_position(instance_name, {"tag": effective_tag})
                except Exception:
                    pass
        except Exception as e:
            errors.append({"tool": normalized, "error": str(e)})
            continue

        try:
            match normalized:
                case "claude":
                    from .terminal import launch_terminal
                    from .core.tool_utils import build_claude_command

                    claude_cmd = build_claude_command(args)
                    try:
                        update_instance_position(instance_name, {"launch_args": json.dumps(list(args or []))})
                    except Exception:
                        pass

                    if background:
                        import time
                        import random
                        from .core.paths import LOGS_DIR

                        log_filename = f"background_{int(time.time())}_{random.randint(1000, 9999)}.log"
                        instance_env["HCOM_BACKGROUND"] = log_filename
                        try:
                            update_instance_position(
                                instance_name,
                                {
                                    "background": True,
                                    "background_log_file": str(hcom_path(LOGS_DIR, log_filename)),
                                },
                            )
                        except Exception:
                            pass

                        result = launch_terminal(claude_cmd, instance_env, cwd=working_dir, background=True)
                        if isinstance(result, tuple):
                            log_file, pid = result
                            # Store PID for hcom kill
                            try:
                                update_instance_position(instance_name, {"pid": pid})
                            except Exception:
                                pass
                            launched += 1
                            log_files.append(str(log_file))
                            handles.append(
                                {
                                    "tool": "claude",
                                    "instance_name": instance_name,
                                    "log_file": str(log_file),
                                    "pid": pid,
                                }
                            )
                        else:
                            _cleanup_instance(instance_name, process_id)
                    else:
                        effective_run_here = will_run_in_current_terminal(count, False, run_here)
                        success = launch_terminal(
                            claude_cmd,
                            instance_env,
                            cwd=working_dir,
                            run_here=effective_run_here,
                        )
                        if success:
                            launched += 1
                            handles.append({"tool": "claude", "instance_name": instance_name})
                        else:
                            _cleanup_instance(instance_name, process_id)

                case "claude-pty":
                    from .pty.claude import launch_claude_pty
                    from .tools.claude.args import resolve_claude_args

                    # Strip default prompt for PTY mode (contactable via inject, doesn't need kickstart)
                    pty_spec = resolve_claude_args(args, None)
                    if pty_spec.positional_tokens == ("say hi in hcom chat",):
                        args = pty_spec.rebuild_tokens(include_positionals=False)
                    try:
                        update_instance_position(instance_name, {"launch_args": json.dumps(list(args or []))})
                    except Exception:
                        pass

                    effective_run_here = will_run_in_current_terminal(count, False, run_here)
                    name = launch_claude_pty(
                        working_dir,
                        instance_env,
                        instance_name,
                        tag=effective_tag,
                        claude_args=args,
                        run_here=effective_run_here,
                    )
                    if name:
                        launched += 1
                        handles.append({"tool": "claude-pty", "instance_name": instance_name})
                    else:
                        _cleanup_instance(instance_name, process_id)

                case "gemini":
                    effective_run_here = will_run_in_current_terminal(count, False, run_here)

                    try:
                        update_instance_position(instance_name, {"launch_args": json.dumps(list(args or []))})
                    except Exception:
                        pass

                    from .pty.gemini import launch_gemini_pty

                    name = launch_gemini_pty(
                        working_dir,
                        instance_env,
                        instance_name,
                        tag=effective_tag,
                        gemini_args=list(args or []),
                        run_here=effective_run_here,
                    )
                    if name:
                        launched += 1
                        handles.append({"tool": "gemini", "instance_name": instance_name})
                    else:
                        _cleanup_instance(instance_name, process_id)

                case "codex":
                    # Bootstrap delivered via developer_instructions at launch - mark announced
                    try:
                        update_instance_position(instance_name, {"name_announced": True})
                    except Exception:
                        pass

                    # Add developer instructions if system prompt provided
                    # Uses developer_instructions (adds to context) not experimental_instructions_file
                    # (which replaces system prompt and fails validation with GPT-5 models)
                    effective_codex_args = list(codex_args or args or [])
                    if system_prompt:
                        effective_codex_args = [
                            "-c",
                            f"developer_instructions={system_prompt}",
                        ] + effective_codex_args
                    try:
                        update_instance_position(
                            instance_name,
                            {"launch_args": json.dumps(list(effective_codex_args))},
                        )
                    except Exception:
                        pass

                    from .pty.codex import launch_codex_pty

                    effective_run_here = will_run_in_current_terminal(count, False, run_here)
                    name = launch_codex_pty(
                        working_dir,
                        instance_env,
                        instance_name,
                        codex_args=effective_codex_args,
                        resume_thread_id=codex_resume_thread_id,
                        run_here=effective_run_here,
                    )
                    if name:
                        launched += 1
                        handles.append(
                            {
                                "tool": "codex",
                                "instance_name": instance_name,
                            }
                        )
                    else:
                        _cleanup_instance(instance_name, process_id)
        except HcomError:
            _cleanup_instance(instance_name, process_id)
            raise
        except Exception as e:
            # Best-effort: continue launching remaining instances, but collect errors
            # for visibility so callers can surface "what went wrong".
            try:
                _cleanup_instance(instance_name, process_id)
                errors.append({"tool": normalized, "error": str(e)})
            except Exception:
                pass

    failed = count - launched
    if launched == 0:
        if errors:
            error_details = "; ".join(e.get("error", "unknown") for e in errors)
            raise HcomError(f"No instances launched (0/{count}): {error_details}")
        raise HcomError(f"No instances launched (0/{count})")

    # Log batch launch event (enables `hcom events launch` + TUI batch banner).
    try:
        log_event(
            "life",
            launcher_name,
            {
                "action": "batch_launched",
                "by": launcher_name,
                "batch_id": batch_id,
                "tool": normalized,
                "count_requested": count,
                "launched": launched,
                "failed": failed,
                "background": bool(background),
                "tag": effective_tag or "",
            },
        )
    except Exception:
        pass

    # Push launch event to relay (notify TUI if running, else inline push)
    try:
        from .relay import notify_relay_tui, push

        if not notify_relay_tui():
            push()
    except Exception:
        pass

    return {
        "tool": normalized,
        "batch_id": batch_id,
        "launched": launched,
        "failed": failed,
        "background": bool(background),
        "log_files": log_files,
        "handles": handles,
        "errors": errors,
    }


__all__ = ["launch", "LaunchTool", "will_run_in_current_terminal"]
