#!/usr/bin/env python3
"""Unit tests for codex_args - Codex CLI argument parsing and composition.

Run: pytest test/test_codex_args.py -v
"""

import pytest
from hypothesis import given, strategies as st, assume, settings, HealthCheck
from hcom.tools.codex.args import (
    resolve_codex_args,
    merge_codex_args,
    validate_conflicts,
    CodexArgsSpec,
)


class TestBasicParsing:
    """Test fundamental parsing scenarios."""

    def test_empty_args(self):
        """Empty input produces empty spec."""
        spec = resolve_codex_args(None, None)
        assert spec.source == "none"
        assert spec.raw_tokens == ()
        assert spec.clean_tokens == ()
        assert spec.positional_tokens == ()
        assert spec.subcommand is None
        assert not spec.is_exec
        assert not spec.is_json
        assert not spec.errors

    def test_simple_prompt(self):
        """Single positional argument parsed correctly."""
        spec = resolve_codex_args(["hello world"], None)
        assert spec.positional_tokens == ("hello world",)
        assert spec.clean_tokens == ("hello world",)
        assert spec.subcommand is None

    def test_exec_subcommand(self):
        """exec subcommand detected."""
        spec = resolve_codex_args(["exec", "do task"], None)
        assert spec.subcommand == "exec"
        assert spec.is_exec
        assert spec.positional_tokens == ("do task",)

    def test_resume_subcommand(self):
        """resume subcommand detected."""
        spec = resolve_codex_args(["resume", "abc123"], None)
        assert spec.subcommand == "resume"
        assert not spec.is_exec
        assert spec.positional_tokens == ("abc123",)

    def test_review_subcommand(self):
        """review subcommand detected."""
        spec = resolve_codex_args(["review"], None)
        assert spec.subcommand == "review"
        assert not spec.is_exec

    def test_unknown_option_has_suggestion(self):
        spec = resolve_codex_args(["--moddel", "o3"], None)
        assert spec.has_errors()
        assert any("unknown option" in err.lower() for err in spec.errors)
        assert any("--model" in err for err in spec.errors)


class TestJsonFlag:
    """Test --json flag detection (streaming output mode)."""

    def test_json_flag_detected(self):
        """--json flag sets is_json."""
        spec = resolve_codex_args(["exec", "--json", "task"], None)
        assert spec.is_json
        assert spec.is_exec

    def test_json_without_exec(self):
        """--json without exec still parses (validation catches it)."""
        spec = resolve_codex_args(["--json", "task"], None)
        assert spec.is_json
        assert not spec.is_exec

    def test_json_warning_without_exec(self):
        """validate_conflicts warns about --json without exec."""
        spec = resolve_codex_args(["--json", "task"], None)
        warnings = validate_conflicts(spec)
        assert any("--json" in w for w in warnings)


class TestModelFlag:
    """Test model flag parsing."""

    def test_model_flag_separate(self):
        """--model with separate value."""
        spec = resolve_codex_args(["--model", "o3"], None)
        assert spec.get_flag_value("--model") == "o3"
        assert spec.get_flag_value("-m") == "o3"

    def test_model_flag_equals(self):
        """--model= syntax."""
        spec = resolve_codex_args(["--model=gpt-4"], None)
        assert spec.get_flag_value("--model") == "gpt-4"

    def test_model_short_form(self):
        """-m short form."""
        spec = resolve_codex_args(["-m", "o3-mini"], None)
        assert spec.get_flag_value("-m") == "o3-mini"


class TestConfigFlag:
    """Test -c/--config repeatable flag."""

    def test_single_config(self):
        """Single -c flag."""
        spec = resolve_codex_args(["-c", "model=o3"], None)
        value = spec.get_flag_value("-c")
        assert value == ["model=o3"]

    def test_multiple_config(self):
        """Multiple -c flags accumulate."""
        spec = resolve_codex_args(["-c", "model=o3", "-c", "sandbox_permissions=[]"], None)
        value = spec.get_flag_value("-c")
        assert isinstance(value, list)
        assert len(value) == 2
        assert "model=o3" in value
        assert "sandbox_permissions=[]" in value

    def test_config_equals_syntax(self):
        """-c= syntax."""
        spec = resolve_codex_args(["-c=model=o3"], None)
        value = spec.get_flag_value("-c")
        assert value == ["model=o3"]


class TestSandboxFlag:
    """Test sandbox policy flag."""

    def test_sandbox_values(self):
        """Valid sandbox values."""
        for mode in ["read-only", "workspace-write", "danger-full-access"]:
            spec = resolve_codex_args(["--sandbox", mode], None)
            assert spec.get_flag_value("--sandbox") == mode

    def test_sandbox_short(self):
        """-s short form."""
        spec = resolve_codex_args(["-s", "read-only"], None)
        assert spec.get_flag_value("-s") == "read-only"


class TestBooleanFlags:
    """Test boolean flag handling."""

    @pytest.mark.parametrize(
        "flag",
        [
            "--oss",
            "--full-auto",
            "--dangerously-bypass-approvals-and-sandbox",
            "--search",
            "--skip-git-repo-check",
            "--last",
            "--all",
            "--uncommitted",
            "--no-alt-screen",
        ],
    )
    def test_boolean_flag_with_prompt(self, flag):
        """Boolean flags don't consume following prompt."""
        spec = resolve_codex_args([flag, "do task"], None)
        assert flag.lower() in [t.lower() for t in spec.clean_tokens]
        assert "do task" in spec.positional_tokens
        assert not spec.has_errors()

    @pytest.mark.parametrize(
        "flag",
        [
            "--oss",
            "--full-auto",
            "--search",
            "--no-alt-screen",
        ],
    )
    def test_boolean_flag_alone(self, flag):
        """Boolean flags alone work."""
        spec = resolve_codex_args([flag], None)
        assert spec.has_flag([flag])
        assert spec.positional_tokens == ()
        assert not spec.has_errors()


class TestExecMode:
    """Test exec subcommand specifics."""

    def test_exec_with_json_and_prompt(self):
        """Full exec mode example."""
        spec = resolve_codex_args(["exec", "--json", "--model", "o3", "analyze code"], None)
        assert spec.is_exec
        assert spec.is_json
        assert spec.get_flag_value("--model") == "o3"
        assert spec.positional_tokens == ("analyze code",)

    def test_exec_full_auto(self):
        """exec --full-auto mode."""
        spec = resolve_codex_args(["exec", "--full-auto", "fix bugs"], None)
        assert spec.is_exec
        assert spec.has_flag(["--full-auto"])

    def test_exec_with_output_file(self):
        """exec -o output file."""
        spec = resolve_codex_args(["exec", "-o", "result.txt", "task"], None)
        assert spec.get_flag_value("-o") == "result.txt"


class TestResumeMode:
    """Test resume subcommand specifics."""

    def test_resume_with_session_id(self):
        """resume with session ID."""
        spec = resolve_codex_args(["resume", "abc-123-def"], None)
        assert spec.subcommand == "resume"
        assert spec.positional_tokens == ("abc-123-def",)

    def test_fork_with_session_id(self):
        """fork with session ID."""
        spec = resolve_codex_args(["fork", "abc-123-def"], None)
        assert spec.subcommand == "fork"
        assert not spec.is_exec
        assert spec.positional_tokens == ("abc-123-def",)

    def test_fork_last(self):
        """fork --last."""
        spec = resolve_codex_args(["fork", "--last"], None)
        assert spec.subcommand == "fork"
        assert spec.has_flag(["--last"])

    def test_resume_last(self):
        """resume --last."""
        spec = resolve_codex_args(["resume", "--last"], None)
        assert spec.subcommand == "resume"
        assert spec.has_flag(["--last"])

    def test_resume_last_with_prompt(self):
        """resume --last with follow-up prompt."""
        spec = resolve_codex_args(["resume", "--last", "continue task"], None)
        assert spec.has_flag(["--last"])
        assert spec.positional_tokens == ("continue task",)


class TestReviewMode:
    """Test review subcommand specifics."""

    def test_review_uncommitted(self):
        """review --uncommitted."""
        spec = resolve_codex_args(["review", "--uncommitted"], None)
        assert spec.subcommand == "review"
        assert spec.has_flag(["--uncommitted"])

    def test_review_base_branch(self):
        """review --base branch."""
        spec = resolve_codex_args(["review", "--base", "main"], None)
        assert spec.get_flag_value("--base") == "main"

    def test_review_commit(self):
        """review --commit SHA."""
        spec = resolve_codex_args(["review", "--commit", "abc123"], None)
        assert spec.get_flag_value("--commit") == "abc123"


class TestDoubleDash:
    """Test -- separator handling."""

    def test_flags_after_double_dash_are_positional(self):
        """Flags after -- are positional."""
        spec = resolve_codex_args(["exec", "--", "--not-a-flag"], None)
        assert "--not-a-flag" in spec.positional_tokens
        assert not spec.has_flag(["--not-a-flag"])

    def test_json_before_double_dash(self):
        """--json before -- is detected, after is not."""
        spec = resolve_codex_args(["exec", "--json", "--", "--json"], None)
        assert spec.is_json
        # Second --json is positional
        assert "--json" in spec.positional_tokens


class TestErrorHandling:
    """Test error detection."""

    def test_missing_value_at_end(self):
        """Flag without value at end is error."""
        spec = resolve_codex_args(["--model"], None)
        assert spec.has_errors()
        assert any("--model" in e and "requires a value" in e for e in spec.errors)

    def test_missing_value_before_flag(self):
        """Flag without value before another flag."""
        spec = resolve_codex_args(["--model", "--json"], None)
        assert spec.has_errors()
        assert any("--model" in e for e in spec.errors)

    def test_env_string_invalid_quoting(self):
        """Invalid shell quoting in env string."""
        spec = resolve_codex_args(None, 'unmatched "quote')
        assert spec.has_errors()


class TestEnvStringParsing:
    """Test parsing from env string."""

    def test_env_string_simple(self):
        """Simple env string."""
        spec = resolve_codex_args(None, "exec --json --model o3")
        assert spec.source == "env"
        assert spec.is_exec
        assert spec.is_json
        assert spec.get_flag_value("--model") == "o3"

    def test_env_string_quoted(self):
        """Quoted values in env string."""
        spec = resolve_codex_args(None, 'exec "analyze the code"')
        assert spec.positional_tokens == ("analyze the code",)

    def test_cli_overrides_env(self):
        """CLI args take precedence over env."""
        spec = resolve_codex_args(["--model", "gpt-4"], "--model o3")
        assert spec.source == "cli"
        assert spec.get_flag_value("--model") == "gpt-4"


class TestRebuildTokens:
    """Test token rebuilding."""

    def test_rebuild_with_subcommand(self):
        """rebuild_tokens includes subcommand by default."""
        spec = resolve_codex_args(["exec", "--json", "task"], None)
        tokens = spec.rebuild_tokens()
        assert tokens[0] == "exec"
        assert "--json" in tokens
        assert "task" in tokens

    def test_rebuild_without_subcommand(self):
        """rebuild_tokens can exclude subcommand."""
        spec = resolve_codex_args(["exec", "--json", "task"], None)
        tokens = spec.rebuild_tokens(include_subcommand=False)
        assert "exec" not in tokens
        assert "--json" in tokens

    def test_to_env_string(self):
        """to_env_string produces valid shell string."""
        spec = resolve_codex_args(["exec", "--model", "o3", "do task"], None)
        env_str = spec.to_env_string()
        assert "exec" in env_str
        assert "o3" in env_str
        # Reparsing should give same result
        reparsed = resolve_codex_args(None, env_str)
        assert reparsed.is_exec
        assert reparsed.get_flag_value("--model") == "o3"


class TestUpdateMethod:
    """Test CodexArgsSpec.update() modifications."""

    def test_update_add_json(self):
        """Add --json via update()."""
        spec = resolve_codex_args(["exec", "task"], None)
        assert not spec.is_json
        updated = spec.update(json_output=True)
        assert updated.is_json

    def test_update_remove_json(self):
        """Remove --json via update()."""
        spec = resolve_codex_args(["exec", "--json", "task"], None)
        assert spec.is_json
        updated = spec.update(json_output=False)
        assert not updated.is_json

    def test_update_prompt(self):
        """Update prompt via update()."""
        spec = resolve_codex_args(["exec", "old task"], None)
        updated = spec.update(prompt="new task")
        assert "new task" in updated.positional_tokens

    def test_update_combined(self):
        """Update multiple fields."""
        spec = resolve_codex_args([], None)
        updated = spec.update(subcommand="exec", json_output=True, prompt="analyze")
        assert updated.is_exec
        assert updated.is_json
        assert "analyze" in updated.positional_tokens

    def test_update_developer_instructions(self):
        """Add developer instructions via update()."""
        spec = resolve_codex_args(["exec", "task"], None)
        updated = spec.update(developer_instructions="You are helpful")
        # Should prepend -c developer_instructions=...
        tokens = updated.rebuild_tokens()
        assert "-c" in tokens
        assert any("developer_instructions" in t for t in tokens)

    def test_update_developer_instructions_prepends(self):
        """Developer instructions -c flag is prepended (takes precedence)."""
        spec = resolve_codex_args(["exec", "-c", "model=o3", "task"], None)
        updated = spec.update(developer_instructions="Be concise")
        tokens = updated.rebuild_tokens(include_subcommand=False)
        # Developer instructions -c should come before user's -c model=o3
        c_indexes = [i for i, t in enumerate(tokens) if t == "-c"]
        assert len(c_indexes) == 2
        # First -c should be followed by developer_instructions
        first_c_idx = c_indexes[0]
        assert "developer_instructions" in tokens[first_c_idx + 1]

    def test_update_developer_instructions_with_existing_config(self):
        """Developer instructions works with existing -c flags from env."""
        env = resolve_codex_args(None, "-c model=gpt-4")
        cli = resolve_codex_args(["-c", "sandbox_permissions=[]"], None)
        merged = merge_codex_args(env, cli)
        final = merged.update(developer_instructions="Custom instructions")
        # Should have 3 -c flags: developer instructions, env model, cli sandbox
        c_values = final.get_flag_value("-c")
        assert isinstance(c_values, list)
        assert len(c_values) == 3


class TestMergeCodexArgs:
    """Test merge_codex_args functionality."""

    def test_merge_cli_overrides_env_model(self):
        """CLI model overrides env model."""
        env = resolve_codex_args(None, "--model o3")
        cli = resolve_codex_args(["--model", "gpt-4"], None)
        merged = merge_codex_args(env, cli)
        assert merged.get_flag_value("--model") == "gpt-4"

    def test_merge_cli_subcommand_wins(self):
        """CLI subcommand takes precedence."""
        env = resolve_codex_args(None, "exec --model o3")
        cli = resolve_codex_args(["resume", "--last"], None)
        merged = merge_codex_args(env, cli)
        assert merged.subcommand == "resume"

    def test_merge_preserves_env_when_cli_empty(self):
        """Empty CLI preserves env."""
        env = resolve_codex_args(None, "exec --json --model o3")
        cli = resolve_codex_args([], None)
        merged = merge_codex_args(env, cli)
        assert merged.is_exec
        assert merged.is_json
        assert merged.get_flag_value("--model") == "o3"

    def test_merge_cli_prompt_replaces_env(self):
        """CLI prompt replaces env prompt."""
        env = resolve_codex_args(None, "exec 'env task'")
        cli = resolve_codex_args(["cli task"], None)
        merged = merge_codex_args(env, cli)
        assert merged.positional_tokens == ("cli task",)

    def test_merge_repeatable_flags(self):
        """Repeatable flags (-c) accumulate."""
        env = resolve_codex_args(None, "-c model=o3")
        cli = resolve_codex_args(["-c", "sandbox_permissions=[]"], None)
        merged = merge_codex_args(env, cli)
        values = merged.get_flag_value("-c")
        assert isinstance(values, list)
        assert len(values) == 2

    def test_merge_resume_with_flags_preserves_thread_id_position(self):
        """Resume subcommand: thread-id must come immediately after 'resume'.

        Bug fix: merge was putting flags before positionals, but codex expects
        'resume <thread-id> [--flags...]' not 'resume --flags... <thread-id>'.
        """
        env = resolve_codex_args(None, None)
        cli = resolve_codex_args(["resume", "abc-123-thread-id", "--sandbox", "workspace-write"], None)
        merged = merge_codex_args(env, cli)

        # Verify subcommand and thread-id preserved
        assert merged.subcommand == "resume"
        assert "abc-123-thread-id" in merged.positional_tokens

        # Critical: rebuild_tokens should produce valid codex resume syntax
        # where thread-id comes right after "resume", not after flags
        tokens = merged.rebuild_tokens(include_subcommand=True)
        assert tokens[0] == "resume"
        assert tokens[1] == "abc-123-thread-id"  # Thread-id must be second!
        assert "--sandbox" in tokens
        assert "workspace-write" in tokens

    def test_merge_sandbox_flags_as_group(self):
        """Sandbox flags are stripped as a group when CLI overrides any.

        If CLI passes --full-auto, env's --sandbox and -a should be stripped
        to avoid conflicting sandbox configurations.
        """
        # Test 1: --full-auto strips all env sandbox flags
        env = resolve_codex_args(None, "--sandbox workspace-write -a untrusted --model o3")
        cli = resolve_codex_args(["--full-auto"], None)
        merged = merge_codex_args(env, cli)
        tokens = merged.rebuild_tokens()
        assert "--full-auto" in tokens
        assert "--sandbox" not in tokens  # Stripped
        assert "-a" not in tokens  # Stripped
        assert "--model" in tokens  # Preserved

        # Test 2: --sandbox override strips -a
        env = resolve_codex_args(None, "--sandbox workspace-write -a untrusted")
        cli = resolve_codex_args(["--sandbox", "read-only"], None)
        merged = merge_codex_args(env, cli)
        tokens = merged.rebuild_tokens()
        assert "--sandbox" in tokens
        assert "read-only" in tokens
        assert "-a" not in tokens  # Stripped

        # Test 3: Non-sandbox CLI preserves env sandbox flags
        env = resolve_codex_args(None, "--sandbox workspace-write -a untrusted")
        cli = resolve_codex_args(["--model", "o4-mini"], None)
        merged = merge_codex_args(env, cli)
        tokens = merged.rebuild_tokens()
        assert "--sandbox" in tokens
        assert "-a" in tokens


class TestValidateConflicts:
    """Test conflict detection."""

    def test_no_conflicts_normal_usage(self):
        """Normal interactive usage has no conflicts."""
        spec = resolve_codex_args(["--model", "o3"], None)
        warnings = validate_conflicts(spec)
        assert warnings == []

    def test_exec_mode_rejected(self):
        """Exec mode is rejected by hcom (not supported)."""
        spec = resolve_codex_args(["exec", "--json", "--model", "o3", "task"], None)
        warnings = validate_conflicts(spec)
        assert len(warnings) == 1
        assert "ERROR:" in warnings[0]
        assert "exec mode not supported" in warnings[0].lower()

    def test_full_auto_and_bypass_warning(self):
        """--full-auto with --dangerously-bypass warns."""
        spec = resolve_codex_args(["--full-auto", "--dangerously-bypass-approvals-and-sandbox"], None)
        warnings = validate_conflicts(spec)
        assert len(warnings) == 1
        assert "redundant" in warnings[0].lower()


class TestEdgeCases:
    """Test edge cases."""

    def test_empty_prompt(self):
        """Empty string as prompt."""
        spec = resolve_codex_args([""], None)
        assert spec.positional_tokens == ("",)

    def test_numeric_prompt(self):
        """Numeric string is positional."""
        spec = resolve_codex_args(["123"], None)
        assert spec.positional_tokens == ("123",)

    def test_unicode_prompt(self):
        """Unicode in prompt."""
        spec = resolve_codex_args(["analyze 你好世界"], None)
        assert spec.positional_tokens[0] == "analyze 你好世界"

    def test_unknown_flags_preserved(self):
        """Unknown flags passed through."""
        spec = resolve_codex_args(["--unknown-flag", "value"], None)
        assert "--unknown-flag" in spec.clean_tokens
        assert "value" in spec.clean_tokens

    def test_case_sensitivity(self):
        """Subcommands are case-insensitive for detection."""
        spec = resolve_codex_args(["EXEC", "--JSON"], None)
        assert spec.is_exec
        assert spec.is_json

    def test_version_flag_case_sensitive(self):
        """Codex uses -V (uppercase) for version, -v (lowercase) is invalid.

        This is different from most CLI tools which use lowercase -v.
        The parser must accept -V but reject -v.
        """
        # -V (uppercase) should be accepted
        spec_V = resolve_codex_args(["-V"], None)
        assert spec_V.has_flag(["-V"])
        assert not spec_V.errors

        # -v (lowercase) should produce an error
        spec_v = resolve_codex_args(["-v"], None)
        assert spec_v.errors
        assert "unknown option '-v'" in spec_v.errors[0]

        # --version (long form) should work
        spec_long = resolve_codex_args(["--version"], None)
        assert spec_long.has_flag(["--version"])
        assert not spec_long.errors


class TestRealWorldScenarios:
    """Test realistic usage patterns."""

    def test_headless_exec_with_full_options(self):
        """Full exec mode command."""
        spec = resolve_codex_args(
            [
                "exec",
                "--json",
                "--model",
                "o3",
                "--sandbox",
                "workspace-write",
                "-C",
                "/project",
                "analyze and fix bugs",
            ],
            None,
        )
        assert spec.is_exec
        assert spec.is_json
        assert spec.get_flag_value("--model") == "o3"
        assert spec.get_flag_value("--sandbox") == "workspace-write"
        assert spec.get_flag_value("-C") == "/project"
        assert spec.positional_tokens == ("analyze and fix bugs",)

    def test_resume_continuation(self):
        """Resume with continuation prompt."""
        spec = resolve_codex_args(["resume", "abc-123", "--model", "gpt-4", "continue from where we left off"], None)
        assert spec.subcommand == "resume"
        assert spec.get_flag_value("--model") == "gpt-4"
        # Both session ID and prompt are positional
        assert "abc-123" in spec.positional_tokens
        assert "continue from where we left off" in spec.positional_tokens

    def test_interactive_with_config(self):
        """Interactive mode with config overrides."""
        spec = resolve_codex_args(
            ["-c", "model=o3", "-c", 'sandbox_permissions=["disk-full-read-access"]', "--search"], None
        )
        assert spec.subcommand is None
        assert not spec.is_exec
        configs = spec.get_flag_value("-c")
        assert len(configs) == 2
        assert spec.has_flag(["--search"])


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
                "--oss",
                "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox",
                "--search",
                "--skip-git-repo-check",
                "--last",
                "--all",
                "--uncommitted",
                "--json",
            ]
        )

    @staticmethod
    def value_flags():
        """Generate known value flags with realistic values."""
        return st.one_of(
            st.tuples(st.just("--model"), st.sampled_from(["o3", "o3-mini", "gpt-4", "gpt-4o"])),
            st.tuples(st.just("-m"), st.sampled_from(["o3", "o3-mini", "gpt-4"])),
            st.tuples(st.just("--sandbox"), st.sampled_from(["read-only", "workspace-write", "danger-full-access"])),
            st.tuples(st.just("-s"), st.sampled_from(["read-only", "workspace-write"])),
            st.tuples(st.just("-C"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("-o"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--output"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("-i"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--image"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--add-dir"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("-a"), st.sampled_from(["always", "never", "untrusted"])),
            st.tuples(st.just("--approval-mode"), st.sampled_from(["always", "never", "untrusted"])),
            st.tuples(st.just("--base"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--commit"), st.text(min_size=1, max_size=20)),
        )

    @staticmethod
    def repeatable_flags():
        """Generate repeatable flags with values."""
        return st.one_of(
            st.tuples(st.just("-c"), st.text(min_size=1, max_size=30)),
            st.tuples(st.just("--config"), st.text(min_size=1, max_size=30)),
            st.tuples(st.just("--enable"), st.text(min_size=1, max_size=20)),
            st.tuples(st.just("--disable"), st.text(min_size=1, max_size=20)),
        )

    @staticmethod
    def subcommands():
        """Generate valid subcommands."""
        return st.sampled_from(
            [
                "exec",
                "e",
                "resume",
                "review",
                "mcp",
                "mcp-server",
                "app-server",
                "login",
                "logout",
                "completion",
                "sandbox",
                "debug",
                "apply",
                "a",
                "cloud",
                "features",
                "help",
            ]
        )

    @staticmethod
    def positional_args():
        """Generate realistic positional arguments (prompts)."""
        return st.one_of(
            st.text(min_size=1, max_size=100),
            st.sampled_from(
                [
                    "analyze this code",
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
                "--sandbox=",
                # Unknown flags
                "--weird-unknown-flag",
                "--custom-option",
                # Unicode in flags
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
                TestPropertyBased.repeatable_flags().map(lambda t: list(t)),
                TestPropertyBased.positional_args(),
                TestPropertyBased.exotic_tokens(),
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
            spec = resolve_codex_args(args, None)
            assert isinstance(spec, CodexArgsSpec)
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

        spec1 = resolve_codex_args(data, None)

        # Skip if initial parse had errors
        assume(not spec1.has_errors())

        # Serialize to env string
        env_str = spec1.to_env_string()

        # Reparse
        spec2 = resolve_codex_args(None, env_str)

        # Should not introduce new errors
        assert not spec2.has_errors(), f"Roundtrip introduced errors: {spec2.errors}"

        # Key flags should be preserved
        assert spec2.is_exec == spec1.is_exec
        assert spec2.is_json == spec1.is_json
        assert spec2.subcommand == spec1.subcommand

        # Positional tokens should be preserved
        assert spec2.positional_tokens == spec1.positional_tokens

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_update_preserves_unrelated_fields(self, valid_token_sequence):
        """PROPERTY: update() operations don't lose unrelated data.

        Critical for maintaining parser state during transformations.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_codex_args(data, None)
        assume(not spec.has_errors())

        # Test json_output toggle
        if not spec.is_json:
            updated = spec.update(json_output=True)

            # Model flag should survive
            if spec.has_flag(["--model", "-m"]):
                assert updated.has_flag(["--model", "-m"]), "json toggle lost --model flag"

            # Sandbox flag should survive
            if spec.has_flag(["--sandbox", "-s"]):
                assert updated.has_flag(["--sandbox", "-s"]), "json toggle lost --sandbox flag"

            # Positionals should survive
            assert updated.positional_tokens == spec.positional_tokens, "json toggle changed positionals"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=150, deadline=None)
    def test_flag_detection_consistency(self, valid_token_sequence):
        """PROPERTY: has_flag() and get_flag_value() agree.

        If has_flag returns True, get_flag_value should return non-None for value flags.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_codex_args(data, None)
        assume(not spec.has_errors())

        # Test known value flags
        for flag in ["--model", "--sandbox", "-C", "-o"]:
            if spec.has_flag([flag]):
                value = spec.get_flag_value(flag)
                if not spec.has_errors():
                    assert value is not None, f"has_flag({flag}) = True but get_flag_value({flag}) = None"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_double_dash_boundary_respected(self, valid_token_sequence):
        """PROPERTY: -- separator prevents flag interpretation after it.

        Critical for allowing flag-like strings as positional arguments.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        # Insert -- and flag-like positional
        modified = list(data) + ["--", "--not-a-flag", "--json", "--model"]

        spec = resolve_codex_args(modified, None)

        # Flags after -- should be positional
        assert "--not-a-flag" in spec.positional_tokens
        assert "--json" in spec.positional_tokens
        assert "--model" in spec.positional_tokens

        # has_flag should not detect them (after --)
        assert not spec.has_flag(["--not-a-flag"])

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
    def test_prompt_update_preserves_flags(self, valid_token_sequence):
        """PROPERTY: Updating prompt doesn't affect flags.

        Ensures update(prompt=...) only changes positional, not flags.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_codex_args(data, None)
        assume(not spec.has_errors())

        # Skip if there are no positional tokens - prompt update behavior
        # is different when adding vs replacing
        assume(len(spec.positional_tokens) > 0)

        # Update prompt
        updated = spec.update(prompt="new prompt")

        # Flags should be unchanged
        assert updated.is_exec == spec.is_exec
        assert updated.is_json == spec.is_json

        if spec.has_flag(["--model", "-m"]):
            assert updated.has_flag(["--model", "-m"])

        if spec.has_flag(["--sandbox", "-s"]):
            assert updated.has_flag(["--sandbox", "-s"])

        # Prompt should be updated
        assert "new prompt" in updated.positional_tokens

    @given(st.lists(st.text(max_size=50), min_size=1, max_size=20))
    @settings(max_examples=150, deadline=None)
    def test_error_messages_reference_problematic_flag(self, args):
        """PROPERTY: Every error message contains the problematic flag name.

        Ensures error messages are actionable and reference what went wrong.
        """
        spec = resolve_codex_args(args, None)

        if spec.has_errors():
            for error in spec.errors:
                # Error should be non-empty and informative
                assert len(error) > 10, f"Error message too short: {error!r}"

                # Common error patterns should reference the flag
                if "requires a value" in error:
                    assert any(token.startswith("-") for token in error.split()), (
                        f"Error about missing value doesn't reference flag: {error!r}"
                    )

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
        spec = resolve_codex_args(combined, None)
        assert isinstance(spec, CodexArgsSpec)

        # Unknown flags should be preserved in clean_tokens
        for token in exotic:
            if token and token.startswith("--") and token not in ["--model=", "--sandbox="]:
                # Unknown flags should appear somewhere (clean_tokens or positional)
                assert token in spec.clean_tokens or token in spec.positional_tokens, f"Exotic token {token!r} was lost"

    @given(
        base_args=st.lists(st.text(max_size=50), min_size=0, max_size=10),
        new_json=st.booleans(),
    )
    @settings(max_examples=100, deadline=None)
    def test_json_toggle_idempotent(self, base_args, new_json):
        """PROPERTY: Toggling json twice is idempotent.

        update(json_output=X).update(json_output=X) == update(json_output=X)
        """
        spec = resolve_codex_args(base_args, None)
        assume(not spec.has_errors())

        once = spec.update(json_output=new_json)
        twice = once.update(json_output=new_json)

        assert once.is_json == twice.is_json
        assert once.positional_tokens == twice.positional_tokens

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_exec_mode_detection_consistent(self, valid_token_sequence):
        """PROPERTY: is_exec is True iff subcommand is exec/e.

        Ensures exec mode detection is consistent with subcommand parsing.
        """
        data = valid_token_sequence.draw(TestPropertyBased.valid_token_sequence())

        spec = resolve_codex_args(data, None)

        if spec.subcommand in ("exec", "e"):
            assert spec.is_exec, "Subcommand exec/e but is_exec is False"
        else:
            assert not spec.is_exec, f"is_exec True but subcommand is {spec.subcommand}"

    @given(valid_token_sequence=st.data())
    @settings(max_examples=100, deadline=None)
    def test_repeatable_flags_accumulate(self, valid_token_sequence):
        """PROPERTY: Repeatable flags (-c, --config, etc.) accumulate values.

        Multiple -c flags should produce a list of values.
        """
        # Generate args with multiple -c flags
        data = ["-c", "first=1", "-c", "second=2", "-c", "third=3"]

        spec = resolve_codex_args(data, None)
        assume(not spec.has_errors())

        values = spec.get_flag_value("-c")
        assert isinstance(values, list), "-c should return a list"
        assert len(values) == 3, f"Expected 3 -c values, got {len(values)}"
        assert "first=1" in values
        assert "second=2" in values
        assert "third=3" in values


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
