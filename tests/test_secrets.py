"""Secrets in Credential Manager: config lookup order, import/cleanup, set, and Gmail sign-in storage."""

import json
from pathlib import Path

import pytest

from jarvis.core import secrets_cli
from jarvis.core.config import _KEYS, load_config, secret_source
from jarvis.integrations import credentials
from jarvis.integrations.credentials import MemoryStore

ENV = (
    "OPENAI_API_KEY=sk-from-env\nELEVENLABS_API_KEY=el-from-env\nGOOGLE_SEARCH_API_KEY=AIza-old\n"
    "JARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\nJARVIS_MAX_OUTPUT_TOKENS=100\nJARVIS_LLM_TIMEOUT_SECONDS=10\n"
    "JARVIS_HISTORY_TURNS=3\nJARVIS_FILE_SEARCH_DIRS=C:\\Users\\Ahmed\\Documents\\Files for JARVIS to Access\n"
    "JARVIS_SEARCH_PROVIDER=tavily\nTAVILY_API_KEY=tvly-from-env\n"
)


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*_KEYS, "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(ENV.replace("JARVIS_FILE_SEARCH_DIRS=C:\\Users\\Ahmed\\Documents\\Files for JARVIS to Access\n", ""),
                                   encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "google_client.json").write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com",
                                                                        "client_secret": "GOCSPX-secret"}}), encoding="utf-8")
    (data / "google_token.json").write_text(json.dumps({"refresh_token": "1//refresh", "token": "ya29.short"}),
                                            encoding="utf-8")
    return tmp_path


# --- lookup order --------------------------------------------------------------------------------


def test_credential_manager_wins_over_env_file(project: Path, isolated_secret_store: MemoryStore) -> None:
    assert load_config(project_root=project).llm_api_key == "sk-from-env"
    isolated_secret_store.set("OPENAI_API_KEY", "sk-from-vault")
    assert load_config(project_root=project).llm_api_key == "sk-from-vault"
    assert secret_source("OPENAI_API_KEY", project / ".env") == "Windows Credential Manager"
    assert secret_source("TAVILY_API_KEY", project / ".env") == ".env (plain text)"


def test_environment_variable_wins_over_everything(project: Path, isolated_secret_store: MemoryStore,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_secret_store.set("OPENAI_API_KEY", "sk-from-vault")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")
    assert load_config(project_root=project).llm_api_key == "sk-from-environment"


def test_non_secret_settings_never_read_from_vault(project: Path, isolated_secret_store: MemoryStore) -> None:
    isolated_secret_store.set("JARVIS_LLM_MODEL", "sneaky")  # only named secrets are looked up in the vault
    assert load_config(project_root=project).llm_model == "m"


# --- import --------------------------------------------------------------------------------------


def config_for(project: Path):  # type: ignore[no-untyped-def]
    return load_config(project_root=project)


def test_import_copies_everything_and_keeps_files_if_declined(project: Path, isolated_secret_store: MemoryStore) -> None:
    code = secrets_cli.import_secrets(config_for(project), project / ".env", isolated_secret_store, ask=lambda q: "n")
    assert code == 0
    assert isolated_secret_store.values == {
        "OPENAI_API_KEY": "sk-from-env", "ELEVENLABS_API_KEY": "el-from-env", "TAVILY_API_KEY": "tvly-from-env",
        "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "GOCSPX-secret",
        "GOOGLE_REFRESH_TOKEN": "1//refresh"}
    assert "sk-from-env" in (project / ".env").read_text() and (project / "data" / "google_token.json").exists()


def test_import_cleanup_blanks_only_secret_lines_and_deletes_google_files(project: Path, isolated_secret_store: MemoryStore) -> None:
    before = (project / ".env").read_text(encoding="utf-8")
    secrets_cli.import_secrets(config_for(project), project / ".env", isolated_secret_store, ask=lambda q: "y")
    after = (project / ".env").read_text(encoding="utf-8")
    for name in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "TAVILY_API_KEY", "GOOGLE_SEARCH_API_KEY"):
        assert f"{name}=\n" in after
    assert "sk-from-env" not in after and "AIza-old" not in after
    assert [l for l in after.splitlines() if l.startswith("JARVIS_")] == [l for l in before.splitlines() if l.startswith("JARVIS_")]
    assert not (project / "data" / "google_client.json").exists() and not (project / "data" / "google_token.json").exists()
    assert load_config(project_root=project).llm_api_key == "sk-from-env"  # still works, now from the vault


def test_import_stops_without_removing_anything_on_vault_error(project: Path) -> None:
    class Broken(MemoryStore):
        def set(self, name: str, value: str) -> None:
            raise credentials.SecretStoreError(f"Could not save {name}.")

    code = secrets_cli.import_secrets(config_for(project), project / ".env", Broken(), ask=lambda q: "y")
    assert code == 1 and "sk-from-env" in (project / ".env").read_text()


def test_blank_env_values_keeps_other_lines_byte_for_byte(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENAI_API_KEY=sk-1\nJARVIS_FILE_SEARCH_DIRS=C:\\Users\\A B\\Docs\nOPENAI_API_KEY_OLD=x\n",
                   encoding="utf-8")
    secrets_cli.blank_env_values(env, ["OPENAI_API_KEY"])
    assert env.read_text(encoding="utf-8") == ("# comment\nOPENAI_API_KEY=\nJARVIS_FILE_SEARCH_DIRS=C:\\Users\\A B\\Docs\n"
                                               "OPENAI_API_KEY_OLD=x\n")


# --- set (restore after a reinstall) ---------------------------------------------------------------


def test_set_prompts_hidden_and_enter_keeps_current(isolated_secret_store: MemoryStore) -> None:
    isolated_secret_store.set("TAVILY_API_KEY", "tvly-keep")
    answers = iter(["sk-new", "", "", "cid-new", ""])
    prompts: list[str] = []
    secrets_cli.set_secrets(isolated_secret_store, lambda prompt: prompts.append(prompt) or next(answers))
    assert isolated_secret_store.values == {"TAVILY_API_KEY": "tvly-keep", "OPENAI_API_KEY": "sk-new",
                                            "GOOGLE_CLIENT_ID": "cid-new"}
    assert any("TAVILY_API_KEY (set)" in p for p in prompts) and any("OPENAI_API_KEY (not set)" in p for p in prompts)


# --- Gmail sign-in kept in the vault ---------------------------------------------------------------


def test_gmail_client_config_from_vault_and_save_keeps_old_token_file(tmp_path: Path, isolated_secret_store: MemoryStore) -> None:
    from jarvis.integrations.gmail import AUTH_URI, TOKEN_URI, GmailClient

    token_file = tmp_path / "google_token.json"
    token_file.write_text("{}", encoding="utf-8")
    isolated_secret_store.values.update({"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec"})
    client = GmailClient(tmp_path / "missing_client.json", token_file, isolated_secret_store)
    assert client.configured
    assert client._client_config() == {"installed": {"client_id": "cid", "client_secret": "sec", "auth_uri": AUTH_URI,
                                                     "token_uri": TOKEN_URI, "redirect_uris": ["http://localhost"]}}

    class FakeCreds:
        refresh_token, client_id, client_secret = "1//new-refresh", "cid", "sec"

    client._save(FakeCreds())  # type: ignore[arg-type]
    assert isolated_secret_store.get("GOOGLE_REFRESH_TOKEN") == "1//new-refresh"
    assert token_file.read_text(encoding="utf-8") == "{}"  # never deleted or rewritten automatically


def test_windows_store_refuses_oversized_secret() -> None:
    store = credentials.WindowsCredentialStore.__new__(credentials.WindowsCredentialStore)
    with pytest.raises(credentials.SecretStoreError, match="too long"):
        store.set("X", "a" * 2000)  # 4,000 bytes as UTF-16 > 2,560
