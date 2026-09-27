"""Logging to `logs/`: an app log and a JSON-lines usage log.

- logs/jarvis.log   : application log (rotating)
- logs/usage.jsonl  : one JSON object per provider request (tokens, latency, ...)

Known secret values are redacted from every record before it is written.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

USAGE_LOGGER_NAME = "jarvis.usage"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_REDACTED = "[REDACTED]"


class SecretRedactingFilter(logging.Filter):
    """Replace any known secret value in a log message with [REDACTED]."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(secret in message for secret in self._secrets):
            for secret in self._secrets:
                message = message.replace(secret, _REDACTED)
            record.msg, record.args = message, None
        return True


def _file_handler(path: Path, fmt: str, secrets: Iterable[str]) -> RotatingFileHandler:
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(SecretRedactingFilter(secrets))
    return handler


def setup_logging(logs_dir: Path, secrets: Iterable[str] = (), level: int = logging.INFO) -> None:
    """Configure file logging. Safe to call more than once (handlers are replaced)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    secrets = tuple(secrets)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    root.addHandler(
        _file_handler(logs_dir / "jarvis.log", "%(asctime)s %(levelname)s %(name)s: %(message)s", secrets)
    )

    usage = logging.getLogger(USAGE_LOGGER_NAME)
    for handler in list(usage.handlers):
        usage.removeHandler(handler)
        handler.close()
    usage.setLevel(logging.INFO)
    usage.propagate = False  # usage records go only to usage.jsonl
    usage.addHandler(_file_handler(logs_dir / "usage.jsonl", "%(message)s", secrets))


def log_usage(record: dict[str, Any]) -> None:
    """Append one usage record (with a UTC timestamp) to logs/usage.jsonl."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    logging.getLogger(USAGE_LOGGER_NAME).info(json.dumps(entry, ensure_ascii=False))
