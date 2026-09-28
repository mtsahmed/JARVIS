"""Secrets in Windows Credential Manager (via keyring). The only module that imports `keyring`.

Windows encrypts these entries with the user's sign-in (DPAPI): only this Windows user can read them,
and they are stored for this computer only (never roam). A normal password change keeps them; a
Windows reinstall or a forced password reset loses them -> restore with `jarvis --secrets set` from
the master copy in the owner's password manager (owner decision, Phase 6).

Limitation (Windows): a stored secret may be at most 2,560 bytes, so only small values are kept here
(API keys, Google client ID/secret, Google refresh token), never whole token files.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

SERVICE = "Jarvis"
MAX_SECRET_BYTES = 2560  # CRED_MAX_CREDENTIAL_BLOB_SIZE (5*512)

# Every secret Jarvis uses, with what it is for (shown by `jarvis --secrets status`).
SECRET_NAMES = {
    "OPENAI_API_KEY": "OpenAI (chat, speech recognition, memory recall)",
    "ELEVENLABS_API_KEY": "ElevenLabs (briefing voice)",
    "TAVILY_API_KEY": "Tavily (web search)",
    "GOOGLE_CLIENT_ID": "Google sign-in: OAuth client ID",
    "GOOGLE_CLIENT_SECRET": "Google sign-in: OAuth client secret",
    "GOOGLE_REFRESH_TOKEN": "Google sign-in: Gmail read-only refresh token",
}


class SecretStoreError(Exception):
    """Credential Manager could not be used. The message never contains a secret."""


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class WindowsCredentialStore:
    def __init__(self) -> None:
        try:
            from keyring.backends.Windows import WinVaultKeyring

            self._backend = WinVaultKeyring()
            self._backend.persist = "local machine"  # CRED_PERSIST_LOCAL_MACHINE: never roams off this PC
        except Exception as e:
            logger.error("Windows Credential Manager unavailable: %s", type(e).__name__)
            raise SecretStoreError("Windows Credential Manager is not available.") from None

    def get(self, name: str) -> str | None:
        try:
            return self._backend.get_password(SERVICE, name)
        except Exception as e:
            logger.error("Could not read %s from Credential Manager: %s", name, type(e).__name__)
            return None

    def set(self, name: str, value: str) -> None:
        if len(value.encode("utf-16-le")) > MAX_SECRET_BYTES:  # keyring stores text as UTF-16
            raise SecretStoreError(f"{name} is too long for Windows Credential Manager.")
        try:
            self._backend.set_password(SERVICE, name, value)
        except Exception as e:
            logger.error("Could not save %s to Credential Manager: %s", name, type(e).__name__)
            raise SecretStoreError(f"Could not save {name} to Windows Credential Manager.") from None
        if self.get(name) != value:  # read back: never trust a write we haven't verified
            raise SecretStoreError(f"{name} did not read back correctly from Credential Manager.")

    def delete(self, name: str) -> None:
        try:
            self._backend.delete_password(SERVICE, name)
        except Exception:
            pass  # already absent


class MemoryStore:
    """In-memory store for tests (and when Credential Manager is unavailable)."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


_default: SecretStore | None = None


def default_store() -> SecretStore:
    """The store Jarvis uses: Windows Credential Manager, or an empty in-memory store if unavailable."""
    global _default
    if _default is None:
        try:
            _default = WindowsCredentialStore()
        except SecretStoreError:
            _default = MemoryStore()
    return _default


def set_default_store(store: SecretStore | None) -> None:
    """Replace the default store (tests use an in-memory one so they never touch the real vault)."""
    global _default
    _default = store
