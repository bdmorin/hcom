#!/usr/bin/env python3
"""Tests for strict tool-args validation override env var."""

from __future__ import annotations

import pytest

from hcom.commands.utils import CLIError


def test_claude_cmd_launch_strict_blocks_unknown_flag(monkeypatch, capsys):
    from hcom.commands.lifecycle import cmd_launch

    monkeypatch.delenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", raising=False)

    rc = cmd_launch(["--moddel", "haiku"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown option" in err.lower()


def test_claude_cmd_launch_override_allows_unknown_flag(monkeypatch):
    from hcom.commands.lifecycle import cmd_launch

    monkeypatch.setenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", "1")

    # Prevent actual launching; we only care that validation is bypassed.
    def fake_op_launch(*args, **kwargs):
        return {"batch_id": "test", "launched": 1, "failed": 0, "background": False, "log_files": []}

    monkeypatch.setattr("hcom.core.ops.op_launch", fake_op_launch)

    rc = cmd_launch(["--moddel", "haiku"])
    assert rc == 0


def test_gemini_cmd_launch_strict_blocks_unknown_flag(monkeypatch):
    from hcom.commands.lifecycle import cmd_launch_gemini

    monkeypatch.delenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", raising=False)
    monkeypatch.setattr("hcom.tools.gemini.settings.setup_gemini_hooks", lambda **_: True)

    with pytest.raises(CLIError) as excinfo:
        cmd_launch_gemini(["1", "gemini", "--moddel"])

    assert "unknown option" in str(excinfo.value).lower()


def test_gemini_cmd_launch_override_allows_unknown_flag(monkeypatch):
    from hcom.commands.lifecycle import cmd_launch_gemini

    monkeypatch.setenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", "1")
    monkeypatch.setattr("hcom.tools.gemini.settings.setup_gemini_hooks", lambda **_: True)

    def fake_launch(*args, **kwargs):
        return {"batch_id": "test", "launched": 1, "failed": 0, "handles": [{"instance_name": "g1"}]}

    monkeypatch.setattr("hcom.launcher.launch", fake_launch)

    rc = cmd_launch_gemini(["1", "gemini", "--moddel"])
    assert rc == 0


def test_codex_cmd_launch_strict_blocks_unknown_flag(monkeypatch):
    from hcom.commands.lifecycle import cmd_launch_codex

    monkeypatch.delenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", raising=False)
    monkeypatch.setattr("hcom.tools.codex.settings.setup_codex_hooks", lambda **_: True)

    with pytest.raises(CLIError) as excinfo:
        cmd_launch_codex(["1", "codex", "--moddel"])

    assert "unknown option" in str(excinfo.value).lower()


def test_codex_cmd_launch_override_allows_unknown_flag(monkeypatch):
    from hcom.commands.lifecycle import cmd_launch_codex

    monkeypatch.setenv("HCOM_SKIP_TOOL_ARGS_VALIDATION", "1")
    monkeypatch.setattr("hcom.tools.codex.settings.setup_codex_hooks", lambda **_: True)

    def fake_launch(*args, **kwargs):
        return {"batch_id": "test", "launched": 1, "failed": 0, "handles": [{"instance_name": "c1"}]}

    monkeypatch.setattr("hcom.launcher.launch", fake_launch)

    rc = cmd_launch_codex(["1", "codex", "--moddel"])
    assert rc == 0
