"""Tests for Gemini CLI settings management."""
import json

import pytest


def _read_json(path):
    return json.loads(path.read_text())


@pytest.fixture
def gemini_test_env(tmp_path, monkeypatch):
    """Set up isolated environment for Gemini settings tests.

    Sets HCOM_DIR so get_project_root() returns tmp_path/home,
    and .gemini settings go to tmp_path/home/.gemini/settings.json
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


def test_setup_gemini_hooks_installs_expected(gemini_test_env, monkeypatch):
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")

    assert gemini_settings.setup_gemini_hooks() is True

    settings_path = test_home / ".gemini" / "settings.json"
    settings = _read_json(settings_path)

    assert settings["tools"]["enableHooks"] is True
    # Note: skipNextSpeakerCheck no longer set (bug was fixed in Gemini CLI)

    hooks = settings["hooks"]
    # Always use hooksConfig.enabled (v0.26.0+ required)
    assert settings.get("hooksConfig", {}).get("enabled") is True
    assert hooks.get("enabled") is None  # Legacy schema should not be present
    for hook_type, expected_matcher, cmd_suffix, expected_timeout, _ in gemini_settings.GEMINI_HOOK_CONFIGS:
        assert hook_type in hooks
        assert isinstance(hooks[hook_type], list)
        assert len(hooks[hook_type]) == 1

        matcher_dict = hooks[hook_type][0]
        assert matcher_dict.get("matcher", "") == expected_matcher
        assert len(matcher_dict.get("hooks", [])) == 1

        hook = matcher_dict["hooks"][0]
        assert hook["type"] == "command"
        assert hook["name"] == f"hcom-{hook_type.lower()}"
        assert hook["timeout"] == expected_timeout
        assert hook["command"] == f"hcom {cmd_suffix}"

    assert gemini_settings.verify_gemini_hooks_installed() is True


def test_verify_gemini_hooks_detects_command_drift(gemini_test_env, monkeypatch):
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")
    assert gemini_settings.setup_gemini_hooks() is True

    settings_path = test_home / ".gemini" / "settings.json"
    settings = _read_json(settings_path)

    hook_type, _, cmd_suffix, _, _ = gemini_settings.GEMINI_HOOK_CONFIGS[0]
    settings["hooks"][hook_type][0]["hooks"][0]["command"] = f"uvx hcom {cmd_suffix}"
    settings_path.write_text(json.dumps(settings, indent=2))

    assert gemini_settings.verify_gemini_hooks_installed() is False


def test_remove_gemini_hooks_preserves_hooks_disabled(gemini_test_env):
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env

    settings_path = test_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {
        "tools": {"enableHooks": True},
        "model": {"skipNextSpeakerCheck": False},
        "hooks": {
            "disabled": ["keep-me"],
            "SessionStart": [{
                "matcher": "startup",
                "hooks": [{
                    "name": "hcom-sessionstart",
                    "type": "command",
                    "command": "hcom gemini-sessionstart",
                    "timeout": 5000,
                }],
            }],
            "BeforeAgent": [{
                "matcher": "*",
                "hooks": [
                    {
                        "name": "hcom-beforeagent",
                        "type": "command",
                        "command": "hcom gemini-beforeagent",
                        "timeout": 5000,
                    },
                    {
                        "name": "keep-other",
                        "type": "command",
                        "command": "echo hi",
                        "timeout": 1,
                    },
                ],
            }],
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2))

    assert gemini_settings.remove_gemini_hooks() is True

    updated = _read_json(settings_path)
    assert updated["hooks"]["disabled"] == ["keep-me"]
    assert updated["model"]["skipNextSpeakerCheck"] is False
    assert "SessionStart" not in updated["hooks"]
    assert "BeforeAgent" in updated["hooks"]
    remaining = updated["hooks"]["BeforeAgent"][0]["hooks"]
    assert len(remaining) == 1
    assert remaining[0]["name"] == "keep-other"


def test_verify_gemini_hooks_detects_missing_hooks_enabled(gemini_test_env, monkeypatch):
    """Test that verify returns False when hooksConfig.enabled is missing or false."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")

    # Setup hooks normally
    assert gemini_settings.setup_gemini_hooks() is True
    assert gemini_settings.verify_gemini_hooks_installed() is True

    settings_path = test_home / ".gemini" / "settings.json"
    settings = _read_json(settings_path)

    # Remove hooksConfig.enabled - verify should fail
    del settings["hooksConfig"]["enabled"]
    settings_path.write_text(json.dumps(settings, indent=2))
    assert gemini_settings.verify_gemini_hooks_installed() is False

    # Set hooksConfig.enabled to false - verify should fail
    settings["hooksConfig"]["enabled"] = False
    settings_path.write_text(json.dumps(settings, indent=2))
    assert gemini_settings.verify_gemini_hooks_installed() is False


def test_setup_gemini_hooks_preserves_user_tools_allowed(gemini_test_env, monkeypatch):
    """Test that setup preserves user's existing tools.allowed entries."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")

    settings_path = test_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # User has existing tools.allowed
    settings = {
        "tools": {
            "allowed": ["run_shell_command(git status)", "read_file"]
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2))

    assert gemini_settings.setup_gemini_hooks() is True

    updated = _read_json(settings_path)
    # User's allowed entries should still be there
    assert "run_shell_command(git status)" in updated["tools"]["allowed"]
    assert "read_file" in updated["tools"]["allowed"]
    # hcom permissions should be added
    for pattern in gemini_settings.GEMINI_HCOM_PERMISSIONS:
        assert pattern in updated["tools"]["allowed"]


def test_remove_gemini_hooks_cleans_up_legacy_enabled(gemini_test_env):
    """Test that removal cleans up legacy hooks.enabled (schema-invalid in 0.26.0+)."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env

    settings_path = test_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # User has legacy hooks.enabled and hcom hooks
    settings = {
        "tools": {"enableHooks": True},
        "hooks": {
            "enabled": True,  # Legacy schema - should be cleaned up
            "SessionStart": [{
                "matcher": "*",
                "hooks": [{
                    "name": "hcom-sessionstart",
                    "type": "command",
                    "command": "hcom gemini-sessionstart",
                    "timeout": 5000,
                }],
            }],
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2))

    assert gemini_settings.remove_gemini_hooks() is True

    updated = _read_json(settings_path)
    # Legacy hooks.enabled should be removed (schema-invalid in 0.26.0+)
    assert "hooks" not in updated or updated.get("hooks", {}).get("enabled") is None
    # hcom hooks should be gone
    assert "hooks" not in updated or "SessionStart" not in updated.get("hooks", {})


def test_setup_gemini_hooks_preserves_user_model_settings(gemini_test_env, monkeypatch):
    """Test that setup preserves user's other model settings."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")

    settings_path = test_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # User has various model settings
    settings = {
        "model": {
            "name": "gemini-2.5-pro",
            "maxSessionTurns": 50,
            "compressionThreshold": 0.3,
        },
        "ui": {
            "hideBanner": True,
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2))

    assert gemini_settings.setup_gemini_hooks() is True

    updated = _read_json(settings_path)
    # User's model settings should be preserved
    assert updated["model"]["name"] == "gemini-2.5-pro"
    assert updated["model"]["maxSessionTurns"] == 50
    assert updated["model"]["compressionThreshold"] == 0.3
    # Note: skipNextSpeakerCheck no longer set (bug was fixed in Gemini CLI)
    # Other settings preserved
    assert updated["ui"]["hideBanner"] is True


def test_set_hooks_enabled_sets_hooksconfig():
    """Test _set_hooks_enabled sets hooksConfig.enabled."""
    from hcom.tools.gemini import settings as gemini_settings

    settings = {}
    gemini_settings._set_hooks_enabled(settings)
    assert settings == {"hooksConfig": {"enabled": True}}
    assert gemini_settings._is_hooks_enabled(settings) is True


def test_set_hooks_enabled_cleans_up_legacy():
    """Test _set_hooks_enabled removes legacy hooks.enabled."""
    from hcom.tools.gemini import settings as gemini_settings

    # Simulate old schema with hook entries
    settings = {
        "hooks": {
            "enabled": True,
            "SessionStart": [{"matcher": "*", "hooks": []}],
        }
    }

    gemini_settings._set_hooks_enabled(settings)

    # Legacy hooks.enabled should be removed
    assert settings["hooks"].get("enabled") is None
    # hooksConfig.enabled should be set
    assert settings["hooksConfig"]["enabled"] is True
    # Hook entries should be preserved
    assert "SessionStart" in settings["hooks"]


def test_set_hooks_enabled_removes_false_value():
    """Test that legacy hooks.enabled=false is also removed."""
    from hcom.tools.gemini import settings as gemini_settings

    settings = {
        "hooks": {
            "enabled": False,
            "SessionStart": [],
        }
    }

    gemini_settings._set_hooks_enabled(settings)

    assert settings["hooks"].get("enabled") is None
    assert settings["hooksConfig"]["enabled"] is True


def test_ensure_hooks_enabled_migrates_legacy(gemini_test_env, monkeypatch):
    """Test that ensure_hooks_enabled migrates legacy hooks.enabled."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env

    settings_path = test_home / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Simulate legacy schema (from older hcom)
    settings = {
        "tools": {"enableHooks": True},
        "hooks": {
            "enabled": True,
            "SessionStart": [{"matcher": "*", "hooks": []}],
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2))

    # ensure_hooks_enabled should detect and migrate
    assert gemini_settings.ensure_hooks_enabled() is True

    updated = _read_json(settings_path)
    assert updated["hooks"].get("enabled") is None
    assert updated["hooksConfig"]["enabled"] is True
    # Hook entries preserved
    assert "SessionStart" in updated["hooks"]


def test_setup_rejects_old_gemini_version(gemini_test_env, monkeypatch):
    """Test setup_gemini_hooks returns False for Gemini < 0.26.0."""
    from hcom.tools.gemini import settings as gemini_settings

    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")
    monkeypatch.setattr(gemini_settings, "get_gemini_version", lambda: (0, 25, 0))

    # Should reject old version
    assert gemini_settings.setup_gemini_hooks() is False


def test_setup_accepts_new_gemini_version(gemini_test_env, monkeypatch):
    """Test setup_gemini_hooks works for Gemini >= 0.26.0."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")
    monkeypatch.setattr(gemini_settings, "get_gemini_version", lambda: (0, 26, 0))

    assert gemini_settings.setup_gemini_hooks() is True

    settings_path = test_home / ".gemini" / "settings.json"
    settings = _read_json(settings_path)

    assert settings["hooksConfig"]["enabled"] is True
    assert settings["hooks"].get("enabled") is None


def test_setup_accepts_unknown_version(gemini_test_env, monkeypatch):
    """Test setup_gemini_hooks proceeds with unknown version (optimistic)."""
    from hcom.tools.gemini import settings as gemini_settings

    test_home = gemini_test_env
    monkeypatch.setattr(gemini_settings, "build_hcom_command", lambda: "hcom")
    monkeypatch.setattr(gemini_settings, "get_gemini_version", lambda: None)

    # Unknown version proceeds optimistically
    assert gemini_settings.setup_gemini_hooks() is True

    settings_path = test_home / ".gemini" / "settings.json"
    settings = _read_json(settings_path)

    assert settings["hooksConfig"]["enabled"] is True
