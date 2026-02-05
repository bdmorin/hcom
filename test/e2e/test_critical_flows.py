"""Critical path E2E tests for hcom.

These tests verify the most important functionality that if broken,
renders the system unusable. They use the hermetic harness for isolation.

Critical paths tested:
1. Identity Resolution - HCOM_PROCESS_ID, --name flag, error on missing
2. Message Delivery - broadcast, mentions, invalid mention rejection
3. Hook Lifecycle - sessionstart binding, poll delivery, post handling
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermetic.harness import (
    make_workspace,
    run_hcom,
    parse_single_json,
    seed_instance,
    seed_session_binding,
    seed_process_binding,
    get_session_binding,
    get_process_binding,
    db_conn,
)


@pytest.fixture
def ws():
    """Create hermetic workspace for testing."""
    workspace = make_workspace(timeout_s=1, hints="Critical flow test")
    try:
        yield workspace
    finally:
        workspace.cleanup()


class TestIdentityResolution:
    """Test identity resolution - foundation for all other functionality."""

    def test_process_binding_enables_send(self, ws):
        """HCOM_PROCESS_ID → process binding → can send messages.

        This is how hcom-launched instances identify themselves.
        """
        process_id = f"proc-{uuid4()}"
        session_id = f"sess-{uuid4()}"
        name = "launched-instance"

        # Pre-register via process binding (launcher does this)
        seed_instance(ws, name=name)
        seed_process_binding(ws, process_id=process_id, instance_name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Send with HCOM_PROCESS_ID set
        env = ws.env()
        env["HCOM_PROCESS_ID"] = process_id

        result = run_hcom(env, "send", "-b", "hello from launched")
        assert result.code == 0, f"Send failed: {result.stderr}"

    def test_name_flag_enables_participation(self, ws):
        """--name flag → instance lookup → participation.

        This is how vanilla/adhoc instances identify themselves.
        """
        name = "named-instance"
        session_id = f"sess-{uuid4()}"

        seed_instance(ws, name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Another instance can send to it via @mention
        result = run_hcom(ws.env(), "send", "-b", f"@{name} hello named")
        assert result.code == 0, f"Send to named instance failed: {result.stderr}"

    def test_send_to_missing_instance_fails(self, ws):
        """@nonexistent → send fails with clear error.

        Critical: invalid mentions must fail, not silently drop messages.
        """
        result = run_hcom(ws.env(), "send", "-b", "@nonexistent hello")
        assert result.code != 0, "Send to nonexistent should fail"
        assert "non-existent" in result.stderr.lower() or "not found" in result.stderr.lower()

    def test_sessionstart_creates_binding(self, ws):
        """SessionStart hook → creates session binding → enables participation.

        This is how Claude Code sessions register themselves.
        """
        session_id = f"sess-{uuid4()}"
        process_id = f"proc-{uuid4()}"
        name = "sessionstart-instance"

        # Simulate launcher pre-registration
        seed_instance(ws, name=name)
        seed_process_binding(ws, process_id=process_id, instance_name=name)

        # Call sessionstart hook
        env = ws.env()
        env["HCOM_PROCESS_ID"] = process_id

        payload = {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "transcript_path": str(ws.transcript),
        }

        result = run_hcom(env, "sessionstart", stdin=payload)
        assert result.code == 0, f"SessionStart failed: {result.stderr}"

        # Verify binding was created
        bound_name = get_session_binding(ws, session_id=session_id)
        assert bound_name == name, f"Expected binding to {name}, got {bound_name}"


class TestMessageDelivery:
    """Test message delivery - core functionality."""

    def test_broadcast_reaches_all_instances(self, ws):
        """Broadcast (no @mentions) → delivered to all participating instances."""
        s1, s2 = f"sess-{uuid4()}", f"sess-{uuid4()}"
        a, b = "alpha", "bravo"

        seed_instance(ws, name=a)
        seed_instance(ws, name=b)
        seed_session_binding(ws, session_id=s1, instance_name=a)
        seed_session_binding(ws, session_id=s2, instance_name=b)

        # Send broadcast
        result = run_hcom(ws.env(), "send", "-b", "broadcast-test")
        assert result.code == 0, f"Broadcast failed: {result.stderr}"

        # Both should receive
        for sid, name in [(s1, a), (s2, b)]:
            payload = {
                "hook_event_name": "Stop",
                "session_id": sid,
                "transcript_path": str(ws.transcript),
            }
            poll = run_hcom(ws.env(), "poll", stdin=payload)
            assert poll.code == 2, f"{name} didn't receive: {poll.stderr}"
            assert "broadcast-test" in parse_single_json(poll.stdout)["reason"]

    def test_mention_filters_delivery(self, ws):
        """@mention → only mentioned instance receives.

        Critical: messages must not leak to unintended recipients.
        """
        s1, s2 = f"sess-{uuid4()}", f"sess-{uuid4()}"
        alice, bob = "alice", "bob"

        seed_instance(ws, name=alice)
        seed_instance(ws, name=bob)
        seed_session_binding(ws, session_id=s1, instance_name=alice)
        seed_session_binding(ws, session_id=s2, instance_name=bob)

        # Send to alice only
        result = run_hcom(ws.env(), "send", "-b", f"@{alice} secret message")
        assert result.code == 0, f"Send to alice failed: {result.stderr}"

        # Alice should receive
        poll_alice = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": s1,
            "transcript_path": str(ws.transcript),
        })
        assert poll_alice.code == 2, "Alice should receive"
        assert "secret message" in parse_single_json(poll_alice.stdout)["reason"]

        # Bob should NOT receive (poll times out with code 0)
        poll_bob = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": s2,
            "transcript_path": str(ws.transcript),
        })
        assert poll_bob.code == 0, f"Bob should not receive (code={poll_bob.code})"

    def test_prefix_matching_respects_underscore_boundary(self, ws):
        """@ali matches alice, alice-worker, but NOT alice_subagent.

        Underscore indicates subagent and should block prefix matching.
        """
        s1, s2 = f"sess-{uuid4()}", f"sess-{uuid4()}"

        seed_instance(ws, name="alice")
        seed_instance(ws, name="alice_subagent")  # Should NOT match @alice
        seed_session_binding(ws, session_id=s1, instance_name="alice")
        seed_session_binding(ws, session_id=s2, instance_name="alice_subagent")

        # Send to @alice
        result = run_hcom(ws.env(), "send", "-b", "@alice parent message")
        assert result.code == 0

        # alice should receive
        poll_alice = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": s1,
            "transcript_path": str(ws.transcript),
        })
        assert poll_alice.code == 2, "alice should receive"

        # alice_subagent should NOT receive
        poll_sub = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": s2,
            "transcript_path": str(ws.transcript),
        })
        assert poll_sub.code == 0, "alice_subagent should not receive @alice message"


class TestHookLifecycle:
    """Test hook lifecycle - keeps instances connected."""

    def test_poll_returns_pending_messages(self, ws):
        """Poll hook → returns pending messages with exit code 2."""
        session_id = f"sess-{uuid4()}"
        name = "poll-test"

        seed_instance(ws, name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Send a message
        run_hcom(ws.env(), "send", "-b", f"@{name} poll-test-message")

        # Poll should return it
        poll = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": session_id,
            "transcript_path": str(ws.transcript),
        })
        assert poll.code == 2, f"Expected exit code 2, got {poll.code}"

        out = parse_single_json(poll.stdout)
        assert out["decision"] == "block"
        assert "poll-test-message" in out["reason"]

    def test_poll_returns_0_when_no_messages(self, ws):
        """Poll hook → returns exit code 0 when no pending messages."""
        session_id = f"sess-{uuid4()}"
        name = "empty-poll"

        seed_instance(ws, name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Poll without any messages
        poll = run_hcom(ws.env(), "poll", stdin={
            "hook_event_name": "Stop",
            "session_id": session_id,
            "transcript_path": str(ws.transcript),
        })
        assert poll.code == 0, f"Expected exit code 0 (no messages), got {poll.code}"

    def test_post_delivers_after_tool_use(self, ws):
        """PostToolUse hook → delivers pending messages after tool execution.

        Note: Post hook returns code 0 with message in hookSpecificOutput,
        unlike poll which returns code 2.
        """
        session_id = f"sess-{uuid4()}"
        name = "post-test"

        seed_instance(ws, name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Send a message
        run_hcom(ws.env(), "send", "-b", f"@{name} post-test-message")

        # Post hook delivers via hookSpecificOutput
        post = run_hcom(ws.env(), "post", stdin={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "transcript_path": str(ws.transcript),
            "tool_name": "Bash",
            "tool_result": "command output",
        })
        assert post.code == 0, f"Post hook failed: {post.stderr}"

        out = parse_single_json(post.stdout)
        # Message delivered in hookSpecificOutput.additionalContext or systemMessage
        assert "post-test-message" in out.get("systemMessage", "") or \
               "post-test-message" in out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_pre_updates_status_to_active(self, ws):
        """PreToolUse hook → updates instance status to active."""
        session_id = f"sess-{uuid4()}"
        name = "pre-test"

        seed_instance(ws, name=name)
        seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Call pre hook
        pre = run_hcom(ws.env(), "pre", stdin={
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "transcript_path": str(ws.transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        })
        assert pre.code == 0, f"Pre hook failed: {pre.stderr}"

        # Verify status updated
        conn = db_conn(ws)
        try:
            row = conn.execute(
                "SELECT status FROM instances WHERE name = ?", (name,)
            ).fetchone()
            assert row is not None, "Instance not found"
            assert row["status"] == "active", f"Expected active, got {row['status']}"
        finally:
            conn.close()
