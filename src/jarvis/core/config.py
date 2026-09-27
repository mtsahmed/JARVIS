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
    # None = do not send; the provider uses its default.
    reasoning_effort: str | None
    # repr=False keeps the key out of repr()/str(), and therefore out of logs.
    llm_api_key: str = field(repr=False)

    @property
    def secrets(self) -> tuple[str, ...]:
        """All secret values, for log redaction."""
        return (self.llm_api_key,)


def _read_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if key in _KEYS and value is not None:
                values[key] = value
    for key in _KEYS:
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
        reasoning_effort=reasoning_effort,
        llm_api_key=_require(values, _PROVIDER_SECRET[provider], env_path),
    )
