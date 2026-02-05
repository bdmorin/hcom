"""State persistence tests.

Tests that data survives across DB connections, simulating process restarts
and ensuring state isn't corrupted by connection lifecycle.

Pattern: Write state → Force reconnection → Read state → Verify match
"""
import json
import time
import pytest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from hcom.core.db import (
    init_db,
    get_db,
    close_db,
    save_instance,
    get_instance,
    update_instance,
    set_session_binding,
    get_session_binding,
    set_process_binding,
    get_process_binding,
    log_event,
    get_last_event_id,
)


def force_reconnect():
    """Force a new DB connection by closing existing one."""
    close_db()


class TestInstancePersistence:
    """Instance data survives across DB connections."""

    def test_instance_persists_after_reconnect(self, hcom_env):
        """Instance created in one connection visible after reconnect."""
        init_db()

        instance_name = "persist_test"
        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
            "tool": "claude",
        })

        # Force new connection (simulates restart)
        force_reconnect()

        # Verify instance persists
        instance = get_instance(instance_name)
        assert instance is not None
        assert instance["name"] == instance_name
        assert instance["tool"] == "claude"

    def test_instance_update_persists(self, hcom_env):
        """Instance updates persist across connections."""
        init_db()

        instance_name = "update_persist"
        save_instance(instance_name, {
            "name": instance_name,
            "status": "inactive",
            "created_at": time.time(),
        })

        # Update status
        update_instance(instance_name, {
            "status": "active",
            "status_time": time.time(),
        })

        # Force reconnect
        force_reconnect()

        # Verify update persisted
        instance = get_instance(instance_name)
        assert instance["status"] == "active"
        assert instance["status_time"] is not None

    def test_multiple_instances_persist(self, hcom_env):
        """Multiple instances all persist correctly."""
        init_db()

        names = ["alpha", "beta", "gamma"]
        for name in names:
            save_instance(name, {
                "name": name,
                "status": "active",
                "created_at": time.time(),
                "tool": f"tool_{name}",
            })

        force_reconnect()

        # All instances should exist
        for name in names:
            instance = get_instance(name)
            assert instance is not None
            assert instance["tool"] == f"tool_{name}"


class TestSessionBindingPersistence:
    """Session bindings survive across connections."""

    def test_session_binding_persists(self, hcom_env):
        """Session binding visible after reconnect."""
        init_db()

        session_id = f"session-{uuid4().hex[:8]}"
        instance_name = "bound_instance"

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
        })
        set_session_binding(session_id, instance_name)

        force_reconnect()

        # Binding should persist
        bound_name = get_session_binding(session_id)
        assert bound_name == instance_name

    def test_multiple_bindings_independent(self, hcom_env):
        """Multiple session bindings persist independently."""
        init_db()

        bindings = {}
        for i in range(3):
            session_id = f"session-{uuid4().hex[:8]}"
            instance_name = f"instance_{i}"
            save_instance(instance_name, {
                "name": instance_name,
                "status": "active",
                "created_at": time.time(),
            })
            set_session_binding(session_id, instance_name)
            bindings[session_id] = instance_name

        force_reconnect()

        # All bindings should persist
        for session_id, expected_name in bindings.items():
            bound_name = get_session_binding(session_id)
            assert bound_name == expected_name


class TestProcessBindingPersistence:
    """Process bindings survive across connections."""

    def test_process_binding_persists(self, hcom_env):
        """Process binding visible after reconnect."""
        init_db()

        process_id = str(uuid4())
        instance_name = "process_bound"

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
        })
        set_process_binding(process_id, None, instance_name)

        force_reconnect()

        # Binding should persist
        binding = get_process_binding(process_id)
        assert binding is not None
        assert binding["instance_name"] == instance_name


class TestRunningTasksPersistence:
    """Subagent state (running_tasks JSON) survives freeze cycle."""

    def test_running_tasks_json_persists(self, hcom_env):
        """running_tasks JSON survives reconnection."""
        init_db()

        parent_name = "parent_with_tasks"
        running_tasks = {
            "task-abc123": {
                "started_at": time.time(),
                "subagents": ["child_1", "child_2"],
            }
        }

        save_instance(parent_name, {
            "name": parent_name,
            "status": "active",
            "created_at": time.time(),
            "running_tasks": json.dumps(running_tasks),
        })

        force_reconnect()

        # Verify JSON persisted correctly
        instance = get_instance(parent_name)
        loaded_tasks = json.loads(instance["running_tasks"] or "{}")

        assert "task-abc123" in loaded_tasks
        assert loaded_tasks["task-abc123"]["subagents"] == ["child_1", "child_2"]

    def test_running_tasks_update_persists(self, hcom_env):
        """Updates to running_tasks JSON persist."""
        init_db()

        parent_name = "parent_update_tasks"
        save_instance(parent_name, {
            "name": parent_name,
            "status": "active",
            "created_at": time.time(),
            "running_tasks": json.dumps({}),
        })

        # Add a task
        new_tasks = {"task-xyz": {"started_at": time.time(), "subagents": ["sub1"]}}
        update_instance(parent_name, {"running_tasks": json.dumps(new_tasks)})

        force_reconnect()

        # Task should persist
        instance = get_instance(parent_name)
        loaded = json.loads(instance["running_tasks"] or "{}")
        assert "task-xyz" in loaded
        assert "sub1" in loaded["task-xyz"]["subagents"]


class TestEventPersistence:
    """Events persist correctly across connections."""

    def test_events_persist(self, hcom_env):
        """Events visible after reconnect."""
        init_db()

        # Insert some events using correct signature: log_event(type, instance, data)
        for i in range(5):
            log_event(
                "message",
                "sender",
                {
                    "to": "receiver",
                    "content": f"Message {i}",
                }
            )

        force_reconnect()

        # Events should persist (last_event_id tracks count)
        last_id = get_last_event_id()
        assert last_id >= 5

    def test_event_ordering_preserved(self, hcom_env):
        """Event ordering preserved after reconnect."""
        init_db()

        # Insert events
        events = []
        for i in range(3):
            event_id = log_event(
                "message",
                "sender",
                {"content": f"msg-{i}"}
            )
            events.append((event_id, i))

        force_reconnect()

        # Query events and verify ordering
        conn = get_db()
        rows = conn.execute(
            "SELECT id, data FROM events WHERE type='message' ORDER BY id"
        ).fetchall()

        # Data is stored as JSON, parse and check content
        import json
        for idx, row in enumerate(rows):
            data = json.loads(row["data"]) if row["data"] else {}
            assert data.get("content") == f"msg-{idx}"


class TestConcurrentPersistence:
    """Concurrent writes persist correctly (WAL mode)."""

    def test_concurrent_saves_all_persist(self, hcom_env):
        """Concurrent instance saves all persist."""
        init_db()

        def create_instance(i):
            # Each thread needs its own connection
            name = f"concurrent_{i}"
            save_instance(name, {
                "name": name,
                "status": "active",
                "created_at": time.time(),
            })
            return name

        # Create instances concurrently
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(create_instance, i) for i in range(10)]
            names = [f.result() for f in futures]

        force_reconnect()

        # All should persist
        for name in names:
            instance = get_instance(name)
            assert instance is not None, f"Instance {name} not found"

    def test_concurrent_events_all_persist(self, hcom_env):
        """Concurrent event inserts all persist with unique IDs."""
        init_db()

        def insert_msg(i):
            return log_event(
                "message",
                f"sender_{i}",
                {"content": f"concurrent-{i}"}
            )

        # Insert events concurrently
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(insert_msg, i) for i in range(20)]
            event_ids = [f.result() for f in futures]

        force_reconnect()

        # All events should have unique IDs
        assert len(set(event_ids)) == 20, "Duplicate event IDs detected"

        # All events should be in DB (data is JSON column containing content)
        conn = get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE data LIKE '%concurrent-%'"
        ).fetchone()[0]
        assert count == 20


class TestStaleConnectionDetection:
    """Stale connection detection (inode change) works."""

    def test_db_reset_clears_old_data(self, hcom_env):
        """After DB file replacement, old data is gone."""
        import os
        from hcom.core.paths import hcom_path

        init_db()

        # Create data in old DB
        save_instance("old_instance", {
            "name": "old_instance",
            "status": "active",
            "created_at": time.time(),
        })

        # Simulate hcom reset by deleting and recreating DB
        db_path = hcom_path("hcom.db")
        close_db()
        os.remove(db_path)

        # New connection should create fresh DB
        init_db()

        # Old instance should not exist
        old = get_instance("old_instance")
        assert old is None

        # Can create new instance in fresh DB
        save_instance("new_instance", {
            "name": "new_instance",
            "status": "active",
            "created_at": time.time(),
        })
        new = get_instance("new_instance")
        assert new is not None
