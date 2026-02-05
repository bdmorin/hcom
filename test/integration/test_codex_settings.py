"""Tests for Codex CLI settings management (mirrors test_gemini_settings.py patterns)."""
import pytest


@pytest.fixture
def codex_test_env(tmp_path, monkeypatch):
    """Set up isolated environment for Codex settings tests.

    Sets HCOM_DIR so get_project_root() returns tmp_path/home,
    and .codex config goes to tmp_path/home/.codex/config.toml
    """
    test_home = tmp_path / "home"
    test_home.mkdir()

    # Set HCOM_DIR so get_project_root() returns test_home
    hcom_dir = test_home / ".hcom"
    hcom_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HCOM_DIR", str(hcom_dir))
    monkeypatch.setenv("HOME", str(test_home))  # For fallback paths

    # Clear path cache to pick up new HCOM_DIR
    from hcom.core.paths import clear_path_cache
    clear_path_cache()

    return test_home


def test_setup_codex_hooks_creates_config(codex_test_env, monkeypatch):
    """Test that setup creates config file with notify hook."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    assert codex_settings.setup_codex_hooks() is True

    config_path = test_home / ".codex" / "config.toml"
    assert config_path.exists()

    content = config_path.read_text()
    assert 'notify = ["hcom", "codex-notify"]' in content
    assert '# hcom integration' in content

    assert codex_settings.verify_codex_hooks_installed() is True


def test_setup_codex_hooks_inserts_before_section(codex_test_env, monkeypatch):
    """Test that notify is inserted before [section] headers (TOML requirement)."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[model]\nname = "gpt-4"\n\n[other]\nkey = "value"\n')

    assert codex_settings.setup_codex_hooks() is True

    content = config_path.read_text()
    # notify must appear BEFORE first [section]
    notify_pos = content.find('notify =')
    section_pos = content.find('[model]')
    assert notify_pos < section_pos, "notify must be before [model] section"

    assert codex_settings.verify_codex_hooks_installed() is True


def test_setup_codex_hooks_idempotent(codex_test_env, monkeypatch):
    """Test that running setup twice doesn't duplicate the hook."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    assert codex_settings.setup_codex_hooks() is True
    assert codex_settings.setup_codex_hooks() is True  # Second call

    config_path = test_home / ".codex" / "config.toml"
    content = config_path.read_text()

    # Should only have one notify line
    assert content.count('notify =') == 1


def test_setup_codex_hooks_refuses_existing_notify(codex_test_env, monkeypatch, capsys):
    """Test that setup refuses to override existing non-hcom notify hook."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('notify = ["some-other-tool", "arg"]\n')

    assert codex_settings.setup_codex_hooks() is False

    captured = capsys.readouterr()
    assert "already has a notify hook" in captured.err

    # Original content should be preserved
    content = config_path.read_text()
    assert 'some-other-tool' in content
    assert 'codex-notify' not in content


def test_verify_codex_hooks_detects_missing(codex_test_env, monkeypatch):
    """Test that verify returns False when hooks aren't installed."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env

    # No config file
    assert codex_settings.verify_codex_hooks_installed() is False

    # Config exists but no notify
    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[model]\nname = "test"\n')

    assert codex_settings.verify_codex_hooks_installed() is False


def test_verify_codex_hooks_detects_wrong_notify(codex_test_env, monkeypatch):
    """Test that verify returns False when notify exists but isn't hcom."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env

    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('notify = ["other-tool"]\n')

    assert codex_settings.verify_codex_hooks_installed() is False


def test_remove_codex_hooks_cleans_config(codex_test_env, monkeypatch):
    """Test that remove cleans up hcom hook from config."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    # Setup first
    assert codex_settings.setup_codex_hooks() is True
    assert codex_settings.verify_codex_hooks_installed() is True

    # Now remove
    assert codex_settings.remove_codex_hooks() is True

    config_path = test_home / ".codex" / "config.toml"
    content = config_path.read_text()
    assert 'codex-notify' not in content
    assert '# hcom integration' not in content


def test_remove_codex_hooks_preserves_other_config(codex_test_env, monkeypatch):
    """Test that remove preserves non-hcom config."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env

    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('''# My config
# hcom integration
notify = ["hcom", "codex-notify"]

[model]
name = "gpt-4"

[other]
key = "value"
''')

    assert codex_settings.remove_codex_hooks() is True

    content = config_path.read_text()
    assert 'codex-notify' not in content
    assert '[model]' in content
    assert 'name = "gpt-4"' in content
    assert '[other]' in content


def test_remove_codex_hooks_noop_when_no_config(codex_test_env, monkeypatch):
    """Test that remove succeeds even when no config exists."""
    from hcom.tools.codex import settings as codex_settings

    # No config file - should succeed (nothing to remove)
    assert codex_settings.remove_codex_hooks() is True


def test_setup_codex_hooks_uvx_command(codex_test_env, monkeypatch):
    """Test that setup uses uvx hcom when that's the detected command."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "uvx hcom")

    assert codex_settings.setup_codex_hooks() is True

    config_path = test_home / ".codex" / "config.toml"
    content = config_path.read_text()
    assert 'notify = ["uvx", "hcom", "codex-notify"]' in content


def test_setup_codex_hooks_creates_execpolicy(codex_test_env, monkeypatch):
    """Test that setup creates execpolicy rules for auto-approval."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    assert codex_settings.setup_codex_hooks(include_permissions=True) is True

    rules_file = test_home / ".codex" / "rules" / "hcom.rules"
    assert rules_file.exists()
    content = rules_file.read_text()
    assert "hcom" in content


def test_remove_codex_hooks_removes_execpolicy(codex_test_env, monkeypatch):
    """Test that removal also removes execpolicy rules."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")

    # Setup first
    assert codex_settings.setup_codex_hooks(include_permissions=True) is True
    rules_file = test_home / ".codex" / "rules" / "hcom.rules"
    assert rules_file.exists()

    # Remove
    assert codex_settings.remove_codex_hooks() is True
    assert not rules_file.exists()


def test_setup_codex_hooks_updates_stale_command(codex_test_env, monkeypatch):
    """Test that setup updates notify when command context changes."""
    from hcom.tools.codex import settings as codex_settings

    test_home = codex_test_env

    config_path = test_home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Old hcom hook with different command
    config_path.write_text('# hcom integration\nnotify = ["old-hcom", "codex-notify"]\n')

    # Now setup with new command
    monkeypatch.setattr(codex_settings, "build_hcom_command", lambda: "hcom")
    assert codex_settings.setup_codex_hooks() is True

    content = config_path.read_text()
    assert 'notify = ["hcom", "codex-notify"]' in content
    assert "old-hcom" not in content
