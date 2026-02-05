"""Round-trip tests for identity resolution.

Tests that bindings created by one component (launcher, hooks) can be
correctly resolved by another component (identity.py, commands).

Pattern: Format in one place → Parse in another → Verify match
"""
import time
import pytest
from uuid import uuid4

from hcom.core.db import (
    init_db,
    save_instance,
    set_session_binding,
    set_process_binding,
    get_session_binding,
)
from hcom.core.identity import resolve_identity, resolve_from_name
from hcom.shared import HcomError


class TestSessionBindingRoundTrip:
    """Session binding: created by launcher/hooks, resolved by identity."""

    def test_session_binding_roundtrip_basic(self, hcom_env, monkeypatch):
        """Session binding created by launcher, resolved by identity.py"""
        init_db()

        # Step 1: Launcher creates instance + session binding
        instance_name = "api"
        session_id = f"session-{uuid4().hex[:8]}"

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
            "session_id": session_id,
            "tool": "claude",
        })
        set_session_binding(session_id, instance_name)

        # Step 2: Identity resolver finds it via session_id
        resolved = resolve_identity(session_id=session_id)

        assert resolved.name == instance_name
        assert resolved.kind == "instance"
        assert resolved.session_id == session_id

    def test_session_binding_persists_across_lookups(self, hcom_env):
        """Session binding survives multiple resolution calls."""
        init_db()

        instance_name = "persistent_test"
        session_id = f"session-{uuid4().hex[:8]}"

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
            "session_id": session_id,
        })
        set_session_binding(session_id, instance_name)

        # Multiple resolutions should all succeed
        for _ in range(3):
            resolved = resolve_identity(session_id=session_id)
            assert resolved.name == instance_name

    def test_session_binding_mismatch_detection(self, hcom_env):
        """Detect when session is bound to different instance than expected."""
        init_db()

        # Create two instances
        save_instance("instance_a", {
            "name": "instance_a",
            "status": "active",
            "created_at": time.time(),
        })
        save_instance("instance_b", {
            "name": "instance_b",
            "status": "active",
            "created_at": time.time(),
        })

        session_id = f"session-{uuid4().hex[:8]}"

        # Bind to instance_a
        set_session_binding(session_id, "instance_a")
        assert get_session_binding(session_id) == "instance_a"

        # Attempting to rebind should fail (use rebind_session for explicit change)
        with pytest.raises(HcomError, match="already bound"):
            set_session_binding(session_id, "instance_b")


class TestProcessBindingRoundTrip:
    """Process binding: created by PTY launch, resolved by identity.py"""

    def test_process_binding_roundtrip_basic(self, hcom_env, monkeypatch):
        """Process binding created by PTY launch, resolved by identity.py"""
        init_db()

        instance_name = "gemini_01"
        process_id = str(uuid4())

        # Step 1: PTY launcher creates instance + process binding
        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
            "tool": "gemini",
        })
        set_process_binding(process_id, None, instance_name)

        # Step 2: Identity resolver finds it via HCOM_PROCESS_ID env
        monkeypatch.setenv("HCOM_PROCESS_ID", process_id)
        resolved = resolve_identity()

        assert resolved.name == instance_name
        assert resolved.kind == "instance"

    def test_process_binding_survives_instance_data_update(self, hcom_env, monkeypatch):
        """Process binding still works after instance data is updated."""
        init_db()

        instance_name = "updatable"
        process_id = str(uuid4())

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
        })
        set_process_binding(process_id, None, instance_name)

        # Update instance status (simulates hook updating state)
        from hcom.core.db import update_instance
        update_instance(instance_name, {"status": "listening", "status_time": time.time()})

        # Process binding should still resolve
        monkeypatch.setenv("HCOM_PROCESS_ID", process_id)
        resolved = resolve_identity()

        assert resolved.name == instance_name

    def test_stale_process_binding_error(self, hcom_env, monkeypatch):
        """Stale process binding (instance deleted) raises clear error."""
        init_db()

        instance_name = "ephemeral"
        process_id = str(uuid4())

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
        })
        set_process_binding(process_id, None, instance_name)

        # Delete instance (simulates hcom stop/reset)
        from hcom.core.db import delete_instance
        delete_instance(instance_name)

        # Identity resolution should fail with actionable error
        monkeypatch.setenv("HCOM_PROCESS_ID", process_id)
        with pytest.raises(HcomError, match="not found"):
            resolve_identity()


class TestNameResolutionRoundTrip:
    """--name resolution: instance lookup by name."""

    def test_name_resolution_exact_match(self, hcom_env):
        """--name with exact instance name resolves correctly."""
        init_db()

        instance_name = "luna"
        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
        })

        resolved = resolve_from_name(instance_name)

        assert resolved.name == instance_name
        assert resolved.kind == "instance"

    def test_name_resolution_agent_id_lookup(self, hcom_env):
        """--name with agent_id (subagent) resolves to instance."""
        init_db()

        instance_name = "parent_task_abc"
        agent_id = "a6d9caf"  # Short agent ID format from Claude

        save_instance(instance_name, {
            "name": instance_name,
            "status": "active",
            "created_at": time.time(),
            "agent_id": agent_id,
        })

        # Resolution by agent_id should find the instance
        resolved = resolve_from_name(agent_id)

        assert resolved.name == instance_name
        assert resolved.kind == "instance"

    def test_name_resolution_not_found_error(self, hcom_env):
        """--name with unknown instance raises actionable error."""
        init_db()

        with pytest.raises(HcomError, match="not found"):
            resolve_from_name("nonexistent")

    def test_name_resolution_invalid_format_error(self, hcom_env):
        """--name with invalid format raises validation error."""
        init_db()

        # Names with hyphens, uppercase, etc. should fail validation
        invalid_names = [
            "has-hyphen",
            "HAS_UPPERCASE",
            "has.dot",
            "has space",
            "@mention",
        ]

        for invalid in invalid_names:
            with pytest.raises(HcomError, match="Invalid instance name"):
                resolve_from_name(invalid)


class TestIdentityPriorityChain:
    """Test the identity resolution priority chain."""

    def test_system_sender_highest_priority(self, hcom_env, monkeypatch):
        """system_sender takes precedence over all other identity sources."""
        init_db()

        # Set up multiple identity sources
        process_id = str(uuid4())
        save_instance("should_not_use", {
            "name": "should_not_use",
            "status": "active",
            "created_at": time.time(),
        })
        set_process_binding(process_id, None, "should_not_use")
        monkeypatch.setenv("HCOM_PROCESS_ID", process_id)

        # system_sender should win
        resolved = resolve_identity(system_sender="hcom-launcher")

        assert resolved.name == "hcom-launcher"
        assert resolved.kind == "system"

    def test_explicit_name_over_process_binding(self, hcom_env, monkeypatch):
        """--name takes precedence over HCOM_PROCESS_ID auto-detection."""
        init_db()

        # Set up process binding
        process_id = str(uuid4())
        save_instance("auto_detected", {
            "name": "auto_detected",
            "status": "active",
            "created_at": time.time(),
        })
        set_process_binding(process_id, None, "auto_detected")
        monkeypatch.setenv("HCOM_PROCESS_ID", process_id)

        # Also create explicit name target
        save_instance("explicit_name", {
            "name": "explicit_name",
            "status": "active",
            "created_at": time.time(),
        })

        # --name should win over auto-detection
        resolved = resolve_identity(name="explicit_name")

        assert resolved.name == "explicit_name"

    def test_no_identity_raises_clear_error(self, hcom_env, monkeypatch):
        """No identity sources available raises actionable error."""
        init_db()

        # Ensure no identity env vars set
        monkeypatch.delenv("HCOM_PROCESS_ID", raising=False)
        monkeypatch.delenv("HCOM_NAME", raising=False)

        with pytest.raises(HcomError, match="No hcom identity"):
            resolve_identity()
