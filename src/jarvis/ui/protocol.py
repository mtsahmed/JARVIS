"""HUD protocol v1 (docs/ui-protocol.md): server message envelopes and strict checks on client messages.

Every client message is untrusted. `parse_client` either returns a checked message, returns None for a
well-formed message of a type this phase does not handle (the caller ignores and logs it), or raises
ProtocolError. Error texts never repeat what the client sent (it could hold the session token).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024

# UI-2 is one-way: the page may only say hello and ping.
CLIENT_TYPES = frozenset({"hello", "ping"})
# Defined by the protocol for later phases (UI-3); ignored and logged for now.
LATER_CLIENT_TYPES = frozenset({"ptt", "text_input", "confirm_response"})
CLIENTS = frozenset({"hud", "widget"})

_ENVELOPE_KEYS = frozenset({"v", "type", "id", "ts", "data"})
_MAX_ID = 64
_MAX_TS = 40
_MAX_TOKEN = 256


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal  # the server closes the connection


@dataclass(frozen=True)
class ClientMessage:
    type: str
    id: str
    data: dict[str, Any]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def envelope(type: str, data: dict[str, Any] | None = None) -> str:
    """A server message as JSON text."""
    return json.dumps({"v": VERSION, "type": type, "id": str(uuid.uuid4()), "ts": now_utc(), "data": data or {}},
                      ensure_ascii=False)


def error_message(code: str, message: str, recoverable: bool = True) -> str:
    return envelope("error", {"code": code, "message": message, "recoverable": recoverable})


def parse_client(raw: str | bytes) -> ClientMessage | None:
    if not isinstance(raw, str):
        raise ProtocolError("bad_message", "Messages must be text.")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError("bad_message", "Message too large.")
    try:
        message = json.loads(raw)
    except ValueError:
        raise ProtocolError("bad_message", "Message is not valid JSON.") from None
    if not isinstance(message, dict) or set(message) != _ENVELOPE_KEYS:
        raise ProtocolError("bad_message", "Message must have exactly: v, type, id, ts, data.")
    version = message["v"]
    if type(version) is not int or version != VERSION:  # type() check: True is not a version
        raise ProtocolError("version_mismatch", f"Unsupported protocol version (server speaks {VERSION}).", fatal=True)
    kind, message_id, ts, data = message["type"], message["id"], message["ts"], message["data"]
    if not isinstance(kind, str) or not isinstance(message_id, str) or not 0 < len(message_id) <= _MAX_ID:
        raise ProtocolError("bad_message", "Bad type or id.")
    if not isinstance(ts, str) or len(ts) > _MAX_TS or not isinstance(data, dict):
        raise ProtocolError("bad_message", "Bad ts or data.")
    if kind not in CLIENT_TYPES:  # unknown, or a later phase's type (LATER_CLIENT_TYPES): ignored
        return None
    if kind == "hello":
        token, client = data.get("token"), data.get("client")
        if set(data) != {"token", "client"} or not isinstance(token, str) or not 0 < len(token) <= _MAX_TOKEN \
                or client not in CLIENTS:
            raise ProtocolError("bad_message", "hello needs a token and client (hud or widget).")
    elif data:  # ping
        raise ProtocolError("bad_message", "ping takes no data.")
    return ClientMessage(kind, message_id, data)
