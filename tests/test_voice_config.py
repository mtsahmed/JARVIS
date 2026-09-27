from pathlib import Path

import pytest

from jarvis.core.config import _VOICE_KEYS, ConfigError, load_voice_config

VOICE_ENV = """\
JARVIS_DATA_DIR=data
JARVIS_STT_PROVIDER=faster-whisper
JARVIS_STT_MODEL=base.en
JARVIS_STT_COMPUTE_TYPE=int8
JARVIS_STT_BEAM_SIZE=5
JARVIS_TTS_PROVIDER=sapi
JARVIS_TTS_RATE=
JARVIS_PTT_KEY=ctrl_r
JARVIS_MAX_RECORD_SECONDS=30
ELEVENLABS_API_KEY=el-test-secret
JARVIS_ELEVENLABS_USE_FOR=
JARVIS_ELEVENLABS_VOICE_ID=voice123
JARVIS_ELEVENLABS_MODEL=eleven_flash_v2_5
JARVIS_ELEVENLABS_MIN_CHARS=1000
"""


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _VOICE_KEYS:
        monkeypatch.delenv(key, raising=False)


def load(tmp_path: Path, text: str = VOICE_ENV):
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return load_voice_config(project_root=tmp_path)


def test_loads_voice_settings(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert cfg.stt_model == "base.en"
    assert cfg.stt_beam_size == 5
    assert cfg.ptt_key == "ctrl_r"
    assert cfg.tts_rate is None  # blank = Windows default
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.models_dir == tmp_path / "data" / "models"
    assert cfg.elevenlabs_use_for == frozenset()


def test_elevenlabs_key_hidden_and_listed_as_secret(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert "el-test-secret" not in repr(cfg)
    assert cfg.secrets == ("el-test-secret",)


def test_elevenlabs_settings_optional_when_disabled(tmp_path: Path) -> None:
    env = "\n".join(line for line in VOICE_ENV.splitlines() if "ELEVENLABS" not in line)
    cfg = load(tmp_path, env)
    assert cfg.elevenlabs_api_key is None
    assert cfg.secrets == ()


def test_elevenlabs_settings_required_when_enabled(tmp_path: Path) -> None:
    env = VOICE_ENV.replace("JARVIS_ELEVENLABS_USE_FOR=", "JARVIS_ELEVENLABS_USE_FOR=briefing")
    env = env.replace("JARVIS_ELEVENLABS_VOICE_ID=voice123", "JARVIS_ELEVENLABS_VOICE_ID=")
    with pytest.raises(ConfigError, match="JARVIS_ELEVENLABS_VOICE_ID"):
        load(tmp_path, env)


def test_use_for_parsed_as_set(tmp_path: Path) -> None:
    env = VOICE_ENV.replace("JARVIS_ELEVENLABS_USE_FOR=", "JARVIS_ELEVENLABS_USE_FOR=Briefing, reply")
    assert load(tmp_path, env).elevenlabs_use_for == {"briefing", "reply"}


def test_unknown_use_for_rejected(tmp_path: Path) -> None:
    env = VOICE_ENV.replace("JARVIS_ELEVENLABS_USE_FOR=", "JARVIS_ELEVENLABS_USE_FOR=everything")
    with pytest.raises(ConfigError, match="JARVIS_ELEVENLABS_USE_FOR"):
        load(tmp_path, env)


@pytest.mark.parametrize(("key", "value"), [("JARVIS_STT_PROVIDER", "other"), ("JARVIS_TTS_PROVIDER", "other")])
def test_unknown_provider_rejected(tmp_path: Path, key: str, value: str) -> None:
    env = "\n".join(f"{key}={value}" if line.startswith(key + "=") else line for line in VOICE_ENV.splitlines())
    with pytest.raises(ConfigError, match=key):
        load(tmp_path, env)


def test_missing_stt_model_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="JARVIS_STT_MODEL"):
        load(tmp_path, VOICE_ENV.replace("JARVIS_STT_MODEL=base.en\n", ""))


def test_cloud_stt_with_local_fallback(tmp_path: Path) -> None:
    env = VOICE_ENV.replace("JARVIS_STT_PROVIDER=faster-whisper", "JARVIS_STT_PROVIDER=openai")
    env = env.replace("JARVIS_STT_MODEL=base.en", "JARVIS_STT_MODEL=gpt-4o-mini-transcribe\nJARVIS_STT_FALLBACK_MODEL=base.en")
    cfg = load(tmp_path, env)
    assert (cfg.stt_provider, cfg.stt_model, cfg.stt_fallback_model) == ("openai", "gpt-4o-mini-transcribe", "base.en")


def test_fallback_model_optional(tmp_path: Path) -> None:
    assert load(tmp_path).stt_fallback_model is None
