"""What ``claude-hybrid`` hands to ``claude``: the picker rows, the settings
file, the environment, and the session note."""

import json

from claude_code_hybrid import cli

BASE = {"HYBRID_UPSTREAM_URL": "https://upstream.example/api", "HYBRID_UPSTREAM_KEY": "k"}


def test_models_list_becomes_picker_rows_with_optional_labels():
    cfg = {**BASE, "HYBRID_MODELS": "kimi-k3=Kimi K3, deepseek-chat , glm-5.3=GLM 5.3,,"}
    rows = cli.picker_rows(cfg)
    assert [r["model"] for r in rows] == ["kimi-k3", "deepseek-chat", "glm-5.3"]
    assert rows[0]["label"] == "Kimi K3"
    assert "label" not in rows[1]
    assert all(r["description"] for r in rows)


def test_legacy_custom_model_joins_the_rows_once():
    cfg = {**BASE, "HYBRID_MODELS": "kimi-k3=Kimi K3", "HYBRID_CUSTOM_MODEL": "kimi-k3"}
    assert [r["model"] for r in cli.picker_rows(cfg)] == ["kimi-k3"]
    cfg = {**BASE, "HYBRID_MODELS": "glm-5.3", "HYBRID_CUSTOM_MODEL": "kimi-k3",
           "HYBRID_CUSTOM_MODEL_NAME": "Kimi"}
    rows = cli.picker_rows(cfg)
    assert [r["model"] for r in rows] == ["glm-5.3", "kimi-k3"]
    assert rows[1]["label"] == "Kimi"


def test_no_models_means_no_rows():
    assert cli.picker_rows(BASE) == []


def test_invocation_passes_the_picker_as_a_settings_file_and_the_session_note():
    cfg = {**BASE, "HYBRID_MODELS": "kimi-k3=Kimi K3,glm-5.3", "HYBRID_PORT": "8291"}
    argv, env = cli.claude_invocation(cfg, ["--continue"], {
        "ANTHROPIC_API_KEY": "sk-should-go", "ANTHROPIC_AUTH_TOKEN": "also-gone",
        "HYBRID_UPSTREAM_KEY": "never-for-claude", "PATH": "/bin",
    })
    assert argv[:2] == ["--append-system-prompt", cli.SESSION_NOTE]
    assert argv[2] == "--settings"
    settings = json.loads(open(argv[3], encoding="utf-8").read())
    assert [r["model"] for r in settings["modelPicker"]["options"]] == ["kimi-k3", "glm-5.3"]
    assert "replaceBuiltInOptions" not in settings["modelPicker"]  # Opus/Sonnet/Haiku stay
    assert argv[4:] == ["--continue"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8291"
    assert env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
    for gone in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HYBRID_UPSTREAM_KEY",
                 "ANTHROPIC_CUSTOM_MODEL_OPTION"):
        assert gone not in env
    assert env["PATH"] == "/bin"


def test_callers_own_settings_and_prompt_are_left_alone():
    cfg = {**BASE, "HYBRID_MODELS": "kimi-k3", "HYBRID_CUSTOM_MODEL": "kimi-k3"}
    argv, env = cli.claude_invocation(
        cfg, ["--settings", "mine.json", "--append-system-prompt", "mine"], {})
    assert argv == ["--settings", "mine.json", "--append-system-prompt", "mine"]
    # The one-slot env option still covers the custom model when we cannot add rows.
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "kimi-k3"


def test_without_models_the_legacy_env_option_is_used():
    cfg = {**BASE, "HYBRID_CUSTOM_MODEL": "kimi-k3", "HYBRID_CUSTOM_MODEL_NAME": "Kimi K3"}
    argv, env = cli.claude_invocation(cfg, [], {})
    # A single custom model still makes a picker row (rows skip the probe too).
    assert argv[2] == "--settings"
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" not in env


def test_no_upstream_models_at_all():
    argv, env = cli.claude_invocation(BASE, ["-p", "hi"], {})
    assert "--settings" not in argv
    assert argv == ["--append-system-prompt", cli.SESSION_NOTE, "-p", "hi"]
