from __future__ import annotations

import json
from uuid import uuid4

import pytest

from .harness import (
    make_workspace,
    run_hcom,
    db_conn,
    seed_instance,
    seed_process_binding,
    get_process_binding,
)


@pytest.fixture
def ws():
    ws = make_workspace(timeout_s=1, hints="Hermetic hints")
    try:
        yield ws
    finally:
        ws.cleanup()


def test_gemini_notification_sets_blocked_on_tool_permission(ws):
    name = "gem1"
    process_id = f"gemini-proc-{uuid4().hex[:8]}"

    seed_instance(ws, name=name, tool="gemini")
    seed_process_binding(ws, process_id=process_id, instance_name=name)

    env = ws.env()
    env["HCOM_PROCESS_ID"] = process_id
    env["HCOM_LAUNCHED"] = "1"

    res = run_hcom(env, "gemini-notification", stdin={"notification_type": "ToolPermission"})
    assert res.code == 0, res.stderr

    conn = db_conn(ws)
    try:
        row = conn.execute(
            "SELECT status, status_context FROM instances WHERE name = ?",
            (name,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["status"] == "blocked"
    assert row["status_context"] == "approval"


def test_codex_notify_binds_thread_id_and_sets_listening(ws):
    name = "codex1"
    process_id = f"codex-proc-{uuid4().hex[:8]}"
    thread_id = f"thread-{uuid4()}"

    seed_instance(ws, name=name, tool="codex")
    seed_process_binding(ws, process_id=process_id, instance_name=name)

    env = ws.env()
    env["HCOM_PROCESS_ID"] = process_id
    env["HCOM_LAUNCHED"] = "1"

    payload = {
        "type": "agent-turn-complete",
        "thread-id": thread_id,
        "turn-id": "1",
        "cwd": str(ws.root),
        "transcript_path": str(ws.transcript),
        "input-messages": ["hi"],
        "last-assistant-message": "ok",
    }

    res = run_hcom(env, "codex-notify", json.dumps(payload))
    assert res.code == 0, res.stderr

    # Instance updated
    conn = db_conn(ws)
    try:
        row = conn.execute(
            "SELECT session_id, status, idle_since, transcript_path FROM instances WHERE name = ?",
            (name,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["session_id"] == thread_id
    assert row["status"] == "listening"
    assert row["idle_since"], "codex-notify should set idle_since"
    assert row["transcript_path"] == str(ws.transcript)

    # Process binding updated to include session_id
    binding = get_process_binding(ws, process_id=process_id)
    assert binding is not None
    assert binding.get("instance_name") == name
    assert binding.get("session_id") == thread_id


