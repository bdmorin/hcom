"""Thread-local I/O capture for concurrent daemon requests.

Enables concurrent CLI requests without corrupting each other's output.
Each thread sets its capture buffer; the wrapper delegates writes to it.

Usage:
    # At daemon startup (once):
    install_thread_local_streams()

    # Per-request:
    stdout_capture = CaptureBuffer(is_tty=True)
    _thread_streams.stdout = stdout_capture
    try:
        # ... run CLI command ...
        output = stdout_capture.getvalue()
    finally:
        _thread_streams.stdout = None
"""

from __future__ import annotations

import io
import sys
import threading

# Thread-local storage for per-request stream capture
_thread_streams = threading.local()


class ThreadLocalStream:
    """Stream wrapper that delegates to thread-local buffers.

    When a thread sets _thread_streams.stdout, all writes go there.
    When not set, writes go to the fallback (real stdout/stderr).

    This allows concurrent daemon requests to capture output independently
    without a global lock on sys.stdout assignment.
    """

    def __init__(self, fallback, attr_name: str):
        """
        Args:
            fallback: Real stream to use when no thread-local buffer set
            attr_name: Attribute name in _thread_streams ('stdout' or 'stderr')
        """
        self._fallback = fallback
        self._attr_name = attr_name

    def _get_stream(self):
        """Get current thread's stream or fallback."""
        return getattr(_thread_streams, self._attr_name, None) or self._fallback

    def write(self, s: str) -> int:
        return self._get_stream().write(s)

    def flush(self) -> None:
        stream = self._get_stream()
        if hasattr(stream, 'flush'):
            stream.flush()

    def isatty(self) -> bool:
        stream = self._get_stream()
        if hasattr(stream, 'isatty'):
            return stream.isatty()
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._fallback, 'encoding', 'utf-8')

    @property
    def errors(self) -> str:
        return getattr(self._fallback, 'errors', 'strict')

    def fileno(self) -> int:
        # Only works for fallback (real fd), not StringIO captures
        return self._fallback.fileno()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False


class CaptureBuffer:
    """Per-thread output capture buffer with custom isatty()."""

    def __init__(self, is_tty: bool = False):
        self._buffer = io.StringIO()
        self._is_tty = is_tty

    def write(self, s: str) -> int:
        return self._buffer.write(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self._is_tty

    def getvalue(self) -> str:
        return self._buffer.getvalue()


class MockStdin:
    """StringIO wrapper that returns custom isatty() value."""

    def __init__(self, content: str, is_tty: bool):
        self._buffer = io.StringIO(content)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def read(self, *args, **kwargs):
        return self._buffer.read(*args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._buffer.readline(*args, **kwargs)

    def __iter__(self):
        return iter(self._buffer)


class ThreadLocalStdin:
    """Stream wrapper that delegates reads to thread-local stdin buffer.

    Mirrors ThreadLocalStream but for input. When a thread sets
    _thread_streams.stdin, all reads come from that buffer.
    When not set, reads come from the fallback (real stdin).
    """

    def __init__(self, fallback):
        self._fallback = fallback

    def _get_stream(self):
        return getattr(_thread_streams, 'stdin', None) or self._fallback

    def read(self, *args, **kwargs):
        return self._get_stream().read(*args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._get_stream().readline(*args, **kwargs)

    def isatty(self) -> bool:
        stream = self._get_stream()
        if hasattr(stream, 'isatty'):
            return stream.isatty()
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._fallback, 'encoding', 'utf-8')

    def fileno(self) -> int:
        return self._fallback.fileno()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def __iter__(self):
        return iter(self._get_stream())


class MockStdout:
    """Wrapper around real stdout that returns custom isatty() value."""

    def __init__(self, real_stdout, is_tty: bool):
        self._real = real_stdout
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty

    def write(self, *args, **kwargs):
        return self._real.write(*args, **kwargs)

    def flush(self, *args, **kwargs):
        return self._real.flush(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_thread_local_streams() -> None:
    """Install thread-local stream wrappers on sys.stdout/stderr.

    Call once at daemon startup. After this, each thread can set
    _thread_streams.stdout/_thread_streams.stderr to capture output.
    """
    # Only install if not already installed
    if isinstance(sys.stdout, ThreadLocalStream):
        return

    sys.stdout = ThreadLocalStream(sys.__stdout__, 'stdout')
    sys.stderr = ThreadLocalStream(sys.__stderr__, 'stderr')
    sys.stdin = ThreadLocalStdin(sys.__stdin__)


__all__ = [
    "_thread_streams",
    "ThreadLocalStream",
    "ThreadLocalStdin",
    "CaptureBuffer",
    "MockStdin",
    "MockStdout",
    "install_thread_local_streams",
]
