#!/usr/bin/env python3
"""Unit tests for gemini_args - Gemini CLI argument parsing and composition."""

# run 'gemini --help' to see all current up-to-date possible flags and options
# docs/external/gemini-cli/ relevant files: commands.md headless.md configuration.md hooks.md

import pytest
from hypothesis import given, strategies as st, assume, settings
from hcom.tools.gemini.args import (
    resolve_gemini_args,
    merge_gemini_args,
    validate_conflicts as validate_gemini_conflicts,
    GeminiArgsSpec,
)


class TestBasicParsing:
    """Test fundamental parsing scenarios."""

    def test_empty_args(self):
        """Empty input produces empty spec."""
        spec = resolve_gemini_args(None, None)
        assert spec.source == "none"
        assert spec.raw_tokens == ()
        assert spec.clean_tokens == ()
        assert spec.positional_tokens == ()
        assert not spec.is_headless
        assert not spec.is_json
        assert not spec.is_yolo
        assert spec.output_format == "text"
        assert spec.approval_mode == "default"
        assert not spec.errors

    def test_simple_positional(self):
        """Single positional argument (query) parsed correctly."""
        spec = resolve_gemini_args(["hello world"], None)
        assert spec.positional_tokens == ("hello world",)
        assert spec.clean_tokens == ("hello world",)
        assert spec.positional_indexes == (0,)
        # Positional query = headless mode
        assert spec.is_headless

    def test_yolo_flag_short(self):
        """Yolo -y flag detected."""
        spec = resolve_gemini_args(["-y"], None)
        assert spec.is_yolo
        assert spec.approval_mode == "yolo"
        assert spec.clean_tokens == ("-y",)

    def test_yolo_flag_long(self):
        """Yolo --yolo flag detected."""
        spec = resolve_gemini_args(["--yolo"], None)
        assert spec.is_yolo
        assert spec.approval_mode == "yolo"
        assert spec.clean_tokens == ("--yolo",)

    def test_model_flag_separate(self):
        """Model flag with separate value."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash"], None)
        assert spec.flag_values["--model"] == "gemini-2.5-flash"
        assert spec.clean_tokens == ("--model", "gemini-2.5-flash")

    def test_model_flag_equals(self):
        """Model flag with equals syntax."""
        spec = resolve_gemini_args(["--model=gemini-2.5-pro"], None)
        assert spec.flag_values["--model"] == "gemini-2.5-pro"
        assert spec.clean_tokens == ("--model=gemini-2.5-pro",)

    def test_unknown_option_has_suggestion(self):
        spec = resolve_gemini_args(["--moddel", "gemini-2.5-flash"], None)
        assert spec.has_errors()
        assert any("unknown option" in err.lower() for err in spec.errors)
        assert any("--model" in err for err in spec.errors)


class TestHeadlessMode:
    """Test headless mode detection."""

    def test_positional_query_is_headless(self):
        """Positional query makes it headless mode."""
        spec = resolve_gemini_args(["explain this code"], None)
        assert spec.is_headless
        assert spec.positional_tokens == ("explain this code",)

    def test_prompt_flag_is_headless(self):
        """--prompt flag makes it headless mode (deprecated but supported)."""
        spec = resolve_gemini_args(["-p", "do task"], None)
        assert spec.is_headless

    def test_prompt_long_flag_is_headless(self):
        """--prompt long flag makes it headless."""
        spec = resolve_gemini_args(["--prompt", "do task"], None)
        assert spec.is_headless

    def test_no_query_not_headless(self):
        """No positional query = interactive mode."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash"], None)
        assert not spec.is_headless

    def test_prompt_interactive_not_headless(self):
        """--prompt-interactive is NOT headless (starts interactive with prompt)."""
        spec = resolve_gemini_args(["-i", "initial prompt"], None)
        # -i/--prompt-interactive is still interactive, not headless
        assert not spec.is_headless

    def test_prompt_interactive_long_form_not_headless(self):
        """Long form --prompt-interactive is NOT headless."""
        spec = resolve_gemini_args(["--prompt-interactive", "initial prompt"], None)
        assert not spec.is_headless

    def test_prompt_interactive_equals_syntax_not_headless(self):
        """--prompt-interactive=value is NOT headless."""
        spec = resolve_gemini_args(["--prompt-interactive=hello world"], None)
        assert not spec.is_headless

    def test_prompt_interactive_short_equals_syntax_not_headless(self):
        """-i=value is NOT headless."""
        spec = resolve_gemini_args(["-i=hello world"], None)
        assert not spec.is_headless

    def test_prompt_interactive_with_positional_not_headless(self):
        """Positional after --prompt-interactive is NOT headless (still interactive)."""
        # This is an edge case - the first value is consumed by -i
        # but if there's another positional, it should still be interactive
        spec = resolve_gemini_args(["-i", "initial", "extra positional"], None)
        assert not spec.is_headless


class TestOutputFormats:
    """Test output format handling."""

    def test_output_format_text(self):
        """--output-format text is default."""
        spec = resolve_gemini_args([], None)
        assert spec.output_format == "text"
        assert not spec.is_json

    def test_output_format_json(self):
        """--output-format json detected."""
        spec = resolve_gemini_args(["--output-format", "json", "query"], None)
        assert spec.output_format == "json"
        assert spec.is_json

    def test_output_format_stream_json(self):
        """--output-format stream-json detected."""
        spec = resolve_gemini_args(["--output-format", "stream-json", "query"], None)
        assert spec.output_format == "stream-json"
        assert spec.is_json

    def test_output_format_short_flag(self):
        """-o short flag works."""
        spec = resolve_gemini_args(["-o", "json", "query"], None)
        assert spec.output_format == "json"
        assert spec.is_json

    def test_output_format_equals_syntax(self):
        """--output-format= syntax works."""
        spec = resolve_gemini_args(["--output-format=stream-json", "query"], None)
        assert spec.output_format == "stream-json"
        assert spec.is_json


class TestApprovalModes:
    """Test approval mode handling."""

    def test_approval_mode_default(self):
        """Default approval mode."""
        spec = resolve_gemini_args([], None)
        assert spec.approval_mode == "default"
        assert not spec.is_yolo

    def test_approval_mode_auto_edit(self):
        """--approval-mode auto_edit."""
        spec = resolve_gemini_args(["--approval-mode", "auto_edit"], None)
        assert spec.approval_mode == "auto_edit"
        assert not spec.is_yolo

    def test_approval_mode_yolo(self):
        """--approval-mode yolo is equivalent to --yolo."""
        spec = resolve_gemini_args(["--approval-mode", "yolo"], None)
        assert spec.approval_mode == "yolo"
        assert spec.is_yolo

    def test_approval_mode_equals_syntax(self):
        """--approval-mode= syntax."""
        spec = resolve_gemini_args(["--approval-mode=auto_edit"], None)
        assert spec.approval_mode == "auto_edit"

    def test_yolo_flag_sets_approval_mode(self):
        """--yolo flag sets approval_mode to yolo."""
        spec = resolve_gemini_args(["--yolo"], None)
        assert spec.approval_mode == "yolo"
        assert spec.is_yolo


class TestSubcommands:
    """Test subcommand parsing."""

    def test_mcp_subcommand(self):
        """mcp subcommand detected."""
        spec = resolve_gemini_args(["mcp", "list"], None)
        assert spec.subcommand == "mcp"
        assert spec.positional_tokens == ("list",)

    def test_extensions_subcommand(self):
        """extensions subcommand detected."""
        spec = resolve_gemini_args(["extensions", "list"], None)
        assert spec.subcommand == "extensions"

    def test_extension_alias(self):
        """extension alias normalized to extensions."""
        spec = resolve_gemini_args(["extension", "list"], None)
        assert spec.subcommand == "extensions"

    def test_hooks_subcommand(self):
        """hooks subcommand detected."""
        spec = resolve_gemini_args(["hooks", "panel"], None)
        assert spec.subcommand == "hooks"

    def test_hook_alias(self):
        """hook alias normalized to hooks."""
        spec = resolve_gemini_args(["hook", "enable"], None)
        assert spec.subcommand == "hooks"

    def test_no_subcommand(self):
        """No subcommand when starting with query."""
        spec = resolve_gemini_args(["explain this code"], None)
        assert spec.subcommand is None


class TestFlagValues:
    """Test extraction of flag values."""

    def test_include_directories_separate(self):
        """--include-directories with separate value."""
        spec = resolve_gemini_args(["--include-directories", "/path/to/dir"], None)
        assert spec.flag_values["--include-directories"] == ["/path/to/dir"]

    def test_include_directories_multiple(self):
        """--include-directories can be repeated."""
        spec = resolve_gemini_args([
            "--include-directories", "/dir1",
            "--include-directories", "/dir2"
        ], None)
        assert spec.flag_values["--include-directories"] == ["/dir1", "/dir2"]

    def test_extensions_multiple(self):
        """--extensions/-e can be repeated."""
        spec = resolve_gemini_args(["-e", "ext1", "-e", "ext2"], None)
        assert spec.flag_values["-e"] == ["ext1", "ext2"]

    def test_allowed_tools(self):
        """--allowed-tools flag."""
        spec = resolve_gemini_args(["--allowed-tools", "read_file,write_file"], None)
        assert spec.flag_values["--allowed-tools"] == ["read_file,write_file"]

    def test_has_flag_by_name(self):
        """has_flag() detects flags by name."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash"], None)
        assert spec.has_flag(names=["--model"])
        assert not spec.has_flag(names=["--debug"])

    def test_has_flag_by_prefix(self):
        """has_flag() detects flags by prefix."""
        spec = resolve_gemini_args(["--model=gemini-2.5-flash"], None)
        assert spec.has_flag(prefixes=["--model="])

    def test_get_flag_value(self):
        """get_flag_value() retrieves values."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-pro"], None)
        assert spec.get_flag_value("--model") == "gemini-2.5-pro"

    def test_get_flag_value_alias(self):
        """get_flag_value() handles aliases."""
        spec = resolve_gemini_args(["-m", "gemini-2.5-flash"], None)
        assert spec.get_flag_value("--model") == "gemini-2.5-flash"
        assert spec.get_flag_value("-m") == "gemini-2.5-flash"


class TestBooleanFlags:
    """Test boolean flag handling."""

    @pytest.mark.parametrize("flag", [
        "--debug",
        "-d",
        "--sandbox",
        "-s",
        "--yolo",
        "-y",
        "--list-extensions",
        "-l",
        "--list-sessions",
        "--screen-reader",
        "--version",
        "-v",
        "--help",
        "-h",
        "--experimental-acp",
    ])
    def test_boolean_flag_with_query(self, flag):
        """Boolean flags should not consume following query."""
        spec = resolve_gemini_args([flag, "do task"], None)
        assert flag in spec.clean_tokens
        assert spec.positional_tokens == ("do task",)
        assert not spec.has_errors()

    @pytest.mark.parametrize("flag", [
        "--debug",
        "-d",
        "--sandbox",
        "-s",
        "--yolo",
        "-y",
        "--list-extensions",
        "--list-sessions",
        "--screen-reader",
        "--experimental-acp",
    ])
    def test_boolean_flag_alone(self, flag):
        """Boolean flags alone should not register as prompts."""
        spec = resolve_gemini_args([flag], None)
        assert flag in spec.clean_tokens
        assert spec.positional_tokens == ()
        assert not spec.has_errors()


class TestErrorHandling:
    """Test error detection and reporting."""

    def test_flag_missing_value_at_end(self):
        """Flag at end without value is an error."""
        spec = resolve_gemini_args(["hello", "--model"], None)
        assert spec.has_errors()
        assert any("--model" in err and "requires a value" in err for err in spec.errors)

    def test_flag_missing_value_before_flag(self):
        """Flag without value before another flag."""
        spec = resolve_gemini_args(["--model", "--debug"], None)
        assert spec.has_errors()
        assert any("--model" in err for err in spec.errors)

    def test_env_string_invalid_quoting(self):
        """Invalid shell quoting in env string."""
        spec = resolve_gemini_args(None, 'unmatched "quote')
        assert spec.has_errors()
        assert any("invalid Gemini args" in err for err in spec.errors)


class TestEnvStringParsing:
    """Test parsing from env string."""

    def test_env_string_simple(self):
        """Simple env string parsed."""
        spec = resolve_gemini_args(None, "--yolo --model gemini-2.5-flash")
        assert spec.source == "env"
        assert spec.is_yolo
        assert spec.flag_values["--model"] == "gemini-2.5-flash"

    def test_env_string_quoted(self):
        """Quoted values in env string."""
        spec = resolve_gemini_args(None, '"explain this code"')
        assert spec.positional_tokens == ("explain this code",)

    def test_cli_overrides_env(self):
        """CLI args take precedence over env."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-pro"], "--model gemini-2.5-flash")
        assert spec.source == "cli"
        assert spec.flag_values["--model"] == "gemini-2.5-pro"


class TestRebuildTokens:
    """Test token rebuilding for command composition."""

    def test_rebuild_basic(self):
        """rebuild_tokens() returns usable tokens."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash", "hello"], None)
        tokens = spec.rebuild_tokens()
        assert "--model" in tokens
        assert "gemini-2.5-flash" in tokens
        assert "hello" in tokens

    def test_rebuild_with_subcommand(self):
        """rebuild_tokens() includes subcommand."""
        spec = resolve_gemini_args(["mcp", "list"], None)
        tokens = spec.rebuild_tokens(include_subcommand=True)
        assert "mcp" in tokens
        assert "list" in tokens

    def test_rebuild_without_subcommand(self):
        """rebuild_tokens() can exclude subcommand."""
        spec = resolve_gemini_args(["mcp", "list"], None)
        tokens = spec.rebuild_tokens(include_subcommand=False)
        assert "mcp" not in tokens
        assert "list" in tokens

    def test_to_env_string(self):
        """to_env_string() produces valid shell string."""
        spec = resolve_gemini_args(["--yolo", "--model", "gemini-2.5-flash", "do task"], None)
        env_str = spec.to_env_string()
        # Should be shell-quoted
        assert "--yolo" in env_str
        assert "gemini-2.5-flash" in env_str
        # Reparsing should give same result
        reparsed = resolve_gemini_args(None, env_str)
        assert reparsed.is_yolo
        assert reparsed.flag_values["--model"] == "gemini-2.5-flash"


class TestUpdateMethod:
    """Test GeminiArgsSpec.update() for modifications."""

    def test_update_add_yolo(self):
        """Add yolo flag via update()."""
        spec = resolve_gemini_args(["hello"], None)
        assert not spec.is_yolo

        updated = spec.update(yolo=True)
        assert updated.is_yolo
        assert "--yolo" in updated.clean_tokens

    def test_update_remove_yolo(self):
        """Remove yolo flag via update()."""
        spec = resolve_gemini_args(["--yolo", "hello"], None)
        assert spec.is_yolo

        updated = spec.update(yolo=False)
        assert not updated.is_yolo
        assert "--yolo" not in updated.clean_tokens

    def test_update_json_output(self):
        """Set json output via update()."""
        spec = resolve_gemini_args(["hello"], None)
        updated = spec.update(json_output=True)
        assert updated.is_json
        assert updated.output_format == "json"

    def test_update_stream_json(self):
        """Set stream-json via update()."""
        spec = resolve_gemini_args(["hello"], None)
        updated = spec.update(stream_json=True)
        assert updated.is_json
        assert updated.output_format == "stream-json"

    def test_update_interactive_prompt_new(self):
        """Add interactive prompt when none exists."""
        spec = resolve_gemini_args([], None)
        updated = spec.update(prompt="new task")
        # update() sets -i flag for interactive mode (headless not supported in hcom)
        assert updated.has_flag(["-i", "--prompt-interactive"], ("-i=", "--prompt-interactive="))
        assert updated.get_flag_value("-i") == "new task" or updated.get_flag_value("--prompt-interactive") == "new task"

    def test_update_interactive_prompt_replace(self):
        """Replace existing interactive prompt."""
        spec = resolve_gemini_args(["-i", "old task"], None)
        updated = spec.update(prompt="new task")
        # Should replace the -i value
        assert updated.has_flag(["-i", "--prompt-interactive"], ("-i=", "--prompt-interactive="))
        prompt_val = updated.get_flag_value("-i") or updated.get_flag_value("--prompt-interactive")
        assert prompt_val == "new task"

    def test_update_approval_mode(self):
        """Set approval mode via update()."""
        spec = resolve_gemini_args([], None)
        updated = spec.update(approval_mode="auto_edit")
        assert updated.approval_mode == "auto_edit"

    def test_update_include_directories(self):
        """Set include directories via update()."""
        spec = resolve_gemini_args([], None)
        updated = spec.update(include_directories=["/dir1", "/dir2"])
        assert updated.get_flag_value("--include-directories") == ["/dir1", "/dir2"]

    def test_update_combined(self):
        """Update multiple fields at once."""
        spec = resolve_gemini_args([], None)
        updated = spec.update(
            yolo=True,
            prompt="task",
            json_output=True
        )
        assert updated.is_yolo
        # prompt sets -i flag (interactive mode)
        assert updated.has_flag(["-i", "--prompt-interactive"], ("-i=", "--prompt-interactive="))
        assert updated.is_json


class TestEdgeCases:
    """Test edge cases and corner scenarios."""

    def test_empty_positional(self):
        """Empty string as positional is preserved."""
        spec = resolve_gemini_args([""], None)
        assert spec.positional_tokens == ("",)

    def test_numeric_positional(self):
        """Numeric string is positional."""
        spec = resolve_gemini_args(["123"], None)
        assert spec.positional_tokens == ("123",)

    def test_double_dash_delimiter(self):
        """Arguments after -- are all positional."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash", "--", "--not-a-flag"], None)
        assert "--not-a-flag" in spec.positional_tokens
        assert "--" in spec.clean_tokens

    def test_flag_like_positional_after_double_dash(self):
        """Flags after -- are positional."""
        spec = resolve_gemini_args(["--", "--not-a-flag", "-y"], None)
        assert "--not-a-flag" in spec.positional_tokens
        assert "-y" in spec.positional_tokens
        # Yolo flag NOT detected (after --)
        assert not spec.is_yolo

    def test_very_long_prompt(self):
        """Very long positional prompt handled."""
        long_prompt = "x" * 10000
        spec = resolve_gemini_args([long_prompt], None)
        assert spec.positional_tokens[0] == long_prompt

    def test_unicode_in_prompt(self):
        """Unicode characters in prompts preserved."""
        spec = resolve_gemini_args(["你好世界 🎉"], None)
        assert spec.positional_tokens[0] == "你好世界 🎉"

    def test_newlines_in_prompt(self):
        """Newlines in prompts preserved."""
        prompt = "line1\nline2\nline3"
        spec = resolve_gemini_args([prompt], None)
        assert spec.positional_tokens[0] == prompt

    def test_case_sensitivity_flags(self):
        """Flag matching is case-insensitive for detection."""
        spec = resolve_gemini_args(["--MODEL", "gemini-2.5-flash"], None)
        # Lowercase matching
        assert spec.flag_values["--model"] == "gemini-2.5-flash"


class TestRealWorldScenarios:
    """Test realistic usage patterns."""

    def test_gemini_headless_launch(self):
        """Typical gemini headless launch."""
        spec = resolve_gemini_args(["-o", "json", "--yolo", "analyze codebase"], None)
        assert spec.is_headless
        assert spec.is_yolo
        assert spec.is_json
        assert spec.positional_tokens == ("analyze codebase",)

    def test_gemini_with_multiple_dirs(self):
        """Include multiple directories."""
        spec = resolve_gemini_args([
            "--include-directories", "/path1",
            "--include-directories", "/path2",
            "explain this"
        ], None)
        assert spec.get_flag_value("--include-directories") == ["/path1", "/path2"]
        assert spec.is_headless

    def test_gemini_stream_json_mode(self):
        """Stream JSON for real-time monitoring."""
        spec = resolve_gemini_args([
            "--output-format", "stream-json",
            "--yolo",
            "fix the bug"
        ], None)
        assert spec.output_format == "stream-json"
        assert spec.is_json
        assert spec.is_yolo

    def test_gemini_approval_auto_edit(self):
        """Auto edit approval mode."""
        spec = resolve_gemini_args([
            "--approval-mode", "auto_edit",
            "refactor this function"
        ], None)
        assert spec.approval_mode == "auto_edit"
        assert not spec.is_yolo

    def test_gemini_resume_session(self):
        """Resume session."""
        spec = resolve_gemini_args(["--resume", "latest"], None)
        assert spec.get_flag_value("--resume") == "latest"
        # Resume without query is not headless
        assert not spec.is_headless


class TestValidateConflicts:
    """Test conflict detection in GeminiArgsSpec."""

    def test_no_conflicts_empty_spec(self):
        """Empty spec has no conflicts."""
        spec = resolve_gemini_args([], None)
        warnings = validate_gemini_conflicts(spec)
        assert warnings == []

    def test_no_conflicts_simple_args(self):
        """Simple args without conflicts."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash"], None)
        warnings = validate_gemini_conflicts(spec)
        assert warnings == []

    def test_headless_positional_rejected(self):
        """Positional query (headless mode) is rejected in hcom."""
        spec = resolve_gemini_args(["hello world"], None)
        warnings = validate_gemini_conflicts(spec)
        assert len(warnings) == 1
        assert warnings[0].startswith("ERROR:")
        assert "headless mode" in warnings[0]
        assert "positional query" in warnings[0]

    def test_headless_prompt_flag_rejected(self):
        """--prompt flag (headless mode) is rejected in hcom."""
        spec = resolve_gemini_args(["-p", "hello"], None)
        warnings = validate_gemini_conflicts(spec)
        assert len(warnings) >= 1
        assert any("headless mode" in w and "-p/--prompt" in w for w in warnings)

    def test_yolo_and_approval_mode_warning(self):
        """--yolo and --approval-mode together is an error."""
        spec = resolve_gemini_args(["--yolo", "--approval-mode", "yolo"], None)
        warnings = validate_gemini_conflicts(spec)
        assert len(warnings) == 1
        assert warnings[0].startswith("ERROR:")
        assert "--yolo" in warnings[0] and "--approval-mode" in warnings[0]

class TestMergeGeminiArgs:
    """Test merge_gemini_args functionality."""

    def test_merge_cli_flags_keeps_env_prompt(self):
        """CLI flags without prompt should KEEP env prompt."""
        env = resolve_gemini_args(None, "'say hi' --model gemini-2.5-flash")
        cli = resolve_gemini_args(["--model", "gemini-2.5-pro"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.positional_tokens == ("say hi",)  # Kept from env
        assert merged.get_flag_value("--model") == "gemini-2.5-pro"  # CLI wins

    def test_merge_cli_prompt_replaces_env_prompt(self):
        """CLI prompt should REPLACE env prompt entirely."""
        env = resolve_gemini_args(None, "'say hi' --model gemini-2.5-flash")
        cli = resolve_gemini_args(["new task", "--model", "gemini-2.5-pro"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.positional_tokens == ("new task",)  # CLI replaced
        assert merged.get_flag_value("--model") == "gemini-2.5-pro"

    def test_merge_no_cli_args_uses_env(self):
        """No CLI args should use env entirely."""
        env = resolve_gemini_args(None, "--model gemini-2.5-flash --yolo 'do task'")
        cli = resolve_gemini_args(None, None)
        merged = merge_gemini_args(env, cli)

        assert merged.get_flag_value("--model") == "gemini-2.5-flash"
        assert merged.is_yolo
        assert merged.positional_tokens == ("do task",)

    def test_merge_cli_overrides_env_flag(self):
        """CLI flag should override env flag."""
        env = resolve_gemini_args(None, "--model gemini-2.5-flash --output-format text")
        cli = resolve_gemini_args(["--model", "gemini-2.5-pro"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.get_flag_value("--model") == "gemini-2.5-pro"  # CLI wins
        assert merged.get_flag_value("--output-format") == "text"  # Env preserved

    def test_merge_cli_adds_new_flags(self):
        """CLI can add new flags not in env."""
        env = resolve_gemini_args(None, "--model gemini-2.5-flash")
        cli = resolve_gemini_args(["--yolo", "--debug"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.get_flag_value("--model") == "gemini-2.5-flash"  # Env preserved
        assert merged.is_yolo  # CLI added
        assert merged.has_flag(["--debug"])  # CLI added

    def test_merge_boolean_flag_deduplication(self):
        """Duplicate boolean flags should be deduped."""
        env = resolve_gemini_args(None, "--debug")
        cli = resolve_gemini_args(["--debug", "--model", "gemini-2.5-pro"], None)
        merged = merge_gemini_args(env, cli)

        debug_count = merged.clean_tokens.count("--debug")
        assert debug_count == 1  # Deduped

    def test_merge_yolo_mode_inherited(self):
        """Yolo mode from env should be inherited if CLI doesn't specify."""
        env = resolve_gemini_args(None, "--yolo --model gemini-2.5-flash")
        cli = resolve_gemini_args(["--model", "gemini-2.5-pro"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.is_yolo  # Inherited from env
        assert merged.get_flag_value("--model") == "gemini-2.5-pro"  # CLI wins

    def test_merge_subcommand_cli_wins(self):
        """CLI subcommand takes precedence."""
        env = resolve_gemini_args(None, "mcp list")
        cli = resolve_gemini_args(["hooks", "panel"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.subcommand == "hooks"  # CLI wins

    def test_merge_complex_scenario(self):
        """Complex real-world scenario."""
        env = resolve_gemini_args(None, "'default task' --model gemini-2.5-flash --debug --yolo")
        cli = resolve_gemini_args(["new task", "--model", "gemini-2.5-pro", "--output-format", "json"], None)
        merged = merge_gemini_args(env, cli)

        assert merged.positional_tokens == ("new task",)  # CLI prompt
        assert merged.get_flag_value("--model") == "gemini-2.5-pro"  # CLI wins
        assert merged.has_flag(["--debug"])  # Env preserved
        assert merged.is_yolo  # Env preserved
        assert merged.get_flag_value("--output-format") == "json"  # CLI added


class TestResumeOptionalValue:
    """Regression tests for --resume optional value handling.

    Per gemini docs, --resume [session_id] can be used:
    - gemini --resume → resume latest
    - gemini --resume 123 → resume session 123
    - gemini --resume latest → resume latest (explicit)
    - gemini --resume UUID → resume specific session
    - gemini --resume "prompt" → resume latest WITH prompt (not session="prompt")
    """

    def test_resume_alone_no_error(self):
        """--resume without value should not error."""
        spec = resolve_gemini_args(['--resume'], None)
        assert not spec.has_errors()
        assert spec.get_flag_value('--resume') is None

    def test_resume_short_alone_no_error(self):
        """-r without value should not error."""
        spec = resolve_gemini_args(['-r'], None)
        assert not spec.has_errors()
        assert spec.get_flag_value('-r') is None

    def test_resume_with_prompt_not_consumed(self):
        """--resume followed by prompt should NOT consume prompt as session ID."""
        spec = resolve_gemini_args(['--resume', 'continue task'], None)
        assert spec.get_flag_value('--resume') is None
        assert spec.positional_tokens == ('continue task',)
        assert spec.is_headless

    def test_resume_short_with_prompt_not_consumed(self):
        """-r followed by prompt should NOT consume prompt as session ID."""
        spec = resolve_gemini_args(['-r', 'do stuff'], None)
        assert spec.get_flag_value('-r') is None
        assert spec.positional_tokens == ('do stuff',)
        assert spec.is_headless

    def test_resume_with_numeric_id(self):
        """--resume with numeric ID should consume the ID."""
        spec = resolve_gemini_args(['--resume', '123', 'task'], None)
        assert spec.get_flag_value('--resume') == '123'
        assert spec.positional_tokens == ('task',)

    def test_resume_with_latest_keyword(self):
        """--resume with 'latest' should consume it as value."""
        spec = resolve_gemini_args(['--resume', 'latest', 'task'], None)
        assert spec.get_flag_value('--resume') == 'latest'
        assert spec.positional_tokens == ('task',)

    def test_resume_with_uuid(self):
        """--resume with UUID should consume the UUID."""
        uuid = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
        spec = resolve_gemini_args(['--resume', uuid, 'task'], None)
        assert spec.get_flag_value('--resume') == uuid
        assert spec.positional_tokens == ('task',)

    def test_resume_equals_syntax(self):
        """--resume=value syntax should work."""
        spec = resolve_gemini_args(['--resume=abc123', 'task'], None)
        assert spec.get_flag_value('--resume') == 'abc123'
        assert spec.positional_tokens == ('task',)

    def test_resume_before_flag(self):
        """--resume before another flag should not consume the flag."""
        spec = resolve_gemini_args(['--resume', '--yolo', 'task'], None)
        assert spec.get_flag_value('--resume') is None
        assert spec.is_yolo
        assert spec.positional_tokens == ('task',)


class TestBoundaryFlagInteractions:
    """Test boundary cases and complex flag interactions."""

    def test_duplicate_model_flag_space_syntax(self):
        """Duplicate --model flags with space syntax."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash", "--model", "gemini-2.5-pro"], None)
        # Both values appear in clean_tokens
        assert spec.clean_tokens.count("--model") == 2
        assert "gemini-2.5-flash" in spec.clean_tokens
        assert "gemini-2.5-pro" in spec.clean_tokens

    def test_duplicate_model_flag_mixed_syntax(self):
        """Duplicate --model with mixed = and space syntax."""
        spec = resolve_gemini_args(["--model=gemini-2.5-flash", "--model", "gemini-2.5-pro"], None)
        assert "--model=gemini-2.5-flash" in spec.clean_tokens
        assert "--model" in spec.clean_tokens
        assert "gemini-2.5-pro" in spec.clean_tokens

    def test_multiple_yolo_flags(self):
        """Multiple yolo flags preserved but deduplicated in final."""
        spec = resolve_gemini_args(["--yolo", "-y", "task"], None)
        assert spec.is_yolo
        # Both in clean_tokens
        assert "--yolo" in spec.clean_tokens
        assert "-y" in spec.clean_tokens

    def test_empty_flag_value(self):
        """Empty string as flag value."""
        spec = resolve_gemini_args(["--model", "", "task"], None)
        assert spec.get_flag_value("--model") == ""
        assert spec.positional_tokens == ("task",)

    def test_flag_after_double_dash_not_recognized(self):
        """Flags after -- treated as positional."""
        spec = resolve_gemini_args(["--model", "gemini-2.5-flash", "--", "--debug"], None)
        assert spec.get_flag_value("--model") == "gemini-2.5-flash"
        assert not spec.has_flag(["--debug"])
        assert "--debug" in spec.positional_tokens


@pytest.mark.slow
class TestPropertyBased:
    """Property-based tests using Hypothesis for edge case discovery."""

    @staticmethod
    def boolean_flags():
        """Generate known boolean flags."""
        return st.sampled_from([
            '--debug', '-d',
            '--sandbox', '-s',
            '--yolo', '-y',
            '--list-extensions', '-l',
            '--list-sessions',
            '--screen-reader',
            '--version', '-v',
            '--help', '-h',
            '--experimental-acp',
        ])

    @staticmethod
    def value_flags():
        """Generate known value flags with realistic values."""
        return st.one_of(
            st.tuples(st.just('--model'), st.sampled_from(['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.5-flash-lite'])),
            st.tuples(st.just('--output-format'), st.sampled_from(['text', 'json', 'stream-json'])),
            st.tuples(st.just('--approval-mode'), st.sampled_from(['default', 'auto_edit', 'yolo'])),
            st.tuples(st.just('--resume'), st.sampled_from(['latest', '1', '5'])),
            st.tuples(st.just('--include-directories'), st.text(min_size=1, max_size=20)),
            st.tuples(st.just('--extensions'), st.text(min_size=1, max_size=20)),
            st.tuples(st.just('--delete-session'), st.text(min_size=1, max_size=10)),
        )

    @staticmethod
    def positional_args():
        """Generate realistic positional arguments (queries)."""
        return st.one_of(
            st.text(min_size=1, max_size=100),
            st.sampled_from([
                'explain this code',
                'fix the bug',
                'write tests',
                '- check status',
                'task with "quotes"',
            ]),
        )

    @staticmethod
    def valid_token_sequence():
        """Generate realistic token sequences."""
        return st.lists(
            st.one_of(
                TestPropertyBased.boolean_flags(),
                TestPropertyBased.value_flags().map(lambda t: list(t)),
                TestPropertyBased.positional_args(),
            ),
            min_size=0,
            max_size=15,
        ).map(lambda items: [token for item in items for token in (item if isinstance(item, list) else [item])])

    @given(st.lists(st.text(max_size=200), max_size=30))
    @settings(max_examples=200, deadline=None)
    def test_parser_never_crashes(self, args):
        """PROPERTY: Parser handles any input without crashing."""
        try:
            spec = resolve_gemini_args(args, None)
            assert isinstance(spec, GeminiArgsSpec)
            if spec.errors:
                assert all(isinstance(e, str) for e in spec.errors)
        except Exception as e:
            pytest.fail(f"Parser crashed on input {args!r}: {e}")

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_roundtrip_stability(self, valid_token_sequence):
        """PROPERTY: parse → to_env_string → reparse produces stable result."""
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec1 = resolve_gemini_args(data, None)

        assume(not spec1.has_errors())

        env_str = spec1.to_env_string()
        spec2 = resolve_gemini_args(None, env_str)

        assert not spec2.has_errors(), f"Roundtrip introduced errors: {spec2.errors}"
        assert spec2.clean_tokens == spec1.clean_tokens
        assert spec2.is_headless == spec1.is_headless
        assert spec2.positional_tokens == spec1.positional_tokens

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_update_preserves_unrelated_fields(self, valid_token_sequence):
        """PROPERTY: update() operations don't lose unrelated data."""
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_gemini_args(data, None)
        assume(not spec.has_errors())

        if not spec.is_yolo:
            updated = spec.update(yolo=True)

            if spec.has_flag(['--model']):
                assert updated.has_flag(['--model']), "Yolo toggle lost --model flag"
                assert updated.get_flag_value('--model') == spec.get_flag_value('--model')

            assert updated.positional_tokens == spec.positional_tokens, \
                "Yolo toggle changed positionals"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_double_dash_boundary_respected(self, valid_token_sequence):
        """PROPERTY: -- separator prevents flag interpretation after it."""
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        modified = list(data) + ['--', '--not-a-flag', '-y', '--debug']

        spec = resolve_gemini_args(modified, None)

        assert '--not-a-flag' in spec.positional_tokens
        assert '-y' in spec.positional_tokens
        assert '--debug' in spec.positional_tokens
        assert not spec.has_flag(['--not-a-flag'])

    @given(
        base_args=st.lists(st.text(max_size=50), min_size=0, max_size=10),
        new_yolo=st.booleans(),
    )
    @settings(max_examples=100, deadline=None)
    def test_yolo_toggle_idempotent(self, base_args, new_yolo):
        """PROPERTY: Toggling yolo twice is idempotent."""
        spec = resolve_gemini_args(base_args, None)
        assume(not spec.has_errors())

        once = spec.update(yolo=new_yolo)
        twice = once.update(yolo=new_yolo)

        assert once.is_yolo == twice.is_yolo
        assert once.clean_tokens == twice.clean_tokens
        assert once.positional_tokens == twice.positional_tokens


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
