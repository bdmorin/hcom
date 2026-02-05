"""Unit tests for thread_context thread-safe accessors.

Tests that concurrent threads with different contexts see correct values.
"""

import os
import time
import threading
import pytest
from pathlib import Path

from hcom.core.hcom_context import HcomContext
from hcom.core.thread_context import (
    get_process_id,
    get_is_launched,
    get_is_pty_mode,
    get_background_name,
    get_hcom_dir,
    get_cwd,
    get_launched_by,
    with_context,
)


class TestAccessorFallback:
    """Test that accessors fall back to os.environ when contextvar not set."""

    def test_process_id_fallback(self):
        """get_process_id() falls back to os.environ."""
        os.environ.pop("HCOM_PROCESS_ID", None)
        assert get_process_id() is None

        os.environ["HCOM_PROCESS_ID"] = "fallback-proc"
        try:
            assert get_process_id() == "fallback-proc"
        finally:
            os.environ.pop("HCOM_PROCESS_ID", None)

    def test_is_launched_fallback(self):
        """get_is_launched() falls back to os.environ."""
        os.environ.pop("HCOM_LAUNCHED", None)
        assert get_is_launched() is False

        os.environ["HCOM_LAUNCHED"] = "1"
        try:
            assert get_is_launched() is True
        finally:
            os.environ.pop("HCOM_LAUNCHED", None)

    def test_background_name_fallback(self):
        """get_background_name() falls back to os.environ."""
        os.environ.pop("HCOM_BACKGROUND", None)
        assert get_background_name() is None

        os.environ["HCOM_BACKGROUND"] = "bg.log"
        try:
            assert get_background_name() == "bg.log"
        finally:
            os.environ.pop("HCOM_BACKGROUND", None)

    def test_cwd_fallback(self):
        """get_cwd() falls back to Path.cwd()."""
        # Without context, returns actual cwd
        assert get_cwd() == Path.cwd()


class TestWithContext:
    """Test with_context() context manager."""

    def test_sets_process_id(self):
        """with_context sets process_id in contextvar."""
        ctx = HcomContext.from_env({"HCOM_PROCESS_ID": "ctx-proc"}, "/tmp")

        # Before context
        os.environ.pop("HCOM_PROCESS_ID", None)
        assert get_process_id() is None

        with with_context(ctx):
            # Inside context - contextvar has value
            assert get_process_id() == "ctx-proc"

        # After context - back to fallback
        assert get_process_id() is None

    def test_sets_is_launched(self):
        """with_context sets is_launched in contextvar."""
        ctx = HcomContext.from_env({"HCOM_LAUNCHED": "1"}, "/tmp")

        os.environ.pop("HCOM_LAUNCHED", None)
        assert get_is_launched() is False

        with with_context(ctx):
            assert get_is_launched() is True

        assert get_is_launched() is False

    def test_sets_cwd(self):
        """with_context sets cwd in contextvar."""
        ctx = HcomContext.from_env({}, "/custom/cwd")

        with with_context(ctx):
            assert get_cwd() == Path("/custom/cwd")

        # After context - back to actual cwd
        assert get_cwd() == Path.cwd()

    def test_sets_hcom_dir(self):
        """with_context sets hcom_dir in contextvar."""
        ctx = HcomContext.from_env({"HCOM_DIR": "/custom/hcom"}, "/tmp")

        os.environ.pop("HCOM_DIR", None)

        with with_context(ctx):
            assert get_hcom_dir() == Path("/custom/hcom")

        # After context - back to fallback (None since env not set)
        assert get_hcom_dir() is None

    def test_restores_on_exception(self):
        """Context vars are restored even on exception."""
        ctx = HcomContext.from_env({"HCOM_PROCESS_ID": "exc-proc"}, "/tmp")

        os.environ.pop("HCOM_PROCESS_ID", None)

        with pytest.raises(ValueError):
            with with_context(ctx):
                assert get_process_id() == "exc-proc"
                raise ValueError("test error")

        # Restored after exception
        assert get_process_id() is None


class TestConcurrentContexts:
    """Test thread-safety with concurrent contexts."""

    def test_threads_see_own_context(self):
        """Each thread sees its own context values."""
        results: dict[str, str | None] = {}
        errors: list[str] = []

        def worker(name: str):
            ctx = HcomContext.from_env({"HCOM_PROCESS_ID": name}, "/tmp")
            with with_context(ctx):
                # Small sleep to increase chance of race condition
                time.sleep(0.01)
                actual = get_process_id()
                if actual != name:
                    errors.append(f"{name}: expected {name}, got {actual}")
                results[name] = actual

        threads = [threading.Thread(target=worker, args=(f"proc-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread should have seen its own value
        assert not errors, f"Race conditions detected: {errors}"
        assert len(results) == 10
        for i in range(10):
            assert results[f"proc-{i}"] == f"proc-{i}"

    def test_concurrent_different_values(self):
        """Concurrent threads with different is_launched values."""
        results: dict[int, bool] = {}
        errors: list[str] = []

        def worker(i: int, launched: bool):
            env = {"HCOM_LAUNCHED": "1"} if launched else {}
            ctx = HcomContext.from_env(env, "/tmp")
            with with_context(ctx):
                time.sleep(0.005)  # Interleave execution
                actual = get_is_launched()
                if actual != launched:
                    errors.append(f"Thread {i}: expected {launched}, got {actual}")
                results[i] = actual

        # Half threads with launched=True, half with launched=False
        threads = []
        for i in range(20):
            launched = (i % 2 == 0)
            threads.append(threading.Thread(target=worker, args=(i, launched)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Race conditions detected: {errors}"
        # Verify results
        for i in range(20):
            expected = (i % 2 == 0)
            assert results[i] == expected


class TestContextIsolation:
    """Test that contexts don't leak between uses."""

    def test_nested_contexts_restore_correctly(self):
        """Nested with_context calls restore correctly."""
        ctx1 = HcomContext.from_env({"HCOM_PROCESS_ID": "outer"}, "/tmp")
        ctx2 = HcomContext.from_env({"HCOM_PROCESS_ID": "inner"}, "/tmp")

        os.environ.pop("HCOM_PROCESS_ID", None)
        assert get_process_id() is None

        with with_context(ctx1):
            assert get_process_id() == "outer"

            with with_context(ctx2):
                assert get_process_id() == "inner"

            # Back to outer
            assert get_process_id() == "outer"

        # Back to no context
        assert get_process_id() is None

    def test_context_overrides_env(self):
        """Context takes precedence over os.environ."""
        os.environ["HCOM_PROCESS_ID"] = "env-value"
        try:
            ctx = HcomContext.from_env({"HCOM_PROCESS_ID": "ctx-value"}, "/tmp")

            # Without context, accessor sees env
            assert get_process_id() == "env-value"

            with with_context(ctx):
                # With context, accessor sees context (not env)
                assert get_process_id() == "ctx-value"

            # After context, back to env
            assert get_process_id() == "env-value"
        finally:
            os.environ.pop("HCOM_PROCESS_ID", None)
