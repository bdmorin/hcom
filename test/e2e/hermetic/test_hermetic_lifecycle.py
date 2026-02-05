from __future__ import annotations

from uuid import uuid4

import pytest

from .harness import make_workspace, run_hcom, db_conn, seed_instance, seed_session_binding


@pytest.fixture
def ws():
    ws = make_workspace(timeout_s=1, hints="Hermetic hints")
    try:
        yield ws
    finally:
        ws.cleanup()


def test_start_as_rebind_moves_session_binding_and_deletes_old_identity(ws):
    session_id = f"hermetic-sid-{uuid4()}"
    current = "oldname"
    target = "newname"

    # Seed a current identity that already has a session_id (simulates an opted-in session).
    seed_instance(ws, name=current, session_id=session_id)
    seed_session_binding(ws, session_id=session_id, instance_name=current)

    res = run_hcom(ws.env(), "start", "--name", current, "--as", target)
    assert res.code == 0, res.stderr
    assert f"[hcom:{target}]" in res.stdout

    # Verify DB: old identity deleted, new identity exists, binding moved.
    conn = db_conn(ws)
    try:
        old_row = conn.execute("SELECT name FROM instances WHERE name = ?", (current,)).fetchone()
        new_row = conn.execute("SELECT session_id FROM instances WHERE name = ?", (target,)).fetchone()
        bind_row = conn.execute(
            "SELECT instance_name FROM session_bindings WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert old_row is None
    assert new_row is not None
    assert new_row["session_id"] == session_id
    assert bind_row is not None
    assert bind_row["instance_name"] == target


