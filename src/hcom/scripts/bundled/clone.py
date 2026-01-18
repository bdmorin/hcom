#!/usr/bin/env python3
"""Spawn clone for a task, result sent back automatically via hcom.

Fork a session, give the clone a task, get results back via hcom.
Clone has full memory but is read-only by default.
Supports Claude (background or interactive) and Codex (interactive only).

Usage:
    hcom run clone "analyze the auth module"     # clone myself
    hcom run clone --target nova "review this"   # clone nova instead
"""

from __future__ import annotations

import argparse
import sys

from hcom.api import HcomError, instances, launch, session


def main():
    parser = argparse.ArgumentParser(
        description="""Fork an instance with full conversation memory to perform a read-only task.

The clone inherits the parent's entire context (files read, decisions made,
conversation history) but operates independently. Results are sent back via
hcom message to the parent instance.

USE WHEN:
  - Need parallel analysis without interrupting main work
  - Want a second opinion with full context
  - Offloading research/review tasks to a background worker
  - Debugging: clone can investigate while you continue

WHAT HAPPENS:
  1. Forks the session (--resume --fork-session)
  2. Clone receives task + instructions to report back
  3. Clone works in background (or terminal with -i)
  4. Clone sends results via: hcom send "@parent <results>"
  5. Clone stops (unless --no-stop)""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick analysis while you keep working:
  hcom run clone "analyze the auth module for security issues"

  # Clone another instance:
  hcom run clone --target nova "review the auth changes"

  # Keep clone alive for follow-ups:
  hcom run clone "review my last 3 commits" --no-stop

  # Interactive debugging session in new terminal:
  hcom run clone "help debug this test failure" --interactive
""",
    )
    parser.add_argument("task", help="Task description for the clone to perform")
    parser.add_argument("--target", help="Instance to clone (default: self)")
    parser.add_argument("--name", help="Your identity")
    parser.add_argument(
        "--no-stop",
        action="store_true",
        help="Keep clone alive after task (for follow-up questions)",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Launch in terminal window instead of background",
    )
    args = parser.parse_args()

    # Get caller session
    try:
        s = session(name=args.name)
    except HcomError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    caller_name = s.name

    # Target defaults to caller (clone self)
    target = args.target or caller_name
    if target != caller_name:
        try:
            info = instances(name=target)
        except HcomError:
            print(f"Error: instance '{target}' not found", file=sys.stderr)
            sys.exit(1)
    else:
        info = s.info

    session_id = info.get("session_id")
    instance_name = info.get("name")
    tool = info.get("tool", "claude")

    # Only claude and codex support forking
    if tool not in ("claude", "codex"):
        print(
            f"Error: '{instance_name}' uses {tool} which does not support forking",
            file=sys.stderr,
        )
        sys.exit(1)

    if not session_id:
        print(f"Error: '{instance_name}' has no session_id - cannot fork", file=sys.stderr)
        sys.exit(1)

    # Codex doesn't support background mode
    if tool == "codex" and not args.interactive:
        print(
            "Note: Codex doesn't support background mode, launching interactive",
            file=sys.stderr,
        )

    stop_instruction = "\n3. hcom stop" if not args.no_stop else ""
    target_dir = info.get("directory")

    clone_prompt = f'''Task from @{caller_name}: {args.task}

Do not modify any files. Read-only analysis only.

When done:
1. Do your analysis
2. hcom send "@{caller_name} <your response>"{stop_instruction}'''

    try:
        result = launch(
            1,
            tool=tool,
            tag="clone",
            prompt=clone_prompt,
            background=not args.interactive and tool != "codex",
            resume=session_id,
            fork=True,
            cwd=target_dir,
        )
    except HcomError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Check if launch actually succeeded
    if result.get("failed", 0) > 0 or result.get("launched", 0) == 0:
        errors = result.get("errors", [])
        if errors:
            error_msgs = [e.get("error", str(e)) for e in errors]
            print(f"Error: Launch failed: {'; '.join(error_msgs)}", file=sys.stderr)
        else:
            print("Error: Launch failed (no instances launched)", file=sys.stderr)
        sys.exit(1)

    print(f"Clone launched: {result['batch_id'][:8]}")
    print(f"Task: {args.task}")
    print(f"Reports to: @{caller_name}")
    if not args.interactive and tool != "codex":
        print("Mode: background")
    if args.no_stop:
        print("Stays alive after task")


if __name__ == "__main__":
    main()
