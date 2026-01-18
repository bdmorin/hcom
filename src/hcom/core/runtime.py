"""Runtime utilities - shared between hooks and commands
NOTE: bootstrap/launch context text here is injected into Claude's context via hooks, human user never sees it."""

from __future__ import annotations
import socket

from .paths import hcom_path, CONFIG_FILE
from ..shared import parse_env_file
from .instances import load_instance_position

# Re-export from bootstrap module for backward compatibility
from .bootstrap import build_hcom_bootstrap_text, get_bootstrap  # noqa: F401


def build_claude_env() -> dict[str, str]:
    """Load config.env as environment variable defaults.

    Returns all vars from config.env (including HCOM_*).
    Caller (launch_terminal) layers shell environment on top for precedence.
    """
    env = {}

    # Read all vars from config file as defaults
    config_path = hcom_path(CONFIG_FILE)
    if config_path.exists():
        file_config = parse_env_file(config_path)
        for key, value in file_config.items():
            if value == "":
                continue  # Skip blank values
            env[key] = str(value)

    return env


def _truncate_val(key: str, v: str, max_len: int = 80) -> str:
    """Truncate long config values for display.

    HCOM_CLAUDE_ARGS gets special handling - parse args and only truncate
    long string values (prompts), preserving flags.
    Sensitive values (tokens) are masked.
    """
    # Mask sensitive values
    if key in ("HCOM_RELAY_TOKEN",) and v:
        return f"{v[:4]}***" if len(v) > 4 else "***"
    if key == "HCOM_CLAUDE_ARGS" and len(v) > max_len:
        import shlex

        try:
            args = shlex.split(v)
            truncated = []
            for arg in args:
                # Truncate long non-flag args (prompts)
                if not arg.startswith("-") and len(arg) > 60:
                    truncated.append(f"{arg[:57]}...")
                else:
                    truncated.append(arg)
            return shlex.join(truncated)
        except ValueError:
            pass  # shlex parse error, fall through to simple truncate
    return f"{v[:max_len]}..." if len(v) > max_len else v


def notify_instance(instance_name: str, timeout: float = 0.05) -> None:
    """Send TCP notification to specific instance."""
    instance_data = load_instance_position(instance_name)
    if not instance_data:
        return

    ports: list[int] = []
    try:
        from .db import list_notify_ports

        ports.extend(list_notify_ports(instance_name))
    except Exception:
        pass

    if not ports:
        return

    # Dedup while preserving order
    seen = set()
    deduped: list[int] = []
    for p in ports:
        if p and p not in seen:
            deduped.append(p)
            seen.add(p)

    from .db import delete_notify_endpoint

    for port in deduped:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
                sock.send(b"\n")
        except Exception:
            # Best effort prune: if a port is dead, remove from notify_endpoints.
            try:
                delete_notify_endpoint(instance_name, port=port)
            except Exception:
                pass


def notify_all_instances(timeout: float = 0.05) -> None:
    """Send TCP wake notifications to all instance notify ports.

    Best effort - connection failures ignored. Polling fallback ensures
    message delivery even if all notifications fail.

    Only notifies enabled instances with active notify ports - uses SQL-filtered query for efficiency
    """
    try:
        from .db import get_db, delete_notify_endpoint

        conn = get_db()

        # Prefer notify_endpoints (supports multiple concurrent listeners per instance).
        # Row exists = participating (no enabled filter needed)
        rows = conn.execute(
            """
            SELECT ne.instance AS name, ne.port AS port
            FROM notify_endpoints ne
            JOIN instances i ON i.name = ne.instance
            WHERE ne.port > 0
            """
        ).fetchall()

        # Dedup (name, port)
        seen: set[tuple[str, int]] = set()
        targets: list[tuple[str, int]] = []
        for row in rows:
            try:
                k = (row["name"], int(row["port"]))
            except Exception:
                continue
            if k in seen:
                continue
            seen.add(k)
            targets.append(k)

        for name, port in targets:
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=timeout
                ) as sock:
                    sock.send(b"\n")
            except Exception:
                # Best-effort prune for notify_endpoints rows.
                try:
                    delete_notify_endpoint(name, port=port)
                except Exception:
                    pass

    except Exception:
        return
