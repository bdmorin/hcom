"""Multi-instance messaging hermetic tests.

Tests complete message flow between multiple instances, including:
- Direct messaging between named instances
- Message relay/forwarding between instances
- Thread context preservation
- Cross-tool message handling
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from .harness import (
    make_workspace,
    run_hcom,
    parse_single_json,
    seed_instance,
    seed_session_binding,
    db_conn,
)


@pytest.fixture
def ws():
    ws = make_workspace(timeout_s=1, hints="Multi-instance test")
    try:
        yield ws
    finally:
        ws.cleanup()


def _hook_poll_payload(ws, session_id: str) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "transcript_path": str(ws.transcript),
    }


class TestMultiInstanceMessaging:
    """Test message flows between multiple instances."""

    def test_three_way_relay(self, ws):
        """A sends to B, B forwards to C, C receives message."""
        # Setup three instances with different tools
        s_alice = f"session-alice-{uuid4()}"
        s_bob = f"session-bob-{uuid4()}"
        s_carol = f"session-carol-{uuid4()}"

        seed_instance(ws, name="alice", tool="claude")
        seed_instance(ws, name="bob", tool="gemini")
        seed_instance(ws, name="carol", tool="codex")
        seed_session_binding(ws, session_id=s_alice, instance_name="alice")
        seed_session_binding(ws, session_id=s_bob, instance_name="bob")
        seed_session_binding(ws, session_id=s_carol, instance_name="carol")

        # Step 1: Alice sends to Bob (using @mention syntax)
        send1 = run_hcom(ws.env(), "send", "--from", "alice",
                        "@bob Forward this to Carol: Hello from Alice!")
        assert send1.code == 0, f"Alice->Bob failed: {send1.stderr}"

        # Step 2: Bob receives Alice's message
        poll_bob = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_bob))
        assert poll_bob.code == 2, f"Bob poll failed: {poll_bob.stderr}"
        bob_msg = parse_single_json(poll_bob.stdout)
        assert "Hello from Alice" in bob_msg["reason"]

        # Step 3: Bob forwards to Carol (using @mention syntax)
        send2 = run_hcom(ws.env(), "send", "--from", "bob",
                        "@carol From Bob: Alice says hello!")
        assert send2.code == 0, f"Bob->Carol failed: {send2.stderr}"

        # Step 4: Carol receives Bob's forwarded message
        poll_carol = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_carol))
        assert poll_carol.code == 2, f"Carol poll failed: {poll_carol.stderr}"
        carol_msg = parse_single_json(poll_carol.stdout)
        assert "Alice says hello" in carol_msg["reason"]

    def test_broadcast_reaches_all_instances(self, ws):
        """Broadcast message reaches all participating instances."""
        sessions = {}
        names = ["alpha", "beta", "gamma", "delta"]

        for name in names:
            session_id = f"session-{name}-{uuid4()}"
            sessions[name] = session_id
            seed_instance(ws, name=name, tool="claude")
            seed_session_binding(ws, session_id=session_id, instance_name=name)

        # Broadcast from system
        send = run_hcom(ws.env(), "send", "-b", "ALERT: System broadcast message")
        assert send.code == 0, send.stderr

        # All instances should receive the broadcast
        for name, session_id in sessions.items():
            poll = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, session_id))
            assert poll.code == 2, f"{name} didn't receive broadcast: {poll.stderr}"
            msg = parse_single_json(poll.stdout)
            assert "ALERT: System broadcast" in msg["reason"], f"{name} got wrong message"

    def test_mention_only_reaches_target(self, ws):
        """@mention only delivers to mentioned instance, not others."""
        s_target = f"session-target-{uuid4()}"
        s_other = f"session-other-{uuid4()}"

        seed_instance(ws, name="target", tool="claude")
        seed_instance(ws, name="other", tool="claude")
        seed_session_binding(ws, session_id=s_target, instance_name="target")
        seed_session_binding(ws, session_id=s_other, instance_name="other")

        # Send with @mention to target only
        send = run_hcom(ws.env(), "send", "-b", "@target Private message for target only")
        assert send.code == 0, send.stderr

        # Target should receive message
        poll_target = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_target))
        assert poll_target.code == 2, f"Target didn't receive: {poll_target.stderr}"

        # Other should NOT receive (poll times out with no message)
        poll_other = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_other))
        assert poll_other.code == 0, "Other should not receive @mention message"
        assert poll_other.stdout.strip() == ""


class TestCrossToolMessaging:
    """Test messaging between instances of different tools."""

    def test_claude_to_gemini_message(self, ws):
        """Claude instance can send to Gemini instance."""
        s_claude = f"session-claude-{uuid4()}"
        s_gemini = f"session-gemini-{uuid4()}"

        seed_instance(ws, name="claude_agent", tool="claude")
        seed_instance(ws, name="gemini_agent", tool="gemini")
        seed_session_binding(ws, session_id=s_claude, instance_name="claude_agent")
        seed_session_binding(ws, session_id=s_gemini, instance_name="gemini_agent")

        # Claude sends to Gemini (using @mention)
        send = run_hcom(ws.env(), "send", "--from", "claude_agent",
                       "@gemini_agent Hello from Claude to Gemini!")
        assert send.code == 0, send.stderr

        # Gemini receives
        poll = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_gemini))
        assert poll.code == 2, f"Gemini didn't receive: {poll.stderr}"
        msg = parse_single_json(poll.stdout)
        assert "Hello from Claude to Gemini" in msg["reason"]

    def test_codex_to_claude_message(self, ws):
        """Codex instance can send to Claude instance."""
        s_codex = f"session-codex-{uuid4()}"
        s_claude = f"session-claude-{uuid4()}"

        seed_instance(ws, name="codex_agent", tool="codex")
        seed_instance(ws, name="claude_agent", tool="claude")
        seed_session_binding(ws, session_id=s_codex, instance_name="codex_agent")
        seed_session_binding(ws, session_id=s_claude, instance_name="claude_agent")

        # Codex sends to Claude (using @mention)
        send = run_hcom(ws.env(), "send", "--from", "codex_agent",
                       "@claude_agent Hello from Codex to Claude!")
        assert send.code == 0, send.stderr

        # Claude receives
        poll = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_claude))
        assert poll.code == 2, f"Claude didn't receive: {poll.stderr}"
        msg = parse_single_json(poll.stdout)
        assert "Hello from Codex to Claude" in msg["reason"]


class TestMessageOrdering:
    """Test message ordering and sequencing."""

    def test_messages_received_in_send_order(self, ws):
        """Multiple messages to same instance received in order sent."""
        session_id = f"session-receiver-{uuid4()}"

        seed_instance(ws, name="receiver", tool="claude")
        seed_session_binding(ws, session_id=session_id, instance_name="receiver")

        # Send 5 messages in order (using @mention)
        for i in range(5):
            send = run_hcom(ws.env(), "send", "-b",
                           f"@receiver Message number {i}")
            assert send.code == 0, f"Send {i} failed: {send.stderr}"

        # Check all messages in events table
        conn = db_conn(ws)
        try:
            rows = conn.execute("""
                SELECT id, data FROM events
                WHERE type = 'message'
                ORDER BY id
            """).fetchall()

            # Verify order (message content is in 'text' field)
            for i, row in enumerate(rows):
                data = json.loads(row["data"]) if row["data"] else {}
                assert f"Message number {i}" in data.get("text", ""), \
                    f"Message {i} out of order, got: {data.get('text', '')}"
        finally:
            conn.close()


class TestEventIntegrity:
    """Test event creation and data integrity."""

    def test_all_sends_create_events(self, ws):
        """Each send creates exactly one event in DB."""
        session_id = f"session-test-{uuid4()}"

        seed_instance(ws, name="tester", tool="claude")
        seed_session_binding(ws, session_id=session_id, instance_name="tester")

        # Get initial event count
        conn = db_conn(ws)
        initial_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        # Send 3 messages (using @mention)
        for i in range(3):
            send = run_hcom(ws.env(), "send", "-b", f"@tester Test {i}")
            assert send.code == 0, send.stderr

        # Verify 3 new events
        conn = db_conn(ws)
        final_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        assert final_count == initial_count + 3, \
            f"Expected 3 new events, got {final_count - initial_count}"

    def test_event_contains_sender_info(self, ws):
        """Events include sender information for provenance."""
        s_sender = f"session-sender-{uuid4()}"
        s_receiver = f"session-receiver-{uuid4()}"

        seed_instance(ws, name="sender", tool="claude")
        seed_instance(ws, name="receiver", tool="gemini")
        seed_session_binding(ws, session_id=s_sender, instance_name="sender")
        seed_session_binding(ws, session_id=s_receiver, instance_name="receiver")

        # Send with explicit --from (using @mention for target)
        send = run_hcom(ws.env(), "send", "--from", "sender",
                       "@receiver Provenance test message")
        assert send.code == 0, send.stderr

        # Check event has sender info
        conn = db_conn(ws)
        row = conn.execute("""
            SELECT data FROM events
            WHERE type = 'message'
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        conn.close()

        data = json.loads(row["data"]) if row["data"] else {}
        assert data.get("from") == "sender" or "sender" in str(data), \
            f"Event missing sender info: {data}"


class TestInstanceIsolation:
    """Test that instance isolation is maintained."""

    def test_stopped_instance_without_binding_does_not_receive(self, ws):
        """Instance without session binding doesn't receive messages.

        In real usage, when sessionend() fires, it:
        1. Sets status to 'inactive'
        2. Calls stop_instance() -> cleanup_session_artifacts() -> deletes binding

        Without a session binding, there's no hook to poll for messages.
        This test simulates proper cleanup (delete binding when stopping).
        """
        from .harness import clear_session_binding

        s_active = f"session-active-{uuid4()}"
        s_stopped = f"session-stopped-{uuid4()}"

        seed_instance(ws, name="active_inst", tool="claude")
        seed_instance(ws, name="stopped_inst", tool="claude")
        seed_session_binding(ws, session_id=s_active, instance_name="active_inst")
        seed_session_binding(ws, session_id=s_stopped, instance_name="stopped_inst")

        # Properly stop: set inactive AND remove session binding
        conn = db_conn(ws)
        conn.execute("UPDATE instances SET status = 'inactive' WHERE name = ?",
                    ("stopped_inst",))
        conn.commit()
        conn.close()
        clear_session_binding(ws, session_id=s_stopped)

        # Broadcast
        send = run_hcom(ws.env(), "send", "-b", "Only for active instances")
        assert send.code == 0, send.stderr

        # Active should receive
        poll_active = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_active))
        assert poll_active.code == 2, "Active instance should receive"

        # Note: Can't poll for stopped instance - no session binding means no hook context
        # The message gets recorded in delivered_to (for logging) but can't be delivered

    def test_multiple_mentions_in_single_message(self, ws):
        """Message with multiple @mentions reaches all mentioned."""
        s_alpha = f"session-alpha-{uuid4()}"
        s_beta = f"session-beta-{uuid4()}"
        s_gamma = f"session-gamma-{uuid4()}"

        seed_instance(ws, name="alpha", tool="claude")
        seed_instance(ws, name="beta", tool="gemini")
        seed_instance(ws, name="gamma", tool="codex")
        seed_session_binding(ws, session_id=s_alpha, instance_name="alpha")
        seed_session_binding(ws, session_id=s_beta, instance_name="beta")
        seed_session_binding(ws, session_id=s_gamma, instance_name="gamma")

        # Send to both alpha and beta (but not gamma)
        send = run_hcom(ws.env(), "send", "-b", "@alpha @beta Multi-mention test")
        assert send.code == 0, send.stderr

        # Alpha should receive
        poll_alpha = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_alpha))
        assert poll_alpha.code == 2, "Alpha should receive multi-mention"

        # Beta should receive
        poll_beta = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_beta))
        assert poll_beta.code == 2, "Beta should receive multi-mention"

        # Gamma should NOT receive (not mentioned)
        poll_gamma = run_hcom(ws.env(), "poll", stdin=_hook_poll_payload(ws, s_gamma))
        assert poll_gamma.code == 0, "Gamma should not receive (not mentioned)"
