"""Load Jarvis configuration from `.env` (and real environment variables).

Values are read with `dotenv_values`, so secrets are never copied into
`os.environ`. A real environment variable overrides the same key in `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# src/jarvis/core/config.py -> project root is three levels above this package.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Only the keys Phase 1 needs. Other secrets in .env are deliberately not loaded.
_KEYS = (
    "OPENAI_API_KEY",
    "JARVIS_LLM_PROVIDER",
    "JARVIS_LLM_MODEL",
    "JARVIS_MAX_OUTPUT_TOKENS",
    "JARVIS_LLM_TIMEOUT_SECONDS",
    "JARVIS_HISTORY_TURNS",
    "JARVIS_REASONING_EFFORT",
    "JARVIS_DATA_DIR",
    "JARVIS_FILE_SEARCH_DIRS",
)

# Secret key required for each supported LLM provider.
_PROVIDER_SECRET = {"openai": "OPENAI_API_KEY"}

# Accepted reasoning-effort values (from the openai SDK's ReasoningEffort type).
# Not every model supports every value.
_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    """Configuration is missing or invalid. Messages never contain secret values."""


@dataclass(frozen=True)
class Config:
    llm_provider: str
    llm_model: str
    max_output_tokens: int
    llm_timeout_seconds: float
    history_turns: int
    project_root: Path
    logs_dir: Path
    data_dir: Path  # local databases (reminders, ...); gitignored
    file_search_dirs: tuple[Path, ...]  # folders file search may index; empty = file search off
    # None = do not send; the provider uses its default.
    reasoning_effort: str | None
    # repr=False keeps the key out of repr()/str(), and therefore out of logs.
    llm_api_key: str = field(repr=False)

    @property
    def secrets(self) -> tuple[str, ...]:
        """All secret values, for log redaction."""
        return (self.llm_api_key,)


def _read_values(env_path: Path, keys: tuple[str, ...] = _KEYS) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if key in keys and value is not None:
                values[key] = value
    for key in keys:
        if key in os.environ:
            values[key] = os.environ[key]
    return {k: v.strip() for k, v in values.items() if v.strip()}


def _require(values: dict[str, str], key: str, env_path: Path) -> str:
    if key not in values:
        raise ConfigError(f"Missing required setting {key} (set it in {env_path}).")
    return values[key]


def _positive_number(values: dict[str, str], key: str, env_path: Path, kind: type) -> int | float:
    raw = _require(values, key, env_path)
    try:
        number = kind(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a {kind.__name__}, got {raw!r}.") from None
    if number <= 0:
        raise ConfigError(f"{key} must be greater than 0, got {raw!r}.")
    return number


def _resolve_dir(value: str, project_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _existing_dirs(value: str, env_path: Path) -> tuple[Path, ...]:
    r"""Semicolon-separated absolute folders, e.g. C:\Users\me\Documents;D:\Notes."""
    dirs = []
    for part in (p.strip() for p in value.split(";")):
        if not part:
            continue
        path = Path(part)
        if not path.is_absolute() or not path.is_dir():
            raise ConfigError(f"JARVIS_FILE_SEARCH_DIRS: {part!r} is not an existing absolute folder (see {env_path}).")
        dirs.append(path)
    return tuple(dirs)


def load_config(env_path: Path | None = None, project_root: Path = PROJECT_ROOT) -> Config:
    """Build a validated Config. Raises ConfigError with a clear message on problems."""
    env_path = env_path or project_root / ".env"
    values = _read_values(env_path)

    provider = _require(values, "JARVIS_LLM_PROVIDER", env_path).lower()
    if provider not in _PROVIDER_SECRET:
        supported = ", ".join(sorted(_PROVIDER_SECRET))
        raise ConfigError(f"Unsupported JARVIS_LLM_PROVIDER {provider!r} (supported: {supported}).")

    reasoning_effort = values.get("JARVIS_REASONING_EFFORT", "").lower() or None
    if reasoning_effort is not None and reasoning_effort not in _REASONING_EFFORTS:
        allowed = ", ".join(_REASONING_EFFORTS)
        raise ConfigError(f"JARVIS_REASONING_EFFORT must be one of: {allowed}; got {reasoning_effort!r}.")

    return Config(
        llm_provider=provider,
        llm_model=_require(values, "JARVIS_LLM_MODEL", env_path),
        max_output_tokens=int(_positive_number(values, "JARVIS_MAX_OUTPUT_TOKENS", env_path, int)),
        llm_timeout_seconds=float(_positive_number(values, "JARVIS_LLM_TIMEOUT_SECONDS", env_path, float)),
        history_turns=int(_positive_number(values, "JARVIS_HISTORY_TURNS", env_path, int)),
        project_root=project_root,
        logs_dir=project_root / "logs",
        data_dir=_resolve_dir(values.get("JARVIS_DATA_DIR", "data"), project_root),
        file_search_dirs=_existing_dirs(values.get("JARVIS_FILE_SEARCH_DIRS", ""), env_path),
        reasoning_effort=reasoning_effort,
        llm_api_key=_require(values, _PROVIDER_SECRET[provider], env_path),
    )


# --- Voice (Phase 2). Loaded only in voice mode, so text mode needs only the keys above. ---

_VOICE_KEYS = (
    "JARVIS_DATA_DIR",
    "JARVIS_STT_PROVIDER",
    "JARVIS_STT_MODEL",
    "JARVIS_STT_COMPUTE_TYPE",
    "JARVIS_STT_BEAM_SIZE",
    "JARVIS_STT_FALLBACK_MODEL",
    "JARVIS_TTS_PROVIDER",
    "JARVIS_TTS_RATE",
    "JARVIS_PTT_KEY",
    "JARVIS_MAX_RECORD_SECONDS",
    "ELEVENLABS_API_KEY",
    "JARVIS_ELEVENLABS_VOICE_ID",
    "JARVIS_ELEVENLABS_MODEL",
    "JARVIS_ELEVENLABS_USE_FOR",
    "JARVIS_ELEVENLABS_MIN_CHARS",
)

_STT_PROVIDERS = ("faster-whisper", "openai")
_TTS_PROVIDERS = ("sapi",)
# What ElevenLabs may be used for; everything else uses the local voice.
_ELEVENLABS_PURPOSES = ("briefing", "reply")


@dataclass(frozen=True)
class VoiceConfig:
    data_dir: Path
    models_dir: Path
    stt_provider: str
    stt_model: str
    stt_compute_type: str
    stt_beam_size: int
    # Local faster-whisper model used when a cloud STT provider fails or is offline; None = no fallback.
    stt_fallback_model: str | None
    tts_provider: str
    tts_rate: int | None  # None = SAPI default speed
    ptt_key: str
    max_record_seconds: float
    elevenlabs_use_for: frozenset[str]  # empty = ElevenLabs disabled
    elevenlabs_voice_id: str | None
    elevenlabs_model: str | None
    elevenlabs_min_chars: int
    elevenlabs_api_key: str | None = field(default=None, repr=False)

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.elevenlabs_api_key,) if self.elevenlabs_api_key else ()


def _choice(values: dict[str, str], key: str, env_path: Path, allowed: tuple[str, ...]) -> str:
    value = _require(values, key, env_path).lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of: {', '.join(allowed)}; got {value!r}.")
    return value


def load_voice_config(env_path: Path | None = None, project_root: Path = PROJECT_ROOT) -> VoiceConfig:
    """Build a validated VoiceConfig. Raises ConfigError with a clear message on problems."""
    env_path = env_path or project_root / ".env"
    values = _read_values(env_path, _VOICE_KEYS)

    data_dir = _resolve_dir(values.get("JARVIS_DATA_DIR", "data"), project_root)

    tts_rate = None
    if "JARVIS_TTS_RATE" in values:
        tts_rate = int(_positive_number(values, "JARVIS_TTS_RATE", env_path, int))

    use_for = frozenset(p.strip().lower() for p in values.get("JARVIS_ELEVENLABS_USE_FOR", "").split(",") if p.strip())
    unknown = use_for - set(_ELEVENLABS_PURPOSES)
    if unknown:
        allowed = ", ".join(_ELEVENLABS_PURPOSES)
        raise ConfigError(f"JARVIS_ELEVENLABS_USE_FOR allows: {allowed}; got {', '.join(sorted(unknown))}.")

    # ElevenLabs settings are read when present, but required only when it is enabled.
    elevenlabs_keys = ("ELEVENLABS_API_KEY", "JARVIS_ELEVENLABS_VOICE_ID", "JARVIS_ELEVENLABS_MODEL")
    if use_for:
        for key in (*elevenlabs_keys, "JARVIS_ELEVENLABS_MIN_CHARS"):
            _require(values, key, env_path)
    api_key, voice_id, el_model = (values.get(key) for key in elevenlabs_keys)
    min_chars = 0
    if "JARVIS_ELEVENLABS_MIN_CHARS" in values:
        min_chars = int(_positive_number(values, "JARVIS_ELEVENLABS_MIN_CHARS", env_path, int))

    return VoiceConfig(
        data_dir=data_dir,
        models_dir=data_dir / "models",
        stt_provider=_choice(values, "JARVIS_STT_PROVIDER", env_path, _STT_PROVIDERS),
        stt_model=_require(values, "JARVIS_STT_MODEL", env_path),
        stt_compute_type=_require(values, "JARVIS_STT_COMPUTE_TYPE", env_path),
        stt_beam_size=int(_positive_number(values, "JARVIS_STT_BEAM_SIZE", env_path, int)),
        stt_fallback_model=values.get("JARVIS_STT_FALLBACK_MODEL"),
        tts_provider=_choice(values, "JARVIS_TTS_PROVIDER", env_path, _TTS_PROVIDERS),
        tts_rate=tts_rate,
        ptt_key=_require(values, "JARVIS_PTT_KEY", env_path).lower(),
        max_record_seconds=float(_positive_number(values, "JARVIS_MAX_RECORD_SECONDS", env_path, float)),
        elevenlabs_use_for=use_for,
        elevenlabs_voice_id=voice_id,
        elevenlabs_model=el_model,
        elevenlabs_min_chars=min_chars,
        elevenlabs_api_key=api_key,
    )
