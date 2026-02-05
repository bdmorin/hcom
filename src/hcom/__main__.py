import os
import sys

# Dev mode re-exec: route to correct worktree's code
# Must happen before any other imports
_dev_root = os.environ.get("HCOM_DEV_ROOT")
if _dev_root:
    _expected_src = os.path.join(_dev_root, "src")
    _current_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.realpath(_current_src) != os.path.realpath(_expected_src):
        # Running wrong worktree's code - re-exec with correct PYTHONPATH
        os.environ["PYTHONPATH"] = _expected_src + os.pathsep + os.environ.get("PYTHONPATH", "")
        os.execvp(sys.executable, [sys.executable, "-m", "hcom"] + sys.argv[1:])
    # Set PYTHONPATH for daemon to load worktree code
    _existing = os.environ.get("PYTHONPATH", "")
    if _expected_src not in _existing:
        os.environ["PYTHONPATH"] = _expected_src + os.pathsep + _existing if _existing else _expected_src


def main():
    """Entry point: exec Rust binary if available, otherwise fall back to Python."""
    from .core.binary import get_native_binary

    # Try Rust binary for fast daemon-based execution
    # Skip if HCOM_PYTHON_FALLBACK is set (Rust already tried and fell back to us)
    native_bin = get_native_binary()
    if native_bin and not os.environ.get("HCOM_PYTHON_FALLBACK"):
        try:
            # Exec replaces this process with the Rust binary
            os.execvp(native_bin, [native_bin] + sys.argv[1:])
            # execvp doesn't return on success
        except OSError:
            # Binary might be busy (being rebuilt) or corrupted - fall through to Python
            pass

    # Fallback: direct Python execution (slower but always works)
    from .cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
