"""Logging to `logs/`: an app log and a JSON-lines usage log.

- logs/jarvis.log   : application log (rotating)
- logs/usage.jsonl  : one JSON object per provider request (tokens, latency, ...)
- logs/audit.jsonl  : one JSON object per tool call (tool, arguments, decision, status)

Known secret values are redacted from every record before it is written.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

USAGE_LOGGER_NAME = "jarvis.usage"
AUDIT_LOGGER_NAME = "jarvis.audit"
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

    for name, filename in ((USAGE_LOGGER_NAME, "usage.jsonl"), (AUDIT_LOGGER_NAME, "audit.jsonl")):
        jsonl = logging.getLogger(name)
        for handler in list(jsonl.handlers):
            jsonl.removeHandler(handler)
            handler.close()
        jsonl.setLevel(logging.INFO)
        jsonl.propagate = False  # these records go only to their own .jsonl file
        jsonl.addHandler(_file_handler(logs_dir / filename, "%(message)s", secrets))


_usage_observers: list[Callable[[dict[str, Any]], None]] = []


def add_usage_observer(observer: Callable[[dict[str, Any]], None]) -> None:
    """Also send every usage record to `observer` (e.g. the repeated-failure watcher). Never raises."""
    _usage_observers.append(observer)


def _log_jsonl(logger_name: str, record: dict[str, Any]) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    logging.getLogger(logger_name).info(json.dumps(entry, ensure_ascii=False))
    if logger_name == USAGE_LOGGER_NAME:
        for observer in list(_usage_observers):
            try:
                observer(entry)
            except Exception:
                logging.getLogger(__name__).exception("Usage observer failed")


def log_usage(record: dict[str, Any]) -> None:
    """Append one usage record (with a UTC timestamp) to logs/usage.jsonl."""
    _log_jsonl(USAGE_LOGGER_NAME, record)


def log_audit(record: dict[str, Any]) -> None:
    """Append one tool-call record (with a UTC timestamp) to logs/audit.jsonl."""
    _log_jsonl(AUDIT_LOGGER_NAME, record)
