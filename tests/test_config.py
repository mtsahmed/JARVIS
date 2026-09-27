from pathlib import Path

import pytest

from jarvis.core.config import _KEYS, ConfigError, load_config

VALID_ENV = """\
OPENAI_API_KEY=sk-test-secret
JARVIS_LLM_PROVIDER=openai
JARVIS_LLM_MODEL=test-model
JARVIS_MAX_OUTPUT_TOKENS=500
JARVIS_LLM_TIMEOUT_SECONDS=15
JARVIS_HISTORY_TURNS=4
JARVIS_REASONING_EFFORT=low
"""


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real environment variables would override the test .env files.
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return tmp_path


def test_loads_valid_env(tmp_path: Path) -> None:
    cfg = load_config(project_root=write_env(tmp_path, VALID_ENV))
    assert cfg.llm_provider == "openai"
    assert cfg.llm_model == "test-model"
    assert cfg.max_output_tokens == 500
    assert cfg.llm_timeout_seconds == 15.0
    assert cfg.history_turns == 4
    assert cfg.reasoning_effort == "low"
    assert cfg.llm_api_key == "sk-test-secret"
    assert cfg.logs_dir == tmp_path / "logs"


def test_api_key_hidden_from_repr(tmp_path: Path) -> None:
    cfg = load_config(project_root=write_env(tmp_path, VALID_ENV))
    assert "sk-test-secret" not in repr(cfg)
    assert "sk-test-secret" not in str(cfg)


def test_missing_setting_names_key_not_value(tmp_path: Path) -> None:
    env = VALID_ENV.replace("JARVIS_LLM_MODEL=test-model\n", "")
    with pytest.raises(ConfigError, match="JARVIS_LLM_MODEL") as exc:
        load_config(project_root=write_env(tmp_path, env))
    assert "sk-test-secret" not in str(exc.value)


def test_blank_api_key_is_missing(tmp_path: Path) -> None:
    env = VALID_ENV.replace("OPENAI_API_KEY=sk-test-secret", "OPENAI_API_KEY=")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(project_root=write_env(tmp_path, env))


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_invalid_number_rejected(tmp_path: Path, value: str) -> None:
    env = VALID_ENV.replace("JARVIS_HISTORY_TURNS=4", f"JARVIS_HISTORY_TURNS={value}")
    with pytest.raises(ConfigError, match="JARVIS_HISTORY_TURNS"):
        load_config(project_root=write_env(tmp_path, env))


def test_unsupported_provider_rejected(tmp_path: Path) -> None:
    env = VALID_ENV.replace("JARVIS_LLM_PROVIDER=openai", "JARVIS_LLM_PROVIDER=other")
    with pytest.raises(ConfigError, match="Unsupported JARVIS_LLM_PROVIDER"):
        load_config(project_root=write_env(tmp_path, env))


def test_reasoning_effort_optional(tmp_path: Path) -> None:
    env = VALID_ENV.replace("JARVIS_REASONING_EFFORT=low\n", "")
    assert load_config(project_root=write_env(tmp_path, env)).reasoning_effort is None


def test_invalid_reasoning_effort_rejected(tmp_path: Path) -> None:
    env = VALID_ENV.replace("JARVIS_REASONING_EFFORT=low", "JARVIS_REASONING_EFFORT=turbo")
    with pytest.raises(ConfigError, match="JARVIS_REASONING_EFFORT"):
        load_config(project_root=write_env(tmp_path, env))


def test_environment_variable_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_LLM_MODEL", "override-model")
    assert load_config(project_root=write_env(tmp_path, VALID_ENV)).llm_model == "override-model"


def test_secrets_not_copied_into_os_environ(tmp_path: Path) -> None:
    import os

    load_config(project_root=write_env(tmp_path, VALID_ENV))
    assert "OPENAI_API_KEY" not in os.environ
