"""Tests for core/identity.py - identity resolution module."""
import pytest


@pytest.fixture
def isolated_hcom_dir(tmp_path, monkeypatch):
    """Isolate tests to temp directory - never touch real ~/.hcom."""
    hcom_dir = tmp_path / ".hcom"
    hcom_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HCOM_DIR", str(hcom_dir))

    # Clear identity-related env vars
    for var in ["HCOM_LAUNCHED", "HCOM_PROCESS_ID", "CLAUDECODE", "GEMINI_CLI"]:
        monkeypatch.delenv(var, raising=False)

    from hcom.core.db import close_db, init_db
    close_db()
    init_db()

    from hcom.core.paths import ensure_hcom_directories
    ensure_hcom_directories()

    yield hcom_dir


@pytest.fixture
def db_with_instance(isolated_hcom_dir):
    """Create a test instance in DB."""
    from hcom.core.db import save_instance
    import time

    instance_data = {
        "name": "alice",
        "session_id": "test-session-123",
        "directory": str(isolated_hcom_dir),
        "status": "listening",
        "created_at": time.time(),
        "last_event_id": 0,
        "last_stop": 0,
    }
    save_instance("alice", instance_data)
    return instance_data


@pytest.fixture
def db_with_disabled_instance(isolated_hcom_dir):
    """Create a disabled test instance in DB."""
    from hcom.core.db import save_instance
    import time

    instance_data = {
        "name": "bob",
        "session_id": "test-session-bob",
        "directory": str(isolated_hcom_dir),
        "status": "inactive",
        "created_at": time.time(),
        "last_event_id": 0,
        "last_stop": 0,
    }
    save_instance("bob", instance_data)
    return instance_data


def test_resolve_identity_from_process_binding(isolated_hcom_dir, db_with_instance, monkeypatch):
    from hcom.core.identity import resolve_identity
    from hcom.core.db import set_process_binding

    monkeypatch.setenv("HCOM_PROCESS_ID", "proc-1")
    set_process_binding("proc-1", "test-session-123", "alice")
    identity = resolve_identity()

    assert identity.kind == "instance"
    assert identity.name == "alice"


def test_resolve_identity_missing_binding_raises(isolated_hcom_dir, monkeypatch):
    from hcom.core.identity import resolve_identity, HcomError

    monkeypatch.setenv("HCOM_PROCESS_ID", "proc-missing")
    with pytest.raises(HcomError):
        resolve_identity()


def test_resolve_from_name_inactive_instance_succeeds(isolated_hcom_dir, db_with_disabled_instance):
    """Inactive status doesn't prevent resolution - row exists = participating"""
    from hcom.core.identity import resolve_from_name

    # In new schema: row exists = participating, regardless of status
    identity = resolve_from_name("bob")
    assert identity.name == "bob"
    assert identity.kind == "instance"


def test_resolve_identity_requires_name(isolated_hcom_dir):
    from hcom.core.identity import resolve_identity, HcomError

    with pytest.raises(HcomError):
        resolve_identity()
