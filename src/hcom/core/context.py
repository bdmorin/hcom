"""Launch context capture - environment snapshot for disambiguation and audit."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any


# Env vars to capture (if set)
CONTEXT_ENV_VARS = [
    # Terminal identification
    "TERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",
    "COLORTERM",
    "VTE_VERSION",
    "WINDOWID",
    # iTerm2
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    # Kitty
    "KITTY_WINDOW_ID",
    "KITTY_PID",
    "KITTY_LISTEN_ON",
    # Alacritty
    "ALACRITTY_WINDOW_ID",
    # WezTerm
    "WEZTERM_PANE",
    "WEZTERM_CONFIG_DIR",
    # GNOME/KDE
    "GNOME_TERMINAL_SCREEN",
    "KONSOLE_DBUS_SESSION",
    "KONSOLE_DBUS_WINDOW",
    "KONSOLE_PROFILE_NAME",
    # Other Linux terminals
    "TERMINATOR_UUID",
    "TILIX_ID",
    "GUAKE_TAB_UUID",
    # Windows Terminal
    "WT_SESSION",
    "WT_PROFILE_ID",
    # ConEmu
    "ConEmuHWND",
    "ConEmuPID",
    "ConEmuDrawHWND",
    "ConEmuServerPID",
    "ConEmuANSI",
    "ConEmuBuild",
    "CMDER_ROOT",
    # Multiplexers
    "TMUX",
    "TMUX_PANE",
    "TMUX_TMPDIR",
    "STY",
    "WINDOW",
    "ZELLIJ",
    "ZELLIJ_SESSION_NAME",
    "ZELLIJ_PANE_ID",
    "BYOBU_WINDOWS",
    # SSH
    "SSH_CLIENT",
    "SSH_TTY",
    "SSH_CONNECTION",
    "SSH_AUTH_SOCK",
    # WSL/Container
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    # IDE terminals
    "VSCODE_GIT_IPC_HANDLE",
    "VSCODE_INJECTION",
    "VSCODE_PID",
    "VSCODE_CWD",
    "CURSOR_AGENT",
    "INSIDE_EMACS",
    "VIM",
    "VIMRUNTIME",
    "NVIM_LISTEN_ADDRESS",
    "TERMINAL_EMULATOR",
    # Cloud IDEs
    "CODESPACES",
    "CODESPACE_NAME",
    "CLOUD_SHELL",
    "GOOGLE_CLOUD_PROJECT",
    "DEVSHELL_PROJECT_ID",
    "REPL_ID",
    "REPL_SLUG",
    "REPL_OWNER",
    "GITPOD_WORKSPACE_ID",
    "GITPOD_WORKSPACE_URL",
    "PROJECT_DOMAIN",
    "PROJECT_ID",
    # System
    "USER",
    "HOSTNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TZ",
    # Cloud/Container
    "AWS_PROFILE",
    "AWS_REGION",
    "DOCKER_HOST",
    "KUBERNETES_SERVICE_HOST",
]


def _run_quiet(cmd: list[str], timeout: float = 1.0) -> str:
    """Run command and return stdout, empty string on any error."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def capture_context() -> dict[str, Any]:
    """Capture launch context snapshot.

    Returns dict with:
        - git_branch: current git branch (empty if not in repo)
        - tty: tty device path (empty if not a tty)
        - env: dict of set env vars from CONTEXT_ENV_VARS
    """
    ctx: dict[str, Any] = {}

    # Git branch
    ctx["git_branch"] = _run_quiet(["git", "branch", "--show-current"])

    # TTY device
    ctx["tty"] = _run_quiet(["tty"])

    # Env vars (only include if set)
    env: dict[str, str] = {}
    for var in CONTEXT_ENV_VARS:
        val = os.environ.get(var)
        if val:
            env[var] = val
    ctx["env"] = env

    return ctx


def capture_context_json() -> str:
    """Capture context and return as JSON string for DB storage."""
    return json.dumps(capture_context(), separators=(",", ":"))


__all__ = ["CONTEXT_ENV_VARS", "capture_context", "capture_context_json"]
