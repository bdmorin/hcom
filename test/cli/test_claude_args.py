#!/usr/bin/env python3
"""Unit tests for claude_args (in shared.py) - Claude CLI argument parsing and composition."""

# run 'claude --help' to see all current up-to-date possible flags and options
# claude --help from 29 oct 2025: docs/external/claude-code/claude--help.md
# docs/external/claude-code/ relevant files: iam.md cli-reference.md settings.md

import pytest
from hypothesis import given, strategies as st, assume, settings
from hcom.tools.claude.args import (
    resolve_claude_args,
    merge_claude_args,
    validate_conflicts,
    add_background_defaults,
    ClaudeArgsSpec,
)


class TestBasicParsing:
    """Test fundamental parsing scenarios."""

    def test_empty_args(self):
        """Empty input produces empty spec."""
        spec = resolve_claude_args(None, None)
        assert spec.source == "none"
        assert spec.raw_tokens == ()
        assert spec.clean_tokens == ()
        assert spec.positional_tokens == ()
        assert not spec.is_background
        assert not spec.errors

    def test_simple_positional(self):
        """Single positional argument parsed correctly."""
        spec = resolve_claude_args(["hello world"], None)
        assert spec.positional_tokens == ("hello world",)
        assert spec.clean_tokens == ("hello world",)
        assert spec.positional_indexes == (0,)

    def test_background_flag_short(self):
        """Background -p flag detected."""
        spec = resolve_claude_args(["-p"], None)
        assert spec.is_background
        assert spec.clean_tokens == ("-p",)

    def test_background_flag_long(self):
        """Background --print flag detected."""
        spec = resolve_claude_args(["--print"], None)
        assert spec.is_background
        assert spec.clean_tokens == ("--print",)

    def test_model_flag_separate(self):
        """Model flag with separate value."""
        spec = resolve_claude_args(["--model", "sonnet"], None)
        assert spec.flag_values["--model"] == "sonnet"
        assert spec.clean_tokens == ("--model", "sonnet")

    def test_model_flag_equals(self):
        """Model flag with equals syntax."""
        spec = resolve_claude_args(["--model=opus"], None)
        assert spec.flag_values["--model"] == "opus"
        assert spec.clean_tokens == ("--model=opus",)

    def test_debug_file_flag_separate(self):
        """--debug-file with separate value."""
        spec = resolve_claude_args(["--debug-file", "/tmp/debug.log"], None)
        assert spec.get_flag_value("--debug-file") == "/tmp/debug.log"
        assert spec.clean_tokens == ("--debug-file", "/tmp/debug.log")

    def test_debug_file_flag_equals(self):
        """--debug-file with equals syntax."""
        spec = resolve_claude_args(["--debug-file=/var/log/claude.log"], None)
        assert spec.get_flag_value("--debug-file") == "/var/log/claude.log"
        assert spec.clean_tokens == ("--debug-file=/var/log/claude.log",)

    def test_unknown_option_has_suggestion(self):
        spec = resolve_claude_args(["--moddel", "haiku"], None)
        assert spec.has_errors()
        assert any("unknown option" in err.lower() for err in spec.errors)
        assert any("--model" in err for err in spec.errors)


class TestSystemPrompts:
    """Test system prompt parsing and merging."""

    def test_append_system_prompt_separate(self):
        """--append-system-prompt with separate value."""
        spec = resolve_claude_args(["--append-system-prompt", "be concise"], None)
        assert spec.get_flag_value("--append-system-prompt") == "be concise"
        # System prompts NOW in clean_tokens
        assert spec.clean_tokens == ("--append-system-prompt", "be concise")

    def test_system_prompt_separate(self):
        """--system-prompt with separate value."""
        spec = resolve_claude_args(["--system-prompt", "you are helpful"], None)
        assert spec.get_flag_value("--system-prompt") == "you are helpful"
        assert spec.clean_tokens == ("--system-prompt", "you are helpful")

    def test_system_prompt_equals(self):
        """--system-prompt= syntax."""
        spec = resolve_claude_args(["--system-prompt=be brief"], None)
        assert spec.get_flag_value("--system-prompt") == "be brief"
        assert spec.clean_tokens == ("--system-prompt=be brief",)

    def test_both_system_prompts(self):
        """Both append and system flags present."""
        spec = resolve_claude_args(["--append-system-prompt", "first", "--system-prompt", "second"], None)
        assert spec.get_flag_value("--append-system-prompt") == "first"
        assert spec.get_flag_value("--system-prompt") == "second"
        # Both in clean_tokens
        assert "--append-system-prompt" in spec.clean_tokens
        assert "first" in spec.clean_tokens
        assert "--system-prompt" in spec.clean_tokens
        assert "second" in spec.clean_tokens

    def test_system_prompt_missing_value(self):
        """System prompt flag without value is an error."""
        spec = resolve_claude_args(["--system-prompt"], None)
        assert spec.has_errors()
        assert any("requires a value" in err for err in spec.errors)


class TestBackgroundMode:
    """Test background flag detection and manipulation."""

    def test_background_with_prompt(self):
        """Background flag plus positional prompt."""
        spec = resolve_claude_args(["-p", "do task"], None)
        assert spec.is_background
        assert spec.positional_tokens == ("do task",)

    def test_duplicate_background_flags(self):
        """Multiple background flags (both -p and --print)."""
        spec = resolve_claude_args(["-p", "--print", "hello"], None)
        assert spec.is_background
        # Both flags preserved in clean_tokens
        assert "-p" in spec.clean_tokens
        assert "--print" in spec.clean_tokens

    def test_update_add_background(self):
        """Add background flag via update()."""
        spec = resolve_claude_args(["hello"], None)
        assert not spec.is_background

        updated = spec.update(background=True)
        assert updated.is_background
        assert "-p" in updated.clean_tokens

    def test_update_remove_background(self):
        """Remove background flag via update()."""
        spec = resolve_claude_args(["-p", "hello"], None)
        assert spec.is_background

        updated = spec.update(background=False)
        assert not updated.is_background
        assert "-p" not in updated.clean_tokens

    def test_has_flag_detects_background(self):
        """has_flag() should detect background flags in clean_tokens."""
        spec_short = resolve_claude_args(["-p", "task"], None)
        assert spec_short.has_flag(["-p"])
        assert spec_short.is_background

        spec_long = resolve_claude_args(["--print", "task"], None)
        assert spec_long.has_flag(["--print"])
        assert spec_long.is_background


class TestPositionalArguments:
    """Test positional argument handling."""

    def test_multiple_positionals(self):
        """Multiple positional arguments preserved."""
        spec = resolve_claude_args(["first", "second", "third"], None)
        assert spec.positional_tokens == ("first", "second", "third")
        assert spec.positional_indexes == (0, 1, 2)

    def test_positionals_with_flags(self):
        """Positionals mixed with flags."""
        spec = resolve_claude_args(["--model", "sonnet", "do task", "--verbose"], None)
        assert spec.positional_tokens == ("do task",)
        # Only the prompt is positional, flags are not
        assert len(spec.positional_indexes) == 1

    def test_double_dash_delimiter(self):
        """Arguments after -- are all positional."""
        spec = resolve_claude_args(["--model", "sonnet", "--", "--not-a-flag"], None)
        assert "--not-a-flag" in spec.positional_tokens
        # Check that -- is in clean_tokens but not positional
        assert "--" in spec.clean_tokens

    def test_update_prompt_empty(self):
        """Update adds prompt when none exists."""
        spec = resolve_claude_args([], None)
        updated = spec.update(prompt="new task")
        assert updated.positional_tokens == ("new task",)

    def test_update_prompt_replaces(self):
        """Update replaces first positional prompt."""
        spec = resolve_claude_args(["old task"], None)
        updated = spec.update(prompt="new task")
        assert updated.positional_tokens == ("new task",)
        # Only one positional
        assert len(updated.positional_tokens) == 1


class TestFlagValues:
    """Test extraction of flag values."""

    def test_allowed_tools_separate(self):
        """--allowedTools with separate value."""
        spec = resolve_claude_args(["--allowedTools", "Read,Write"], None)
        assert spec.flag_values["--allowedTools"] == "Read,Write"

    def test_allowed_tools_equals(self):
        """--allowedTools= syntax."""
        spec = resolve_claude_args(["--allowedTools=Bash"], None)
        assert spec.flag_values["--allowedTools"] == "Bash"

    def test_allowed_tools_alias(self):
        """--allowed-tools (hyphenated alias)."""
        spec = resolve_claude_args(["--allowed-tools", "Grep"], None)
        assert spec.flag_values["--allowedTools"] == "Grep"

    def test_tools_separate(self):
        """--tools with separate value."""
        spec = resolve_claude_args(["--tools", "Bash,Grep"], None)
        assert spec.get_flag_value("--tools") == "Bash,Grep"

    def test_tools_equals(self):
        """--tools= syntax."""
        spec = resolve_claude_args(["--tools=Read,Write"], None)
        assert spec.get_flag_value("--tools") == "Read,Write"

    def test_has_flag_by_name(self):
        """has_flag() detects flags by name."""
        spec = resolve_claude_args(["--model", "sonnet"], None)
        assert spec.has_flag(names=["--model"])
        assert not spec.has_flag(names=["--verbose"])

    def test_has_flag_by_prefix(self):
        """has_flag() detects flags by prefix."""
        spec = resolve_claude_args(["--model=sonnet"], None)
        assert spec.has_flag(prefixes=["--model="])


class TestBooleanFlags:
    """Test boolean flag handling."""

    @pytest.mark.parametrize(
        "flag",
        [
            "--allow-dangerously-skip-permissions",
            "--replay-user-messages",
            "--mcp-debug",
            "--fork-session",
            "--ide",
            "--strict-mcp-config",
            "--dangerously-skip-permissions",
            "--include-partial-messages",
            "--verbose",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--chrome",
            "--no-chrome",
        ],
    )
    def test_boolean_flag_with_prompt(self, flag):
        """Boolean flags should not consume following prompt."""
        spec = resolve_claude_args([flag, "do task"], None)
        assert flag in spec.clean_tokens
        assert spec.positional_tokens == ("do task",)
        assert not spec.has_errors()

    @pytest.mark.parametrize(
        "flag",
        [
            "--allow-dangerously-skip-permissions",
            "--replay-user-messages",
            "--mcp-debug",
            "--fork-session",
            "--ide",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--chrome",
            "--no-chrome",
        ],
    )
    def test_boolean_flag_alone(self, flag):
        """Boolean flags alone should not register as prompts."""
        spec = resolve_claude_args([flag], None)
        assert flag in spec.clean_tokens
        assert spec.positional_tokens == ()
        assert not spec.has_errors()


class TestErrorHandling:
    """Test error detection and reporting."""

    def test_flag_missing_value_at_end(self):
        """Flag at end without value is an error."""
        spec = resolve_claude_args(["hello", "--model"], None)
        assert spec.has_errors()
        assert any("--model" in err and "requires a value" in err for err in spec.errors)

    def test_flag_missing_value_before_flag(self):
        """Flag without value before another flag."""
        spec = resolve_claude_args(["--model", "--verbose"], None)
        assert spec.has_errors()
        assert any("--model" in err for err in spec.errors)

    def test_multiple_errors_accumulated(self):
        """Multiple errors collected."""
        spec = resolve_claude_args(["--model", "--system-prompt"], None)
        assert spec.has_errors()
        # Both flags missing values
        assert len(spec.errors) == 2

    def test_env_string_invalid_quoting(self):
        """Invalid shell quoting in env string."""
        spec = resolve_claude_args(None, 'unmatched "quote')
        assert spec.has_errors()
        assert any("invalid Claude args" in err for err in spec.errors)


class TestEnvStringParsing:
    """Test parsing from HCOM_CLAUDE_ARGS env string."""

    def test_env_string_simple(self):
        """Simple env string parsed."""
        spec = resolve_claude_args(None, "-p --model sonnet")
        assert spec.source == "env"
        assert spec.is_background
        assert spec.flag_values["--model"] == "sonnet"

    def test_env_string_quoted(self):
        """Quoted values in env string."""
        spec = resolve_claude_args(None, '--system-prompt "be helpful"')
        assert spec.get_flag_value("--system-prompt") == "be helpful"

    def test_env_string_escapes(self):
        """Shell escapes in env string."""
        spec = resolve_claude_args(None, r'--system-prompt "say \"hello\""')
        assert spec.get_flag_value("--system-prompt") == 'say "hello"'

    def test_cli_overrides_env(self):
        """CLI args take precedence over env."""
        spec = resolve_claude_args(["--model", "opus"], "--model sonnet")
        assert spec.source == "cli"
        assert spec.flag_values["--model"] == "opus"


class TestRebuildTokens:
    """Test token rebuilding for command composition."""

    def test_rebuild_without_positionals_keeps_system_prompts(self):
        """Verify PTY case: strip positionals, keep system prompts."""
        spec = resolve_claude_args(["--system-prompt", "be helpful", "say hi in hcom chat"], None)
        tokens = spec.rebuild_tokens(include_positionals=False)
        # System prompts kept (they're flags, not positionals)
        assert "--system-prompt" in tokens
        assert "be helpful" in tokens
        # Positional stripped
        assert "say hi in hcom chat" not in tokens

    def test_rebuild_includes_system_prompts(self):
        """rebuild_tokens() includes system prompts by default."""
        spec = resolve_claude_args(["--system-prompt", "help"], None)
        tokens = spec.rebuild_tokens()
        assert "--system-prompt" in tokens
        assert "help" in tokens

    def test_to_env_string(self):
        """to_env_string() produces valid shell string."""
        spec = resolve_claude_args(["-p", "--model", "sonnet", "do task"], None)
        env_str = spec.to_env_string()
        # Should be shell-quoted
        assert "-p" in env_str
        assert "sonnet" in env_str
        # Reparsing should give same result
        reparsed = resolve_claude_args(None, env_str)
        assert reparsed.is_background
        assert reparsed.flag_values["--model"] == "sonnet"


class TestUpdateMethod:
    """Test ClaudeArgsSpec.update() for modifications."""

    def test_update_combined(self):
        """Update multiple fields at once."""
        spec = resolve_claude_args([], None)
        updated = spec.update(background=True, prompt="task")
        assert updated.is_background
        assert updated.positional_tokens == ("task",)


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    def test_empty_positional(self):
        """Empty string as positional is preserved."""
        spec = resolve_claude_args([""], None)
        assert spec.positional_tokens == ("",)

    def test_dash_as_flag(self):
        """Single dash treated as positional (not a known flag)."""
        spec = resolve_claude_args(["-"], None)
        # Parser only treats KNOWN flags as flags
        # Single "-" doesn't match any flag pattern, so it's positional
        # (Unix stdin convention "-" would need special handling if desired)
        assert spec.positional_tokens == ("-",)
        assert "-" in spec.clean_tokens

    def test_numeric_positional(self):
        """Numeric string is positional."""
        spec = resolve_claude_args(["123"], None)
        assert spec.positional_tokens == ("123",)

    def test_flag_like_positional_after_double_dash(self):
        """Flags after -- are positional."""
        spec = resolve_claude_args(["--", "--not-a-flag", "-p"], None)
        assert "--not-a-flag" in spec.positional_tokens
        assert "-p" in spec.positional_tokens
        # Background flag NOT detected (after --)
        assert not spec.is_background

    def test_very_long_prompt(self):
        """Very long positional prompt handled."""
        long_prompt = "x" * 10000
        spec = resolve_claude_args([long_prompt], None)
        assert spec.positional_tokens[0] == long_prompt

    def test_unicode_in_prompt(self):
        """Unicode characters in prompts preserved."""
        spec = resolve_claude_args(["你好世界 🎉"], None)
        assert spec.positional_tokens[0] == "你好世界 🎉"

    def test_newlines_in_prompt(self):
        """Newlines in prompts preserved."""
        prompt = "line1\nline2\nline3"
        spec = resolve_claude_args([prompt], None)
        assert spec.positional_tokens[0] == prompt

    def test_case_sensitivity_flags(self):
        """Flag matching is case-insensitive for detection."""
        spec = resolve_claude_args(["--MODEL", "sonnet"], None)
        # Lowercase matching
        assert spec.flag_values["--model"] == "sonnet"

    def test_unknown_flags_preserved(self):
        """Unknown flags passed through in clean_tokens."""
        spec = resolve_claude_args(["--unknown-flag", "value"], None)
        assert "--unknown-flag" in spec.clean_tokens
        assert "value" in spec.clean_tokens


class TestRealWorldScenarios:
    """Test realistic usage patterns from hcom.py."""

    def test_hcom_background_launch(self):
        """Typical hcom background launch: -p with prompt."""
        spec = resolve_claude_args(["-p", "analyze codebase"], None)
        assert spec.is_background
        assert spec.positional_tokens == ("analyze codebase",)

    def test_hcom_env_fallback(self):
        """CLI args override env string."""
        env_args = "--model sonnet -p"
        cli_args = ["--model", "opus"]

        spec = resolve_claude_args(cli_args, env_args)
        assert spec.source == "cli"
        assert spec.flag_values["--model"] == "opus"
        # Background from env NOT inherited (CLI overrides completely)
        assert not spec.is_background

    def test_max_budget_usd_flag(self):
        """--max-budget-usd flag with value."""
        spec = resolve_claude_args(["-p", "--max-budget-usd", "10.50", "task"], None)
        assert spec.get_flag_value("--max-budget-usd") == "10.50"
        assert spec.positional_tokens == ("task",)
        assert not spec.has_errors()

    def test_no_session_persistence_flag(self):
        """--no-session-persistence boolean flag."""
        spec = resolve_claude_args(["-p", "--no-session-persistence", "task"], None)
        assert spec.has_flag(["--no-session-persistence"])
        assert spec.positional_tokens == ("task",)
        assert not spec.has_errors()

    def test_disable_slash_commands_flag(self):
        """--disable-slash-commands boolean flag."""
        spec = resolve_claude_args(["--disable-slash-commands", "task"], None)
        assert spec.has_flag(["--disable-slash-commands"])
        assert spec.positional_tokens == ("task",)
        assert not spec.has_errors()


class TestBugRegressions:
    """Regression tests for parser bugs fixed in 2025-10-29.

    These tests verify the exact scenarios that were broken before the fixes.
    If any of these tests fail, a critical parser bug has been reintroduced.
    """

    def test_bug_1_hyphen_prefixed_prompt(self):
        """BUG 1: Hyphen-prefixed prompts must be positional."""
        spec = resolve_claude_args(["- check the status"], None)
        assert spec.positional_tokens == ("- check the status",)
        assert not spec.is_background

    def test_bug_1_negative_number_positional(self):
        """BUG 1: Negative numbers must be positional."""
        spec = resolve_claude_args(["-42"], None)
        assert spec.positional_tokens == ("-42",)

    def test_bug_1_dash_word_suffix(self):
        """BUG 1: Words with dash prefix must be positional."""
        spec = resolve_claude_args(["-ish", "-like"], None)
        assert spec.positional_tokens == ("-ish", "-like")

    def test_bug_1_mixed_flag_and_hyphen_prompt(self):
        """BUG 1: Real flag followed by hyphen prompt."""
        spec = resolve_claude_args(["-p", "- do task"], None)
        assert spec.is_background  # -p is real flag
        assert spec.positional_tokens == ("- do task",)  # - do task is prompt

    def test_bug_2_toggle_preserves_flag_like_positional(self):
        """BUG 2: Background toggle must preserve flag-like positionals."""
        spec = resolve_claude_args(["--", "-not-a-flag"], None)
        assert spec.positional_tokens == ("-not-a-flag",)

        # Add background flag - positionals MUST be preserved
        updated = spec.update(background=True)
        assert updated.is_background
        assert updated.positional_tokens == ("-not-a-flag",)

        # Remove background flag - positionals MUST still be preserved
        updated2 = updated.update(background=False)
        assert not updated2.is_background
        assert updated2.positional_tokens == ("-not-a-flag",)

    def test_bug_2_toggle_preserves_multiple_flag_like_positionals(self):
        """BUG 2: Multiple flag-like positionals preserved."""
        spec = resolve_claude_args(["--", "-p", "--verbose", "--model"], None)
        assert spec.positional_tokens == ("-p", "--verbose", "--model")

        updated = spec.update(background=True)
        assert updated.positional_tokens == ("-p", "--verbose", "--model")

    def test_bug_3_has_flag_respects_double_dash(self):
        """BUG 3: has_flag() must stop scanning at -- separator."""
        spec = resolve_claude_args(["--model", "sonnet", "--", "--verbose"], None)
        assert spec.has_flag(names=["--model"])
        assert not spec.has_flag(names=["--verbose"])

    def test_bug_3_has_flag_with_background_and_dash(self):
        """BUG 3: has_flag() with -p before -- and --output-format after."""
        spec = resolve_claude_args(["-p", "--", "--output-format"], None)
        assert not spec.has_flag(names=["--output-format"])
        assert "--output-format" in spec.positional_tokens

    def test_bug_4_resume_without_value(self):
        """BUG 4: --resume must work without value."""
        spec = resolve_claude_args(["--resume"], None)
        assert "--resume" in spec.clean_tokens
        assert not spec.has_errors()

    def test_bug_4_resume_short_without_value(self):
        """BUG 4: -r must work without value."""
        spec = resolve_claude_args(["-r"], None)
        assert "-r" in spec.clean_tokens
        assert not spec.has_errors()

    def test_bug_4_resume_before_other_flag(self):
        """BUG 4: --resume before other flag must not error."""
        spec = resolve_claude_args(["--resume", "--verbose"], None)
        assert not spec.has_errors()
        assert "--resume" in spec.clean_tokens
        assert "--verbose" in spec.clean_tokens

    def test_bug_4_resume_with_value(self):
        """BUG 4: --resume must accept session ID value."""
        spec = resolve_claude_args(["--resume", "abc123"], None)
        assert spec.get_flag_value("--resume") == "abc123"
        assert not spec.has_errors()

    def test_bug_4_resume_short_canonical_lookup(self):
        """BUG 4: -r value must be retrievable via --resume alias."""
        spec = resolve_claude_args(["-r", "abc123"], None)
        assert spec.get_flag_value("--resume") == "abc123"
        assert spec.get_flag_value("-r") == "abc123"

    def test_bug_4_debug_without_value(self):
        """BUG 4: --debug must work without filter."""
        spec = resolve_claude_args(["--debug"], None)
        assert "--debug" in spec.clean_tokens
        assert not spec.has_errors()

    def test_bug_4_debug_short_without_value(self):
        """BUG 4: -d must work without filter."""
        spec = resolve_claude_args(["-d"], None)
        assert "-d" in spec.clean_tokens
        assert not spec.has_errors()

    def test_bug_4_debug_with_filter(self):
        """BUG 4: -d must accept optional filter value."""
        spec = resolve_claude_args(["-d", "api,hooks"], None)
        assert spec.get_flag_value("-d") == "api,hooks"
        assert not spec.has_errors()

    def test_bug_4_debug_short_canonical_lookup(self):
        """BUG 4: -d value must be retrievable via --debug alias."""
        spec = resolve_claude_args(["-d", "api,hooks"], None)
        assert spec.get_flag_value("--debug") == "api,hooks"
        assert spec.get_flag_value("-d") == "api,hooks"

    def test_bug_4_optional_value_consumes_following_token(self):
        """BUG 4: Optional value flags (--debug, --resume) consume next non-flag token.

        Per CLI spec (--debug [filter]), when followed by non-flag token, the token
        is consumed as the optional value. User must use -- separator to prevent this:
        'claude --debug -- "do task"' treats "do task" as prompt, not debug filter.
        """
        spec = resolve_claude_args(["--debug", "do task"], None)
        assert spec.get_flag_value("--debug") == "do task"
        # "do task" consumed as debug filter, not left as positional prompt

    @pytest.mark.parametrize(
        "flag,value",
        [
            ("--fallback-model", "opus"),
            ("--agents", "reviewer"),
            ("--agent", "my-custom-agent"),
            ("--betas", "interleaved-thinking"),
            ("--mcp-config", "config.json"),
            ("--session-id", "abc123"),
            ("--setting-sources", "user"),
            ("--settings", "custom.json"),
            ("--plugin-dir", "/plugins"),
            ("--json-schema", '{"type":"object"}'),
            ("--system-prompt-file", "./prompt.txt"),
            ("--max-budget-usd", "5.00"),
            ("--file", "file_abc:doc.txt"),
        ],
    )
    def test_bug_5_newly_added_flags(self, flag, value):
        """BUG 5: All newly added flags must be recognized."""
        spec = resolve_claude_args([flag, value, "task"], None)
        assert not spec.has_errors()
        assert value in spec.clean_tokens
        assert spec.positional_tokens == ("task",)  # Value consumed, task is positional

    def test_bug_6_get_flag_value_negative_number(self):
        """BUG 6: get_flag_value must retrieve negative numbers."""
        spec = resolve_claude_args(["--max-turns", "-1"], None)
        assert spec.get_flag_value("--max-turns") == "-1"
        assert not spec.has_errors()

    def test_bug_6_get_flag_value_hyphen_string(self):
        """BUG 6: get_flag_value must retrieve hyphen-prefixed strings."""
        spec = resolve_claude_args(["--model", "-experimental"], None)
        assert spec.get_flag_value("--model") == "-experimental"
        assert not spec.has_errors()

    def test_bug_6_get_flag_value_dash_option(self):
        """BUG 6: Hyphen-prefixed option strings work."""
        spec = resolve_claude_args(["--output-format", "-custom"], None)
        assert spec.get_flag_value("--output-format") == "-custom"


class TestValidateConflicts:
    """Test conflict detection in ClaudeArgsSpec."""

    def test_no_conflicts_empty_spec(self):
        """Empty spec has no conflicts."""
        spec = resolve_claude_args([], None)
        warnings = validate_conflicts(spec)
        assert warnings == []

    def test_no_conflicts_simple_args(self):
        """Simple args without conflicts."""
        spec = resolve_claude_args(["--model", "sonnet", "hello"], None)
        warnings = validate_conflicts(spec)
        assert warnings == []

    def test_system_and_append_no_warning(self):
        """Standard pattern: --system-prompt + --append-system-prompt = no warning."""
        spec = resolve_claude_args(["--system-prompt", "first", "--append-system-prompt", "second"], None)
        warnings = validate_conflicts(spec)
        assert len(warnings) == 0  # Standard pattern, no warning

    def test_three_system_prompts_warning(self):
        """Three system prompts still generate one warning."""
        spec = resolve_claude_args(
            ["--system-prompt", "a", "--append-system-prompt", "b", "--system-prompt", "c"], None
        )
        warnings = validate_conflicts(spec)
        assert len(warnings) == 1
        assert "Multiple system prompts" in warnings[0]

    def test_single_system_prompt_no_warning(self):
        """Single system prompt produces no conflict."""
        spec = resolve_claude_args(["--system-prompt", "only one"], None)
        warnings = validate_conflicts(spec)
        assert warnings == []

    def test_single_append_no_warning(self):
        """Single append prompt produces no conflict."""
        spec = resolve_claude_args(["--append-system-prompt", "only one"], None)
        warnings = validate_conflicts(spec)
        assert warnings == []


class TestAddBackgroundDefaults:
    """Test add_background_defaults() helper function."""

    def test_non_background_unchanged(self):
        """Non-background specs are not modified."""
        spec = resolve_claude_args(["hello world"], None)
        updated = add_background_defaults(spec)
        assert updated.clean_tokens == spec.clean_tokens
        assert not updated.has_flag(["--output-format"])
        assert not updated.has_flag(["--verbose"])

    def test_adds_output_format_stream_json(self):
        """Background mode adds --output-format stream-json if missing."""
        spec = resolve_claude_args(["-p", "task"], None)
        updated = add_background_defaults(spec)
        assert updated.has_flag(["--output-format"])
        assert updated.get_flag_value("--output-format") == "stream-json"

    def test_adds_verbose_flag(self):
        """Background mode adds --verbose if missing."""
        spec = resolve_claude_args(["-p", "task"], None)
        updated = add_background_defaults(spec)
        assert updated.has_flag(["--verbose"])

    def test_preserves_existing_output_format(self):
        """Existing --output-format not overridden."""
        spec = resolve_claude_args(["-p", "--output-format", "json", "task"], None)
        updated = add_background_defaults(spec)
        assert updated.get_flag_value("--output-format") == "json"

    def test_preserves_existing_verbose(self):
        """Doesn't duplicate --verbose if already present."""
        spec = resolve_claude_args(["-p", "--verbose", "task"], None)
        updated = add_background_defaults(spec)
        # Should not add second --verbose
        verbose_count = sum(1 for token in updated.clean_tokens if token == "--verbose")
        assert verbose_count == 1

    def test_adds_both_when_missing(self):
        """Adds both flags when background mode and both missing."""
        spec = resolve_claude_args(["--print", "task"], None)
        updated = add_background_defaults(spec)
        assert updated.has_flag(["--output-format"])
        assert updated.get_flag_value("--output-format") == "stream-json"
        assert updated.has_flag(["--verbose"])

    def test_preserves_positional_after_double_dash(self):
        """Positionals after -- are preserved."""
        spec = resolve_claude_args(["-p", "--", "not-a-flag"], None)
        updated = add_background_defaults(spec)
        assert "not-a-flag" in updated.positional_tokens
        # Defaults inserted before --
        assert "--output-format" in updated.clean_tokens
        assert "--verbose" in updated.clean_tokens

    def test_preserves_system_prompts(self):
        """System prompts are preserved when adding defaults."""
        spec = resolve_claude_args(["-p", "--system-prompt", "be brief", "task"], None)
        updated = add_background_defaults(spec)
        assert updated.get_flag_value("--system-prompt") == "be brief"
        assert updated.has_flag(["--output-format"])


class TestBoundaryFlagInteractions:
    """Test boundary cases and complex flag interactions."""

    def test_negative_number_value_then_background_flag(self):
        """Negative number consumed as value, not confused with flag."""
        spec = resolve_claude_args(["--max-turns", "-1", "-p"], None)
        assert spec.get_flag_value("--max-turns") == "-1"
        assert spec.is_background

    def test_negative_value_then_prompt(self):
        """Negative value followed by prompt."""
        spec = resolve_claude_args(["--max-turns", "-1", "hello"], None)
        assert spec.get_flag_value("--max-turns") == "-1"
        assert spec.positional_tokens == ("hello",)

    def test_duplicate_model_flag_space_syntax(self):
        """Duplicate --model flags with space syntax."""
        spec = resolve_claude_args(["--model", "sonnet", "--model", "opus"], None)
        # Both values appear in clean_tokens
        assert spec.clean_tokens.count("--model") == 2
        assert "sonnet" in spec.clean_tokens
        assert "opus" in spec.clean_tokens

    def test_duplicate_model_flag_mixed_syntax(self):
        """Duplicate --model with mixed = and space syntax."""
        spec = resolve_claude_args(["--model=sonnet", "--model", "opus"], None)
        assert "--model=sonnet" in spec.clean_tokens
        assert "--model" in spec.clean_tokens
        assert "opus" in spec.clean_tokens

    def test_multiple_background_flags(self):
        """Multiple -p flags preserved."""
        spec = resolve_claude_args(["-p", "-p", "task"], None)
        assert spec.is_background
        assert spec.clean_tokens.count("-p") == 2

    def test_background_short_and_long(self):
        """Both -p and --print accepted."""
        spec = resolve_claude_args(["-p", "--print", "task"], None)
        assert spec.is_background
        assert "-p" in spec.clean_tokens
        assert "--print" in spec.clean_tokens

    def test_empty_flag_value(self):
        """Empty string as flag value."""
        spec = resolve_claude_args(["--model", "", "task"], None)
        assert spec.get_flag_value("--model") == ""
        assert spec.positional_tokens == ("task",)

    def test_very_long_flag_value(self):
        """Very long flag value (1MB)."""
        long_value = "x" * 1_000_000
        spec = resolve_claude_args(["--system-prompt", long_value], None)
        result = spec.get_flag_value("--system-prompt")
        assert result is not None
        assert result == long_value
        assert len(result) == 1_000_000

    def test_special_chars_in_system_prompt(self):
        """Special characters in system prompt value."""
        value = 'say "hello"\nworld\ttab'
        spec = resolve_claude_args(["--system-prompt", value], None)
        result = spec.get_flag_value("--system-prompt")
        assert result == value
        assert '"hello"' in result
        assert "\n" in result
        assert "\t" in result

    def test_flag_after_double_dash_not_recognized(self):
        """Flags after -- treated as positional."""
        spec = resolve_claude_args(["--model", "sonnet", "--", "--verbose"], None)
        assert spec.get_flag_value("--model") == "sonnet"
        assert not spec.has_flag(["--verbose"])
        assert "--verbose" in spec.positional_tokens

    def test_resume_equals_then_space_syntax(self):
        """Mixed --resume=value and --resume value forms."""
        spec = resolve_claude_args(["--resume=abc", "--resume", "xyz"], None)
        assert "--resume=abc" in spec.clean_tokens
        assert "--resume" in spec.clean_tokens
        assert "xyz" in spec.clean_tokens


@pytest.mark.slow
class TestPropertyBased:
    """Property-based tests using Hypothesis for edge case discovery.

    These tests generate random inputs to verify parser invariants hold
    across a wide range of inputs, finding edge cases manual tests miss.
    """

    # ===== Test Strategies =====

    @staticmethod
    def boolean_flags():
        """Generate known boolean flags."""
        return st.sampled_from(
            [
                "--verbose",
                "-v",
                "--continue",
                "-c",
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
                "--replay-user-messages",
                "--mcp-debug",
                "--fork-session",
                "--ide",
                "--strict-mcp-config",
                "--include-partial-messages",
                "--no-session-persistence",
                "--disable-slash-commands",
                "-h",
                "--help",
            ]
        )

    @staticmethod
    def value_flags():
        """Generate known value flags with realistic values."""
        return st.one_of(
            st.tuples(st.just("--model"), st.sampled_from(["sonnet", "opus", "haiku", "claude-sonnet-4-5-20250929"])),
            st.tuples(st.just("--fallback-model"), st.sampled_from(["opus", "sonnet"])),
            st.tuples(st.just("--output-format"), st.sampled_from(["text", "json", "stream-json"])),
            st.tuples(st.just("--input-format"), st.sampled_from(["text", "stream-json"])),
            st.tuples(
                st.just("--permission-mode"),
                st.sampled_from(["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]),
            ),
            st.tuples(st.just("--max-turns"), st.integers(min_value=1, max_value=100).map(str)),
            st.tuples(
                st.just("--max-budget-usd"), st.floats(min_value=0.01, max_value=100.0).map(lambda f: f"{f:.2f}")
            ),
            st.tuples(st.just("--allowedTools"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--allowed-tools"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--disallowedTools"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--tools"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--add-dir"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--mcp-config"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--session-id"), st.uuids().map(str)),
            st.tuples(st.just("--settings"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--plugin-dir"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--json-schema"), st.text(min_size=1, max_size=50)),
            st.tuples(st.just("--system-prompt-file"), st.text(min_size=1, max_size=20)),
        )

    @staticmethod
    def optional_value_flags():
        """Generate optional value flags (with or without values)."""
        return st.one_of(
            st.just(("--resume",)),
            st.tuples(st.just("--resume"), st.text(min_size=1, max_size=20)),
            st.just(("-r",)),
            st.tuples(st.just("-r"), st.text(min_size=1, max_size=20)),
            st.just(("--debug",)),
            st.tuples(st.just("--debug"), st.sampled_from(["api", "hooks", "api,hooks"])),
            st.just(("-d",)),
            st.tuples(st.just("-d"), st.sampled_from(["api", "hooks"])),
        )

    @staticmethod
    def background_flags():
        """Generate background mode flags."""
        return st.sampled_from(["-p", "--print"])

    @staticmethod
    def system_prompt_args():
        """Generate system prompt arguments."""
        return st.one_of(
            st.tuples(st.just("--system-prompt"), st.text(min_size=1, max_size=50)),
            st.tuples(st.just("--append-system-prompt"), st.text(min_size=1, max_size=50)),
        )

    @staticmethod
    def positional_args():
        """Generate realistic positional arguments (prompts)."""
        return st.one_of(
            st.text(min_size=1, max_size=100),
            st.sampled_from(
                [
                    "explain this code",
                    "fix the bug",
                    "write tests",
                    "- check status",
                    "-42",
                    'task with "quotes"',
                ]
            ),
        )

    @staticmethod
    def exotic_tokens():
        """Generate exotic/edge-case tokens for broader coverage."""
        return st.sampled_from(
            [
                # Malformed equals syntax
                "--model=",
                "--output-format=",
                # Unknown flags
                "--weird-unknown-flag",
                "--custom-option",
                # Unicode in flags (unlikely but possible)
                "--модель",
                "--🚀",
                # Double dash variations
                "---triple-dash",
                # Empty/whitespace
                "",
                "   ",
                # Very long flag names
                "--" + "x" * 100,
            ]
        )

    @staticmethod
    def valid_token_sequence():
        """Generate realistic token sequences."""
        return st.lists(
            st.one_of(
                TestPropertyBased.boolean_flags(),
                TestPropertyBased.value_flags().map(lambda t: list(t)),
                TestPropertyBased.optional_value_flags().map(lambda t: list(t)),
                TestPropertyBased.background_flags(),
                TestPropertyBased.positional_args(),
                TestPropertyBased.exotic_tokens(),  # Add exotic tokens occasionally
            ),
            min_size=0,
            max_size=15,
        ).map(lambda items: [token for item in items for token in (item if isinstance(item, list) else [item])])

    # ===== Property Tests =====

    @given(st.lists(st.text(max_size=200), max_size=30))
    @settings(max_examples=200, deadline=None)
    def test_parser_never_crashes(self, args):
        """PROPERTY: Parser handles any input without crashing.

        Critical safety property - parser must be robust against malformed input.
        """
        try:
            spec = resolve_claude_args(args, None)
            assert isinstance(spec, ClaudeArgsSpec)
            # If errors exist, they should be strings
            if spec.errors:
                assert all(isinstance(e, str) for e in spec.errors)
        except Exception as e:
            pytest.fail(f"Parser crashed on input {args!r}: {e}")

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_roundtrip_stability(self, valid_token_sequence):
        """PROPERTY: parse → to_env_string → reparse produces stable result.

        Ensures serialization is lossless for valid inputs.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec1 = resolve_claude_args(data, None)

        # Skip if initial parse had errors
        assume(not spec1.has_errors())

        # Serialize to env string
        env_str = spec1.to_env_string()

        # Reparse
        spec2 = resolve_claude_args(None, env_str)

        # Should not introduce new errors
        assert not spec2.has_errors(), f"Roundtrip introduced errors: {spec2.errors}"

        # Clean tokens should match (system prompts handled separately)
        assert spec2.clean_tokens == spec1.clean_tokens, (
            f"Roundtrip changed tokens: {spec1.clean_tokens} → {spec2.clean_tokens}"
        )

        # Background flag should be preserved
        assert spec2.is_background == spec1.is_background

        # Positional tokens should be preserved
        assert spec2.positional_tokens == spec1.positional_tokens

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_update_preserves_unrelated_fields(self, valid_token_sequence):
        """PROPERTY: update() operations don't lose unrelated data.

        Critical for maintaining parser state during transformations.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_claude_args(data, None)
        assume(not spec.has_errors())

        # Test background toggle
        if not spec.is_background:
            updated = spec.update(background=True)

            # Model flag should survive
            if spec.has_flag(["--model"]):
                assert updated.has_flag(["--model"]), "Background toggle lost --model flag"
                assert updated.get_flag_value("--model") == spec.get_flag_value("--model")

            # Verbose flag should survive
            if spec.has_flag(["--verbose"]):
                assert updated.has_flag(["--verbose"]), "Background toggle lost --verbose flag"

            # Positionals should survive (critical regression test for bug 2)
            assert updated.positional_tokens == spec.positional_tokens, "Background toggle changed positionals"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_flag_detection_consistency(self, valid_token_sequence):
        """PROPERTY: has_flag() and get_flag_value() agree.

        If has_flag returns True, get_flag_value should return non-None or
        the flag should be in optional/boolean category.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_claude_args(data, None)
        assume(not spec.has_errors())

        # Test known value flags
        for flag in ["--model", "--output-format", "--max-turns", "--permission-mode"]:
            if spec.has_flag([flag]):
                value = spec.get_flag_value(flag)
                # Value flags should have values (or error would be present)
                if not spec.has_errors():
                    assert value is not None or flag in ["--resume", "--debug"], (
                        f"has_flag({flag}) = True but get_flag_value({flag}) = None"
                    )

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_double_dash_boundary_respected(self, valid_token_sequence):
        """PROPERTY: -- separator prevents flag interpretation after it.

        Critical for allowing flag-like strings as positional arguments.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        # Insert -- and flag-like positional
        modified = list(data) + ["--", "--not-a-flag", "-p", "--verbose"]

        spec = resolve_claude_args(modified, None)

        # Flags after -- should be positional
        assert "--not-a-flag" in spec.positional_tokens
        assert "-p" in spec.positional_tokens
        assert "--verbose" in spec.positional_tokens

        # has_flag should not detect them
        assert not spec.has_flag(["--not-a-flag"])

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_prompt_update_preserves_flags(self, valid_token_sequence):
        """PROPERTY: Updating prompt doesn't affect flags.

        Ensures update(prompt=...) only changes positional, not flags.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_claude_args(data, None)
        assume(not spec.has_errors())

        # Skip if optional value flags present - they can consume the prompt
        # (This is correct parser behavior: --resume 'text' treats 'text' as resume value)
        assume(not spec.has_flag(["--resume", "-r", "--debug", "-d"]))

        # Update prompt
        updated = spec.update(prompt="new prompt")

        # Flags should be unchanged
        assert updated.is_background == spec.is_background

        if spec.has_flag(["--model"]):
            assert updated.has_flag(["--model"])
            assert updated.get_flag_value("--model") == spec.get_flag_value("--model")

        if spec.has_flag(["--verbose"]):
            assert updated.has_flag(["--verbose"])

        # Prompt should be updated
        assert "new prompt" in updated.positional_tokens

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_system_prompt_in_clean_tokens(self, valid_token_sequence):
        """PROPERTY: System prompts always in clean_tokens.

        Ensures system prompts are treated like normal value flags.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_claude_args(data, None)

        # Check if system prompts are present and verify they're in clean_tokens
        for i in range(len(data)):
            token_lower = data[i].lower()
            if token_lower in ("--system-prompt", "--append-system-prompt"):
                # System prompt flags SHOULD be in clean_tokens
                assert any(t.lower() == token_lower for t in spec.clean_tokens), (
                    f"System prompt flag {data[i]} not in clean_tokens"
                )

    @given(
        base_args=st.lists(st.text(max_size=50), min_size=0, max_size=10),
        new_background=st.booleans(),
    )
    @settings(max_examples=100, deadline=None)
    def test_background_toggle_idempotent(self, base_args, new_background):
        """PROPERTY: Toggling background twice is idempotent.

        update(background=X).update(background=X) == update(background=X)
        """
        spec = resolve_claude_args(base_args, None)
        assume(not spec.has_errors())

        once = spec.update(background=new_background)
        twice = once.update(background=new_background)

        assert once.is_background == twice.is_background
        assert once.clean_tokens == twice.clean_tokens
        assert once.positional_tokens == twice.positional_tokens

    @given(st.lists(st.text(max_size=50), min_size=1, max_size=20))
    @settings(max_examples=150, deadline=None)
    def test_error_messages_reference_problematic_flag(self, args):
        """PROPERTY: Every error message contains the problematic flag name.

        Ensures error messages are actionable and reference what went wrong.
        """
        spec = resolve_claude_args(args, None)

        if spec.has_errors():
            # For each error, try to extract flag name and verify it's mentioned
            for error in spec.errors:
                # Error should be non-empty and informative
                assert len(error) > 10, f"Error message too short: {error!r}"

                # Common error patterns should reference the flag
                if "requires a value" in error:
                    # Should contain a flag name (starts with -)
                    assert any(token.startswith("-") for token in error.split()), (
                        f"Error about missing value doesn't reference flag: {error!r}"
                    )

                if "invalid" in error.lower():
                    # Should provide context about what's invalid
                    assert len(error) > 20, f"Invalid error lacks detail: {error!r}"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_exotic_tokens_handled_gracefully(self, valid_token_sequence):
        """PROPERTY: Exotic tokens don't cause crashes or corrupt state.

        Tests malformed equals, unicode flags, unknown options, etc.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        # Add some exotic tokens directly
        exotic = valid_token_sequence.draw(st.lists(TestPropertyBased.exotic_tokens(), min_size=0, max_size=3))
        combined = list(data) + exotic

        # Should not crash
        spec = resolve_claude_args(combined, None)
        assert isinstance(spec, ClaudeArgsSpec)

        # Unknown flags should be preserved in clean_tokens
        for token in exotic:
            if token and token.startswith("--") and token not in ["--model=", "--output-format="]:
                # Unknown flags should appear somewhere (clean_tokens or positional)
                assert token in spec.clean_tokens or token in spec.positional_tokens, f"Exotic token {token!r} was lost"


class TestMergeClaudeArgs:
    """Test merge_claude_args functionality."""

    def test_merge_cli_flags_only_keeps_env_prompt(self):
        """CLI flags without prompt should KEEP env prompt."""
        env = resolve_claude_args(None, "'say hi' --model sonnet")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("say hi",)  # Kept from env
        assert merged.get_flag_value("--model") == "opus"  # CLI wins

    def test_merge_cli_prompt_replaces_env_prompt(self):
        """CLI prompt should REPLACE env prompt entirely."""
        env = resolve_claude_args(None, "'say hi' --model sonnet")
        cli = resolve_claude_args(["new task", "--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("new task",)  # CLI replaced
        assert merged.get_flag_value("--model") == "opus"

    def test_merge_empty_cli_prompt_deletes_env_prompt(self):
        """Empty string in CLI should DELETE env prompt."""
        env = resolve_claude_args(None, "'say hi' --model sonnet")
        cli = resolve_claude_args(["", "--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ()  # Deleted
        assert merged.get_flag_value("--model") == "opus"

    def test_merge_multiple_env_positionals_all_replaced(self):
        """CLI positional replaces ALL env positionals (after -- too)."""
        env = resolve_claude_args(None, "'prompt' -- --arg1 --arg2")
        cli = resolve_claude_args(["new prompt"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("new prompt",)  # ALL env positionals gone

    def test_merge_no_cli_args_uses_env(self):
        """No CLI args should use env entirely."""
        env = resolve_claude_args(None, "--model sonnet --verbose 'do task'")
        cli = resolve_claude_args(None, None)
        merged = merge_claude_args(env, cli)

        assert merged.get_flag_value("--model") == "sonnet"
        assert merged.has_flag(["--verbose"])
        assert merged.positional_tokens == ("do task",)

    def test_merge_cli_overrides_env_flag(self):
        """CLI flag should override env flag."""
        env = resolve_claude_args(None, "--model sonnet --output-format text")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.get_flag_value("--model") == "opus"  # CLI wins
        assert merged.get_flag_value("--output-format") == "text"  # Env preserved

    def test_merge_cli_adds_new_flags(self):
        """CLI can add new flags not in env."""
        env = resolve_claude_args(None, "--model sonnet")
        cli = resolve_claude_args(["--verbose", "--max-turns", "10"], None)
        merged = merge_claude_args(env, cli)

        assert merged.get_flag_value("--model") == "sonnet"  # Env preserved
        assert merged.has_flag(["--verbose"])  # CLI added
        assert merged.get_flag_value("--max-turns") == "10"  # CLI added

    def test_merge_boolean_flag_deduplication(self):
        """Duplicate boolean flags should be deduped."""
        env = resolve_claude_args(None, "--verbose")
        cli = resolve_claude_args(["--verbose", "--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        verbose_count = merged.clean_tokens.count("--verbose")
        assert verbose_count == 1  # Deduped

    def test_merge_background_mode_inherited(self):
        """Background mode from env should be inherited if CLI doesn't specify."""
        env = resolve_claude_args(None, "-p --model sonnet")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.is_background  # Inherited from env
        assert merged.get_flag_value("--model") == "opus"  # CLI wins

    def test_merge_system_prompts_cli_wins(self):
        """CLI system prompt should override env system prompt."""
        env = resolve_claude_args(None, "--system-prompt 'env prompt'")
        cli = resolve_claude_args(["--system-prompt", "cli prompt"], None)
        merged = merge_claude_args(env, cli)

        # CLI system prompt wins via standard flag override
        assert merged.get_flag_value("--system-prompt") == "cli prompt"
        # Verify ENV system prompt not present
        system_count = sum(1 for t in merged.clean_tokens if t == "--system-prompt")
        assert system_count == 1

    def test_merge_preserves_env_when_cli_empty(self):
        """Empty CLI should preserve all env args."""
        env = resolve_claude_args(None, "'task' --model sonnet --verbose -p")
        cli = resolve_claude_args([], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("task",)
        assert merged.get_flag_value("--model") == "sonnet"
        assert merged.has_flag(["--verbose"])
        assert merged.is_background

    def test_merge_space_separated_flag_value(self):
        """Merge handles space-separated flag values correctly."""
        env = resolve_claude_args(None, "--model sonnet --allowedTools Bash")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.get_flag_value("--model") == "opus"  # CLI wins
        assert merged.get_flag_value("--allowedTools") == "Bash"  # Env preserved

    def test_merge_equals_syntax_flag(self):
        """Merge handles equals syntax flags correctly."""
        env = resolve_claude_args(None, "--model=sonnet --verbose")
        cli = resolve_claude_args(["--model=opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.get_flag_value("--model") == "opus"  # CLI wins
        assert merged.has_flag(["--verbose"])  # Env preserved

    def test_merge_unknown_flags_passthrough(self):
        """Unknown flags from env should be preserved if not overridden."""
        env = resolve_claude_args(None, "--new-flag value --model sonnet")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        # Unknown flag preserved
        assert "--new-flag" in merged.clean_tokens
        # Note: "value" becomes a positional since --new-flag is unknown
        # So it gets inserted at positionals location, not directly after the flag
        assert "value" in merged.positional_tokens
        assert "--model" in merged.clean_tokens
        assert "opus" in merged.clean_tokens

    def test_merge_multiple_value_flags(self):
        """Multiple occurrences of same flag - CLI replaces all env instances."""
        env = resolve_claude_args(None, "--model sonnet --model opus")
        cli = resolve_claude_args(["--model", "haiku"], None)
        merged = merge_claude_args(env, cli)

        # CLI model appears once
        model_count = sum(1 for t in merged.clean_tokens if t.startswith("--model"))
        assert model_count == 1
        assert merged.get_flag_value("--model") == "haiku"

    def test_merge_positional_with_flags(self):
        """Positional + flags merge correctly."""
        env = resolve_claude_args(None, "'env task' --model sonnet")
        cli = resolve_claude_args(["cli task", "--verbose"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("cli task",)  # CLI positional
        assert merged.get_flag_value("--model") == "sonnet"  # Env flag preserved
        assert merged.has_flag(["--verbose"])  # CLI flag added

    def test_merge_no_positionals_both(self):
        """No positionals in env or CLI - just flags merge."""
        env = resolve_claude_args(None, "--model sonnet --verbose")
        cli = resolve_claude_args(["--model", "opus"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ()
        assert merged.get_flag_value("--model") == "opus"
        assert merged.has_flag(["--verbose"])

    def test_merge_complex_scenario(self):
        """Complex real-world scenario."""
        env = resolve_claude_args(None, "'default task' --model sonnet --verbose -p --max-turns 5")
        cli = resolve_claude_args(["new task", "--model", "opus", "--allowedTools", "Bash"], None)
        merged = merge_claude_args(env, cli)

        assert merged.positional_tokens == ("new task",)  # CLI prompt
        assert merged.get_flag_value("--model") == "opus"  # CLI wins
        assert merged.has_flag(["--verbose"])  # Env preserved
        assert merged.is_background  # Env preserved
        assert merged.get_flag_value("--max-turns") == "5"  # Env preserved
        assert merged.get_flag_value("--allowedTools") == "Bash"  # CLI added


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
