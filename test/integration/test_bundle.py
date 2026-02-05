"""Unit tests for bundle command."""

from __future__ import annotations

import json
import sys
import pytest
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

from hcom.shared import CommandContext
from hcom.core.identity import SenderIdentity


@pytest.fixture
def isolated_hcom_env(monkeypatch, tmp_path):
    """Isolated HCOM environment."""
    hcom_dir = tmp_path / ".hcom"
    hcom_dir.mkdir()

    monkeypatch.setenv("HCOM_DIR", str(hcom_dir))
    monkeypatch.setenv("HOME", str(tmp_path))

    from hcom.core.config import reload_config

    reload_config()

    from hcom.core.db import init_db

    init_db()

    yield hcom_dir


@pytest.fixture
def mock_ctx():
    """Mock command context with identity."""
    identity = SenderIdentity(
        kind="instance",
        name="tester",
        instance_data={"tool": "test", "session_id": "sess-1"},
    )
    return CommandContext(explicit_name=None, identity=identity)


def capture_stdout(func, *args, **kwargs):
    """Capture stdout from a function call."""
    f = StringIO()
    with redirect_stdout(f):
        exit_code = func(*args, **kwargs)
    return exit_code, f.getvalue()


def test_bundle_create_and_show(isolated_hcom_env, mock_ctx):
    from hcom.commands.bundle import cmd_bundle

    # Create bundle
    argv = [
        "create",
        "My Bundle",
        "--description",
        "A test bundle",
        "--events",
        "1,2",
        "--files",
        "a.py",
        "--transcript",
        "10-20:normal",
        "--json",
    ]
    code, out = capture_stdout(cmd_bundle, argv, ctx=mock_ctx)
    assert code == 0
    result = json.loads(out)
    bundle_id = result["bundle_id"]
    assert bundle_id.startswith("bundle:")

    # Show bundle
    argv = ["show", bundle_id, "--json"]
    code, out = capture_stdout(cmd_bundle, argv, ctx=mock_ctx)
    assert code == 0
    data = json.loads(out)
    assert data["bundle_id"] == bundle_id
    assert data["title"] == "My Bundle"
    assert data["description"] == "A test bundle"
    assert data["refs"]["events"] == ["1", "2"]
    assert data["refs"]["files"] == ["a.py"]
    # Transcript refs are normalized to objects
    assert data["refs"]["transcript"][0]["range"] == "10-20"
    assert data["refs"]["transcript"][0]["detail"] == "normal"
    assert data["created_by"] == "tester"


def test_bundle_create_from_json(isolated_hcom_env, mock_ctx):
    from hcom.commands.bundle import cmd_bundle

    bundle_data = {
        "title": "JSON Bundle",
        "description": "Created from JSON",
        "refs": {"events": ["3"], "files": ["b.py"], "transcript": ["5-10:full"]},
    }

    argv = ["create", "--bundle", json.dumps(bundle_data), "--json"]
    code, out = capture_stdout(cmd_bundle, argv, ctx=mock_ctx)
    assert code == 0
    bundle_id = json.loads(out)["bundle_id"]

    # Verify
    code, out = capture_stdout(cmd_bundle, ["show", bundle_id, "--json"], ctx=mock_ctx)
    data = json.loads(out)
    assert data["title"] == "JSON Bundle"


def test_bundle_chain(isolated_hcom_env, mock_ctx):
    from hcom.commands.bundle import cmd_bundle

    # Create parent
    argv1 = [
        "create",
        "Parent",
        "--description",
        "Parent bundle",
        "--files",
        "p.py",
        "--events",
        "1",
        "--transcript",
        "1-5:normal",
        "--json",
    ]

    code, out = capture_stdout(cmd_bundle, argv1, ctx=mock_ctx)
    assert code == 0
    parent_id = json.loads(out)["bundle_id"]

    # Create child
    argv2 = [
        "create",
        "Child",
        "--description",
        "Child bundle",
        "--extends",
        parent_id,
        "--files",
        "c.py",
        "--events",
        "2",
        "--transcript",
        "6-10:detailed",
        "--json",
    ]
    code, out = capture_stdout(cmd_bundle, argv2, ctx=mock_ctx)
    assert code == 0
    child_id = json.loads(out)["bundle_id"]

    # Test chain
    argv3 = ["chain", child_id, "--json"]
    code, out = capture_stdout(cmd_bundle, argv3, ctx=mock_ctx)
    assert code == 0
    chain = json.loads(out)

    assert len(chain) == 2
    assert chain[0]["bundle_id"] == child_id
    assert chain[1]["bundle_id"] == parent_id


def test_bundle_list(isolated_hcom_env, mock_ctx):
    from hcom.commands.bundle import cmd_bundle

    # Create a few bundles
    capture_stdout(
        cmd_bundle,
        [
            "create",
            "B1",
            "--description",
            "D1",
            "--files",
            "f1.py",
            "--events",
            "1",
            "--transcript",
            "1-2:normal",
            "--json",
        ],
        ctx=mock_ctx,
    )
    capture_stdout(
        cmd_bundle,
        [
            "create",
            "B2",
            "--description",
            "D2",
            "--files",
            "f2.py",
            "--events",
            "2",
            "--transcript",
            "3-4:full",
            "--json",
        ],
        ctx=mock_ctx,
    )

    # List
    code, out = capture_stdout(cmd_bundle, ["list", "--json"], ctx=mock_ctx)
    assert code == 0
    bundles = json.loads(out)
    assert len(bundles) == 2
    assert bundles[0]["title"] == "B2"  # Ordered by ID DESC
    assert bundles[1]["title"] == "B1"


def test_bundle_prepare(isolated_hcom_env, mock_ctx):
    """Test bundle prepare command with mock data - shows actual content."""
    from hcom.commands.bundle import cmd_bundle
    from hcom.core.db import get_db, log_event
    from hcom.core.messages import send_message
    from hcom.shared import SenderIdentity
    import tempfile
    import os
    import time

    conn = get_db()
    instance_name = "tester"

    # Create some mock transcript data (Claude format)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        # Add some mock transcript entries
        for i in range(3):
            f.write(
                json.dumps(
                    {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": [{"type": "text", "text": f"Entry {i + 1}"}],
                    }
                )
                + "\n"
            )
        transcript_path = f.name

    # Create test instance directly in DB
    conn.execute(
        """INSERT INTO instances 
           (name, session_id, tool, created_at, last_event_id, directory, transcript_path, status, status_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (instance_name, "sess-test", "claude", time.time(), 0, "/tmp", transcript_path, "active", int(time.time())),
    )
    conn.commit()

    # Create another instance for messaging
    conn.execute(
        """INSERT INTO instances 
           (name, session_id, tool, created_at, last_event_id, directory, status, status_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("other", "sess-other", "claude", time.time(), 0, "/tmp", "active", int(time.time())),
    )
    conn.commit()

    # Create some test events
    identity = SenderIdentity(kind="instance", name=instance_name, instance_data={"tool": "claude"})

    # Log some status events with file activity
    log_event("status", instance_name, {"status": "active", "context": "tool:Write", "detail": "/path/to/test.py"})
    log_event("status", instance_name, {"status": "active", "context": "tool:Edit", "detail": "/path/to/other.py"})

    # Create some messages
    other_identity = SenderIdentity(kind="instance", name="other", instance_data={"tool": "claude"})
    send_message(other_identity, f"@{instance_name} test message")
    send_message(identity, "response message")

    # Log lifecycle events
    log_event("life", instance_name, {"action": "created", "by": "launcher", "batch_id": "batch-123"})

    # Test prepare command with JSON output
    argv = ["prepare", "--for", instance_name, "--json"]
    code, out = capture_stdout(cmd_bundle, argv, ctx=None)

    # Clean up temp file
    try:
        os.unlink(transcript_path)
    except:
        pass

    assert code == 0
    result = json.loads(out)

    # Verify structure (NEW FORMAT: shows actual content)
    assert result["agent"] == instance_name
    assert "transcript" in result
    assert "events" in result
    assert "files" in result
    assert "template_command" in result
    assert "note" in result

    # Verify transcript contains actual text
    assert "text" in result["transcript"]
    assert "range" in result["transcript"]
    assert "total_entries" in result["transcript"]
    # Should have parsed some entries
    if result["transcript"]["text"]:
        assert "Entry" in result["transcript"]["text"] or "conversation" in result["transcript"]["text"]

    # Verify events contain actual event objects (not just IDs)
    events = result["events"]
    # Check for at least one category with actual event data
    has_events = False
    for category, event_list in events.items():
        if event_list and isinstance(event_list, list):
            has_events = True
            # Verify event structure
            event = event_list[0]
            assert "id" in event
            assert "timestamp" in event
            assert "type" in event
            assert "data" in event

    assert has_events, "Should have at least one event category with data"

    # Verify files
    assert isinstance(result["files"], list)
    # Should include the files we logged
    assert "/path/to/test.py" in result["files"] or "/path/to/other.py" in result["files"]

    # Verify template command includes key parts
    template = result["template_command"]
    assert "hcom bundle create" in template
    assert instance_name in template
    assert "--description" in template
