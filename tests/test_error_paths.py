"""Error paths of the adapters (Phase 6D coverage): each failure becomes a clear message, never a crash
and never a leaked secret."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2  # the HTTP library the installed openai SDK uses
import openai
import pytest

from jarvis.integrations import credentials
from jarvis.llm.base import LLMError, Message

HELLO = [Message("user", "hi")]
REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


# --- Windows Credential Manager adapter (fake keyring backend) -----------------------------------------


class FakeBackend:
    def __init__(self, corrupt: bool = False, broken: bool = False) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.corrupt, self.broken = corrupt, broken

    def get_password(self, service: str, name: str) -> str | None:
        if self.broken:
            raise OSError("vault locked")
        value = self.data.get((service, name))
        return value + "x" if (value and self.corrupt) else value

    def set_password(self, service: str, name: str, value: str) -> None:
        if self.broken:
            raise OSError("vault locked")
        self.data[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.data[(service, name)]


def store_with(backend: FakeBackend) -> credentials.WindowsCredentialStore:
    store = credentials.WindowsCredentialStore.__new__(credentials.WindowsCredentialStore)
    store._backend = backend
    return store


def test_vault_write_is_read_back_and_verified() -> None:
    store = store_with(FakeBackend())
    store.set("OPENAI_API_KEY", "sk-1")
    assert store.get("OPENAI_API_KEY") == "sk-1" and store._backend.data == {("Jarvis", "OPENAI_API_KEY"): "sk-1"}
    with pytest.raises(credentials.SecretStoreError, match="did not read back"):
        store_with(FakeBackend(corrupt=True)).set("OPENAI_API_KEY", "sk-1")


def test_vault_failures_are_contained() -> None:
    broken = store_with(FakeBackend(broken=True))
    assert broken.get("OPENAI_API_KEY") is None  # read failure -> treated as absent (falls back to .env)
    with pytest.raises(credentials.SecretStoreError, match="Could not save OPENAI_API_KEY") as exc:
        broken.set("OPENAI_API_KEY", "sk-secret-value")
    assert "sk-secret-value" not in str(exc.value)
    store_with(FakeBackend()).delete("missing")  # deleting something absent is fine


def test_default_store_falls_back_when_vault_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> None:
        raise credentials.SecretStoreError("Windows Credential Manager is not available.")

    credentials.set_default_store(None)
    monkeypatch.setattr(credentials, "WindowsCredentialStore", unavailable)
    assert isinstance(credentials.default_store(), credentials.MemoryStore)


# --- logging to real files ----------------------------------------------------------------------------


def test_log_files_written_redacted_and_not_duplicated(tmp_path: Path) -> None:
    from jarvis.core.logging_setup import AUDIT_LOGGER_NAME, USAGE_LOGGER_NAME, log_audit, log_usage, setup_logging

    root = logging.getLogger()
    saved = list(root.handlers), root.level
    try:
        setup_logging(tmp_path / "logs", secrets=["sk-live-secret"])
        setup_logging(tmp_path / "logs", secrets=["sk-live-secret"])  # twice: handlers replaced, not doubled
        logging.getLogger("jarvis.test").info("key is %s", "sk-live-secret")
        log_usage({"kind": "llm", "total_tokens": 5})
        log_audit({"tool": "x", "args": {"note": "sk-live-secret"}})
        for name in (None, USAGE_LOGGER_NAME, AUDIT_LOGGER_NAME):
            for handler in logging.getLogger(name).handlers:
                handler.flush()
        app = (tmp_path / "logs" / "jarvis.log").read_text(encoding="utf-8")
        usage = (tmp_path / "logs" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
        audit = (tmp_path / "logs" / "audit.jsonl").read_text(encoding="utf-8")
        assert "key is [REDACTED]" in app and "sk-live-secret" not in app + audit
        assert len(usage) == 1 and json.loads(usage[0])["total_tokens"] == 5 and "ts" in json.loads(usage[0])
        assert len(logging.getLogger(USAGE_LOGGER_NAME).handlers) == 1
    finally:
        for name in (None, USAGE_LOGGER_NAME, AUDIT_LOGGER_NAME):
            for handler in list(logging.getLogger(name).handlers):
                logging.getLogger(name).removeHandler(handler)
                handler.close()
        root.handlers[:], root.level = saved[0], saved[1]


# --- Gmail sign-in expiry / network -------------------------------------------------------------------


@pytest.fixture
def gmail_client(isolated_secret_store: credentials.MemoryStore, tmp_path: Path):  # type: ignore[no-untyped-def]
    from jarvis.integrations.gmail import GmailClient

    isolated_secret_store.values.update({"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec",
                                         "GOOGLE_REFRESH_TOKEN": "1//refresh"})
    return GmailClient(tmp_path / "none.json", tmp_path / "none-token.json", isolated_secret_store)


def test_gmail_expired_sign_in_says_how_to_fix(gmail_client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import jarvis.integrations.gmail as gmail
    from google.auth.exceptions import RefreshError

    def expired(self, request: Any) -> None:
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(gmail.Credentials, "refresh", expired)
    with pytest.raises(gmail.GmailAuthError, match="expired. Run: jarvis --gmail-login"):
        gmail_client.important_unread()


def test_gmail_offline_is_clear(gmail_client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import jarvis.integrations.gmail as gmail
    from google.auth.exceptions import TransportError

    def offline(self, request: Any) -> None:
        raise TransportError("connection refused")

    monkeypatch.setattr(gmail.Credentials, "refresh", offline)
    with pytest.raises(gmail.GmailError, match="Could not reach Google"):
        gmail_client.important_unread()


def test_gmail_refreshed_credentials_reused(gmail_client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import jarvis.integrations.gmail as gmail

    refreshes: list[int] = []

    def refresh(self, request: Any) -> None:
        refreshes.append(1)
        self.token = "ya29.fresh"
        self.expiry = None  # no expiry known -> treated as valid

    monkeypatch.setattr(gmail.Credentials, "refresh", refresh)
    gmail_client._credentials()
    gmail_client._credentials()
    assert refreshes == [1]  # access token kept in memory, not refreshed on every call


def test_gmail_http_errors(gmail_client, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import jarvis.integrations.gmail as gmail

    class Session:
        def __init__(self, error: Exception | None = None, status: int = 500) -> None:
            self.error, self.status = error, status

        def request(self, *a: Any, **k: Any) -> Any:
            if self.error:
                raise self.error
            return SimpleNamespace(status_code=self.status, json=lambda: {})

    monkeypatch.setattr(gmail_client, "_credentials", lambda: object())
    monkeypatch.setattr(gmail, "AuthorizedSession", lambda c: Session(status=500))
    with pytest.raises(gmail.GmailError, match="HTTP 500"):
        gmail_client.important_unread()
    monkeypatch.setattr(gmail, "AuthorizedSession", lambda c: Session(error=ConnectionError("down")))
    with pytest.raises(gmail.GmailError, match="Could not reach Gmail"):
        gmail_client.important_unread()


# --- OpenAI embeddings and chat error mapping ---------------------------------------------------------


def test_embeddings_in_order_with_usage_logged() -> None:
    from jarvis.llm.openai_embeddings import OpenAIEmbedder

    usage: list[dict[str, Any]] = []
    embedder = OpenAIEmbedder("sk-test", "text-embedding-3-small", usage_sink=usage.append)
    response = SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0.2]), SimpleNamespace(index=0, embedding=[0.1])],
                               usage=SimpleNamespace(total_tokens=7))
    embedder._client = SimpleNamespace(embeddings=SimpleNamespace(create=lambda **k: response))  # type: ignore[assignment]
    assert embedder.embed(["a", "b"]) == [[0.1], [0.2]] and embedder.embed([]) == []
    assert usage == [{"kind": "embedding", "model": "text-embedding-3-small", "inputs": 2, "total_tokens": 7}]


def status_error(cls: type, code: int) -> Exception:
    return cls("secret echo sk-test", response=httpx2.Response(code, request=REQUEST), body=None)


@pytest.mark.parametrize(("error", "message"), [
    (lambda: status_error(openai.AuthenticationError, 401), "rejected the API key"),
    (lambda: status_error(openai.RateLimitError, 429), "rate limit or quota"),
    (lambda: status_error(openai.InternalServerError, 500), "HTTP 500"),
    (lambda: openai.APITimeoutError(request=REQUEST), "did not answer within"),
    (lambda: openai.APIConnectionError(request=REQUEST), "Could not reach OpenAI"),
])
def test_openai_errors_are_clear_and_leak_nothing(error: Any, message: str) -> None:
    from jarvis.llm.openai_adapter import OpenAIAdapter

    def fail(**request: Any) -> None:
        raise error()

    adapter = OpenAIAdapter("sk-test", "m", max_output_tokens=10, timeout_seconds=5)
    adapter._client = SimpleNamespace(responses=SimpleNamespace(create=fail))  # type: ignore[assignment]
    with pytest.raises(LLMError, match=message) as exc:
        adapter.generate(HELLO)
    assert "sk-test" not in str(exc.value) and exc.value.__cause__ is None


def test_embeddings_errors_are_clear() -> None:
    from jarvis.llm.openai_embeddings import OpenAIEmbedder

    def fail(**k: Any) -> None:
        raise openai.APIConnectionError(request=REQUEST)

    embedder = OpenAIEmbedder("sk-test", "m", usage_sink=lambda r: None)
    embedder._client = SimpleNamespace(embeddings=SimpleNamespace(create=fail))  # type: ignore[assignment]
    with pytest.raises(LLMError, match="Could not reach OpenAI for memory recall"):
        embedder.embed(["x"])


# --- secrets command dispatch --------------------------------------------------------------------------


def test_secrets_command_dispatch(tmp_path: Path, isolated_secret_store: credentials.MemoryStore,
                                  capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.core import secrets_cli
    from jarvis.core.config import _KEYS, load_config

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-x\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\n"
                                   "JARVIS_MAX_OUTPUT_TOKENS=1\nJARVIS_LLM_TIMEOUT_SECONDS=1\nJARVIS_HISTORY_TURNS=1\n",
                                   encoding="utf-8")
    config = load_config(project_root=tmp_path)
    assert secrets_cli.run("status", config, store=isolated_secret_store) == 0
    # the default store here is the in-memory test store standing in for "Credential Manager unavailable"
    assert secrets_cli.run("import", config) == 1
    assert "not available" in capsys.readouterr().out
