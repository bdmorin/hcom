"""One-time tips shown on first use of commands.

Uses kv store to track which tips have been shown per instance.
"""

TIPS = {
    "list": """\
[tip] Statuses: ▶ active (will read new msgs very soon)  ◉ listening (will read new msgs in <1s)  ■ blocked (needs human user approval)  ○ inactive (dead)  ◦ unknown (neutral)
      Types: [CLAUDE] [GEMINI] [CODEX] [claude] full features, automatic msg delivery | [AD-HOC] [gemini] [codex] limited""",
    # Send-side (shown after send with --intent)
    "send:intent:request": "[tip] intent=request: You signaled you expect a response.",
    "send:intent:inform": "[tip] intent=inform: You signaled no response needed.",
    "send:intent:ack": "[tip] intent=ack: You acknowledged receipt. Recipient won't respond.",
    # Recv-side (appended to message on first receipt of each type)
    "recv:intent:request": "[tip] intent=request: Sender expects a response.",
    "recv:intent:inform": "[tip] intent=inform: Sender doesn't expect a response.",
    "recv:intent:ack": "[tip] intent=ack: Sender confirmed receipt. No response needed.",
    # @mention matching
    "mention:matching": "[tip] @targets: @api- matches all with tag 'api' | @luna matches prefix | underscore blocks: @luna won't match luna_sub_1",
}


def _tip_key(instance_name: str, command: str) -> str:
    """Get kv key for tip tracking."""
    return f"tip:{instance_name}:{command}"


def has_seen_tip(instance_name: str, command: str) -> bool:
    """Check if instance has seen this tip before."""
    if not instance_name:
        return True
    from .db import kv_get

    return kv_get(_tip_key(instance_name, command)) is not None


def mark_tip_seen(instance_name: str, command: str):
    """Mark tip as seen for this instance."""
    if not instance_name:
        return
    from .db import kv_set

    kv_set(_tip_key(instance_name, command), "1")


def maybe_show_tip(instance_name: str, command: str, *, json_output: bool = False):
    """Show one-time tip for command if not seen before."""
    if json_output or command not in TIPS:
        return
    if has_seen_tip(instance_name, command):
        return
    mark_tip_seen(instance_name, command)
    print(f"\n{TIPS[command]}")
