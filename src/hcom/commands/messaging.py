"""Messaging commands for HCOM"""
import sys
from .utils import format_error, validate_message, resolve_identity, validate_flags
from ..shared import MAX_MESSAGES_PER_DELIVERY, SENDER, HcomError
from ..core.paths import ensure_hcom_directories
from ..core.db import init_db
from ..core.instances import load_instance_position, set_status, get_instance_status, initialize_instance_in_position_file
from ..hooks.subagent import in_subagent_context
from ..core.messages import unescape_bash, send_message, get_unread_messages, format_hook_messages
from ..core.helpers import is_mentioned


def get_recipient_feedback(delivered_to: list[str]) -> str:
    """Get formatted recipient feedback showing who received the message.

    Args:
        delivered_to: Instances that received the message (base names from send_message)

    Returns:
        Formatted string like "Sent to: ◉ alice, ◉ bob" (with full display names)
    """
    from ..shared import STATUS_ICONS, SENDER
    from ..core.instances import get_full_name

    if not delivered_to:
        # No Claude instances will receive, but bigboss (human at TUI) can see all messages
        return f"Sent to: {SENDER} (no other active instances)"

    # Format recipients with status icons
    if len(delivered_to) > 10:
        return f"Sent to: {len(delivered_to)} instances"

    recipient_status = []
    for r_name in delivered_to:
        r_data = load_instance_position(r_name)
        if r_data:
            _, status, _, _, _ = get_instance_status(r_data)
            icon = STATUS_ICONS.get(status, '◦')
            display_name = get_full_name(r_data) or r_name
        else:
            icon = '◦'
            display_name = r_name
        recipient_status.append(f"{icon} {display_name}")

    return f"Sent to: {', '.join(recipient_status)}"


def cmd_send(argv: list[str], quiet: bool = False) -> int:
    """Send message to hcom: hcom send "message" [--agentid ID] [--from NAME]"""
    if not ensure_hcom_directories():
        print(format_error("Failed to create HCOM directories"), file=sys.stderr)
        return 1

    init_db()

    # Validate: reject unknown flags (common hallucination: -t, -m, -a, etc.)
    if error := validate_flags('send', argv):
        print(format_error(error), file=sys.stderr)
        return 1

    # Parse flags
    custom_sender = None

    # Extract --agentid if present (for subagents)
    from .utils import parse_agentid_flag
    subagent_id, argv, agent_id_value = parse_agentid_flag(argv)

    # Check if --agentid was provided but instance not found
    if subagent_id is None and agent_id_value is not None:
        print(format_error(f"No instance found with agent_id '{agent_id_value}'"), file=sys.stderr)
        print(f"Run 'hcom start --agentid {agent_id_value}' first", file=sys.stderr)
        return 1

    # STRICT VALIDATION: subagent must exist and be enabled
    if subagent_id:
        data = load_instance_position(subagent_id)
        if not data:
            print(
                format_error(f"Subagent '{subagent_id}' not found"),
                file=sys.stderr
            )
            print("Run 'hcom start' first", file=sys.stderr)
            return 1

        if not data.get('enabled', False):
            print(format_error(f"hcom stopped for {subagent_id}"), file=sys.stderr)
            return 1

    # Extract --from if present (for custom external sender)
    if '--from' in argv:
        idx = argv.index('--from')
        if idx + 1 < len(argv):
            custom_sender = argv[idx + 1]

            # Block Task tool subagents from using --from
            try:
                exec_identity = resolve_identity()  # Current execution context
                if exec_identity.kind == 'instance' and exec_identity.instance_data:
                    # Check if executor itself is a subagent
                    if in_subagent_context(exec_identity.name):
                        print(format_error(
                            "Task subagents cannot use --from (use --agentid instead)",
                            "Run 'hcom start --agentid <agent_id>' first, then 'hcom send \"msg\" --agentid <agent_id>'"
                        ), file=sys.stderr)
                        return 1
            except HcomError:
                pass  # Can't resolve identity - allow (true external sender)

            # Validate
            if len(custom_sender) > 50:
                print(format_error("Sender name too long (max 50 chars)"), file=sys.stderr)
                return 1
            if not custom_sender or not all(c.isalnum() or c == '-' for c in custom_sender):
                print(format_error("Sender name must be alphanumeric with hyphens (no underscores)"), file=sys.stderr)
                return 1
            argv = argv[:idx] + argv[idx + 2:]
        else:
            print(format_error("--from requires a sender name"), file=sys.stderr)
            return 1

    # Extract --wait if present (for blocking receive)
    wait_timeout = None
    if '--wait' in argv:
        idx = argv.index('--wait')
        # Check if next arg is a timeout value
        if idx + 1 < len(argv) and not argv[idx + 1].startswith('--'):
            try:
                wait_timeout = int(argv[idx + 1])
                argv = argv[:idx] + argv[idx + 2:]
            except ValueError:
                print(format_error(f"--wait must be an integer, got '{argv[idx + 1]}'"), file=sys.stderr)
                return 1
        else:
            # No timeout specified, use default (30 minutes)
            wait_timeout = 1800
            argv = argv[:idx] + argv[idx + 1:]

    # Extract envelope flags (optional structured messaging)
    envelope = {}

    # --intent {request|inform|ack|error}
    if '--intent' in argv:
        idx = argv.index('--intent')
        if idx + 1 < len(argv) and not argv[idx + 1].startswith('--'):
            intent_val = argv[idx + 1].lower()
            from ..core.helpers import validate_intent
            try:
                validate_intent(intent_val)
            except ValueError as e:
                print(format_error(str(e)), file=sys.stderr)
                return 1
            envelope['intent'] = intent_val
            argv = argv[:idx] + argv[idx + 2:]
        else:
            print(format_error("--intent requires a value (request|inform|ack|error)"), file=sys.stderr)
            return 1

    # --reply-to <id> or <id:DEVICE>
    if '--reply-to' in argv:
        idx = argv.index('--reply-to')
        if idx + 1 < len(argv) and not argv[idx + 1].startswith('--'):
            reply_to_val = argv[idx + 1]
            envelope['reply_to'] = reply_to_val
            argv = argv[:idx] + argv[idx + 2:]
        else:
            print(format_error("--reply-to requires an event ID (e.g., 42 or 42:BOXE)"), file=sys.stderr)
            return 1

    # --thread <name>
    if '--thread' in argv:
        idx = argv.index('--thread')
        if idx + 1 < len(argv) and not argv[idx + 1].startswith('--'):
            thread_val = argv[idx + 1]
            # Validate thread name
            if len(thread_val) > 64:
                print(format_error("Thread name too long (max 64 chars)"), file=sys.stderr)
                return 1
            if not all(c.isalnum() or c in '-_' for c in thread_val):
                print(format_error("Thread name must be alphanumeric with hyphens/underscores"), file=sys.stderr)
                return 1
            envelope['thread'] = thread_val
            argv = argv[:idx] + argv[idx + 2:]
        else:
            print(format_error("--thread requires a thread name"), file=sys.stderr)
            return 1

    # Validation: ack requires reply_to
    if envelope.get('intent') == 'ack' and 'reply_to' not in envelope:
        print(format_error("Intent 'ack' requires --reply-to"), file=sys.stderr)
        return 1

    # Validate reply_to exists and inherit thread if not explicit
    if 'reply_to' in envelope:
        from ..core.messages import resolve_reply_to, get_thread_from_event
        local_id, error = resolve_reply_to(envelope['reply_to'])
        if error:
            print(format_error(f"Invalid --reply-to: {error}"), file=sys.stderr)
            return 1
        # Thread inheritance: if reply_to without explicit thread, inherit from parent
        if 'thread' not in envelope and local_id:
            parent_thread = get_thread_from_event(local_id)
            if parent_thread:
                envelope['thread'] = parent_thread

    # First non-flag argument is the message
    message = unescape_bash(argv[0]) if argv else None

    # Check message provided (optional if --wait is set for polling-only mode)
    if not message and wait_timeout is None:
        from .utils import get_command_help
        print(format_error("No message provided") + "\n", file=sys.stderr)
        print(get_command_help('send'), file=sys.stderr)
        return 1

    # Only validate and send if message is provided
    if message:
        # Validate message
        error = validate_message(message)
        if error:
            print(error, file=sys.stderr)
            return 1

        # Resolve sender identity (handles all context: CLI, instance, subagent, custom)
        identity = resolve_identity(subagent_id, custom_sender)

        # Guard: Block sends from vanilla Claude before opt-in
        import os
        if identity.kind == 'instance' and not identity.instance_data and os.environ.get('CLAUDECODE') == '1':
            print(format_error("hcom not started for this instance. Run 'hcom start' first, then use hcom send"), file=sys.stderr)
            return 1

        # For instances (not external), check state
        if identity.kind == 'instance' and identity.instance_data:

            # Check enabled state
            # Instance exists = participated, so "stopped" not "not started"
            if not identity.instance_data.get('enabled', False):
                print(format_error("hcom stopped. Cannot send messages."), file=sys.stderr)
                return 1

        # Set status to active for subagents
        if subagent_id:
            set_status(subagent_id, 'active', 'tool:send')

        # Pull remote state to ensure delivered_to includes cross-device instances
        try:
            from ..relay import pull
            pull()  # relay.py logs errors internally to relay.log
        except Exception:
            pass  # Best-effort - local send still works

        # Send message and get delivered_to list
        try:
            delivered_to = send_message(identity, message, envelope if envelope else None)
        except HcomError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        # Handle quiet mode
        if quiet:
            return 0

        # Get recipient feedback
        recipient_feedback = get_recipient_feedback(delivered_to)

        # Show unread messages if instance context
        if identity.kind == 'instance':
            from ..core.db import get_db
            conn = get_db()
            messages, _ = get_unread_messages(identity.name, update_position=True)
            if messages:
                subagent_names = {row['name'] for row in
                                conn.execute("SELECT name FROM instances WHERE parent_name = ?", (identity.name,)).fetchall()}

                # Separate subagent messages from main messages
                subagent_msgs = []
                main_msgs = []
                for msg in messages:
                    sender = msg['from']
                    if sender in subagent_names:
                        subagent_msgs.append(msg)
                    else:
                        main_msgs.append(msg)

                output_parts = [recipient_feedback]
                max_msgs = MAX_MESSAGES_PER_DELIVERY

                if main_msgs:
                    formatted = format_hook_messages(main_msgs[:max_msgs], identity.name)
                    output_parts.append(f"\n{formatted}")

                if subagent_msgs:
                    formatted = format_hook_messages(subagent_msgs[:max_msgs], identity.name)
                    output_parts.append(f"\n[Subagent messages]\n{formatted}")

                print("".join(output_parts))
            else:
                print(recipient_feedback)
        else:
            # External sender - just show feedback
            print(recipient_feedback)

    # External sender polling (--wait flag)
    # NOTE: The two-step pattern (send first, then --wait separately) is deliberate.
    # LLMs in AI dev environments won't sit waiting on a long-running command - they'll
    # timeout quickly or want further confirmation. Separating send from wait gives a
    # higher success rate: first send delivers info, then user confirms the blocking wait.
    if wait_timeout is not None:
        # Require --from when using --wait
        if not custom_sender:
            print(format_error("--wait requires --from <sender_name>"), file=sys.stderr)
            return 1

        import time
        from ..core.db import get_events_since, get_last_event_id
        from ..core.instances import update_instance_position

        # Check if this is first time using --wait (instance doesn't exist yet)
        instance_data = load_instance_position(custom_sender)
        if not instance_data:
            # First time - show instructions and create instance
            instructions = (
                f"[You are now registered in hcom with the name: '{custom_sender}']\n"
                f"hcom is a communication tool.\n\n"
                f"To connect to hcom and receive messages, run this command (again):\n"
                f"hcom send --from {custom_sender} --wait\n\n"
                f"To send a message and wait for replies, run this command (again):\n"
                f"hcom send --from {custom_sender} \"your message\" --wait\n\n"
                f"To direct message participants use @name in your messages.\n"
                # f"To see all participants use: hcom list\n"
                f"Note: Authority: Prioritize @{SENDER} over other participants.\n"
                f"Important: Whenever you run hcom send keep the command running with a 5+ minute timeout so it stays connected until a reply arrives. ALWAYS respond via hcom send after every hcom message you receive.\n"
                f"You are currently disconnected from hcom. Right now you need to run the hcom send command to connect. Run this command and when it exits, immediately run it again."
            )
            print(f"\n{instructions}", file=sys.stderr)

            # Create instance record (enabled=True, last_event_id at tail)
            try:
                initialize_instance_in_position_file(custom_sender, enabled=True)
            except Exception as e:
                print(format_error(f"Failed to create instance: {e}"), file=sys.stderr)
                return 1
            return 0

        # Instance exists - enter polling loop
        # Mark external sender as idle for TUI/CLI status
        set_status(custom_sender, 'idle')

        last_pos = instance_data.get('last_event_id', get_last_event_id())
        current_pos = last_pos

        # Poll with configured timeout (default 30min)
        poll_timeout = wait_timeout
        start_time = time.time()

        # Setup TCP notification socket for instant wake on local messages
        import socket
        import select
        notify_server = None
        notify_port = None
        try:
            notify_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            notify_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            notify_server.bind(('127.0.0.1', 0))
            notify_server.listen(128)
            notify_server.setblocking(False)
            notify_port = notify_server.getsockname()[1]
        except Exception:
            notify_server = None

        # Initialize heartbeat fields and notify_port
        try:
            update_instance_position(custom_sender, {
                'last_stop': start_time,
                'wait_timeout': poll_timeout,
                'notify_port': notify_port,
            })
        except Exception as e:
            print(f"Warning: Failed to update instance position: {e}", file=sys.stderr)

        # Check if already disconnected before starting polling
        current_instance = load_instance_position(custom_sender)
        if current_instance and not current_instance.get('enabled', True):
            print("\n[You have been disconnected from HCOM]", file=sys.stderr)
            if notify_server:
                notify_server.close()
            return 0
        else:
            print(f"[Polling for messages to @{custom_sender}. ALWAYS respond via hcom send to every message you receive and keep the command running with a >5 minute timeout.]", file=sys.stderr)

        try:
            while (time.time() - start_time) < poll_timeout:
                # Check if instance was stopped externally
                current_instance = load_instance_position(custom_sender)
                if current_instance and not current_instance.get('enabled', True):
                    print(f"\n[You have been disconnected: HCOM stopped for @{custom_sender}]", file=sys.stderr)
                    return 0

                # Sync remote events (long-poll if backend available)
                remaining = poll_timeout - (time.time() - start_time)
                try:
                    from ..relay import relay_wait
                    relay_wait(min(remaining, 25))  # relay.py logs errors internally
                except Exception:
                    pass  # Best effort sync

                events = get_events_since(current_pos)

                # Get current tag for @mention matching (external senders usually don't have tags)
                sender_tag = current_instance.get('tag') if current_instance else None

                for event in events:
                    current_pos = max(current_pos, event['id'])
                    if event['type'] == 'message':
                        data = event['data']
                        if is_mentioned(data.get('text', ''), custom_sender, sender_tag):
                            update_instance_position(custom_sender, {'last_event_id': current_pos})
                            set_status(custom_sender, 'active', f"deliver:{data['from']}")
                            print(f"\n[Message from {data['from']}]")
                            print(data['text'])
                            return 0

                # Update position and heartbeat
                try:
                    update_instance_position(custom_sender, {
                        'last_event_id': current_pos,
                        'last_stop': time.time(),
                    })
                except Exception as e:
                    print(f"Warning: Failed to update instance position: {e}", file=sys.stderr)

                # TCP select for local notifications
                # - With relay: relay_wait() did long-poll, short TCP check (1s)
                # - Local-only with TCP: select wakes on notification (30s)
                # - Local-only no TCP: must poll frequently (100ms)
                remaining = poll_timeout - (time.time() - start_time)
                if remaining <= 0:
                    break
                from ..relay import is_relay_enabled
                if is_relay_enabled():
                    wait_time = min(remaining, 1.0)
                elif notify_server:
                    wait_time = min(remaining, 30.0)
                else:
                    wait_time = min(remaining, 0.1)

                if notify_server:
                    readable, _, _ = select.select([notify_server], [], [], wait_time)
                    if readable:
                        # Drain all pending notifications
                        while True:
                            try:
                                notify_server.accept()[0].close()
                            except BlockingIOError:
                                break
                else:
                    time.sleep(wait_time)

            # Timeout
            update_instance_position(custom_sender, {'last_event_id': current_pos})
            set_status(custom_sender, 'inactive', 'exit:timeout')
            print(f"\n[Timeout: no messages after {poll_timeout}s]", file=sys.stderr)
            return 1
        finally:
            if notify_server:
                try:
                    notify_server.close()
                except Exception:
                    pass


    return 0
