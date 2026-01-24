import os
import sys

# Dev mode re-exec: route to correct worktree's code
# Must happen before any other imports
dev_root = os.environ.get("HCOM_DEV_ROOT")
if dev_root:
    expected_src = os.path.join(dev_root, "src")
    current_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.realpath(current_src) != os.path.realpath(expected_src):
        # Running wrong worktree's code - re-exec with correct PYTHONPATH
        os.environ["PYTHONPATH"] = expected_src + os.pathsep + os.environ.get("PYTHONPATH", "")
        os.execvp(sys.executable, [sys.executable, "-m", "hcom"] + sys.argv[1:])

from .cli import main  # noqa: E402 - Must import after dev mode re-exec

if __name__ == "__main__":
    raise SystemExit(main())
