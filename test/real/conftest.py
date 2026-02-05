"""Conftest for real/ folder - standalone scripts, not pytest tests.

These scripts require real environment (transcripts, tmux, CLI tools installed).
Run them directly: `python test/public/real/test_pty_delivery.py`

See README.md for usage.
"""


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001
    """Ignore this entire directory during pytest collection."""
    return True
