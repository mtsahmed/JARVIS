"""`jarvis --secrets status|import|set`: manage secrets in Windows Credential Manager. print() is the UI here.

Values are never printed or logged; only names and where each secret lives.
"""

from __future__ import annotations

import getpass
import json
import re
from pathlib import Path

from dotenv import dotenv_values

from jarvis.core.config import Config, secret_source
from jarvis.integrations.credentials import SECRET_NAMES, MemoryStore, SecretStore, SecretStoreError, default_store

API_KEYS = ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "TAVILY_API_KEY")
# Left in .env from earlier phases; Jarvis no longer uses them (Google Custom Search was replaced by Tavily).
UNUSED_ENV_SECRETS = ("GOOGLE_SEARCH_API_KEY", "GOOGLE_SEARCH_ENGINE_ID")


def run(action: str, config: Config, env_path: Path | None = None, store: SecretStore | None = None,
        ask=input, ask_secret=getpass.getpass) -> int:  # type: ignore[no-untyped-def]
    env_path = env_path or config.project_root / ".env"
    store = store or default_store()
    if isinstance(store, MemoryStore) and store is default_store() and action != "status":
        print("Windows Credential Manager is not available on this computer.")
        return 1
    if action == "status":
        return status(config, env_path)
    if action == "import":
        return import_secrets(config, env_path, store, ask)
    if action == "set":
        return set_secrets(store, ask_secret)
    print(f"Unknown action {action!r}. Use status, import or set.")
    return 1


def status(config: Config, env_path: Path) -> int:
    print("Where Jarvis's secrets are (values are never shown):")
    for name, purpose in SECRET_NAMES.items():
        print(f"  {name:22} {secret_source(name, env_path):28} {purpose}")
    for label, path in (("Google client file", config.gmail_client_file), ("Google token file", config.gmail_token_file)):
        print(f"  {label:22} {'present (plain text)' if path.is_file() else 'not present':28} {path}")
    env = dotenv_values(env_path) if env_path.is_file() else {}
    leftovers = [n for n in UNUSED_ENV_SECRETS if (env.get(n) or "").strip()]
    if leftovers:
        print(f"  No longer used, still in .env: {', '.join(leftovers)} (can be removed)")
    return 0


def import_secrets(config: Config, env_path: Path, store: SecretStore, ask) -> int:  # type: ignore[no-untyped-def]
    """Copy secrets from .env and the Google files into Credential Manager, verify, then offer cleanup."""
    env = dotenv_values(env_path) if env_path.is_file() else {}
    found: dict[str, str] = {n: v.strip() for n in API_KEYS if (v := env.get(n) or "").strip()}
    if config.gmail_client_file.is_file():
        try:
            installed = json.loads(config.gmail_client_file.read_text(encoding="utf-8"))["installed"]
            found["GOOGLE_CLIENT_ID"], found["GOOGLE_CLIENT_SECRET"] = installed["client_id"], installed["client_secret"]
        except (ValueError, KeyError):
            print(f"Skipped {config.gmail_client_file.name}: not a Desktop-app client JSON.")
    if config.gmail_token_file.is_file():
        try:
            if token := json.loads(config.gmail_token_file.read_text(encoding="utf-8")).get("refresh_token"):
                found["GOOGLE_REFRESH_TOKEN"] = token
        except ValueError:
            print(f"Skipped {config.gmail_token_file.name}: not valid JSON.")
    if not found:
        print("Nothing to import: no secrets found in .env or the Google files.")
        return 0

    for name, value in found.items():
        try:
            store.set(name, value)  # the Windows store reads each value back to verify it
        except SecretStoreError as e:
            print(f"Import stopped: {e} Nothing was removed.")
            return 1
        print(f"  saved to Credential Manager: {name}")

    leftovers = [n for n in UNUSED_ENV_SECRETS if (env.get(n) or "").strip()]
    plain = [n for n in (*API_KEYS, *leftovers) if n in found or n in leftovers]
    files = [p for p in (config.gmail_client_file, config.gmail_token_file) if p.is_file()]
    print("\nAll imported secrets were verified. Remove the plain-text copies now?")
    print(f"  blank in .env: {', '.join(plain) or '(none)'}")
    print(f"  delete files:  {', '.join(p.name for p in files) or '(none)'}")
    print("  (Keep a master copy of your API keys in your password manager for restores: jarvis --secrets set)")
    if ask("Remove plain-text copies? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Kept the plain-text copies. Jarvis now reads from Credential Manager first.")
        return 0
    blank_env_values(env_path, plain)
    for path in files:
        path.unlink(missing_ok=True)
    print("Removed. Check with: jarvis --secrets status")
    return 0


def set_secrets(store: SecretStore, ask_secret) -> int:  # type: ignore[no-untyped-def]
    """Type or paste each secret (hidden); Enter keeps the current value. For restores after a reinstall."""
    print("Paste each value (input is hidden). Press Enter to keep the current value.")
    for name in (*API_KEYS, "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        current = "set" if store.get(name) else "not set"
        value = ask_secret(f"  {name} ({current}): ").strip()
        if value:
            try:
                store.set(name, value)
            except SecretStoreError as e:
                print(f"  {e}")
                return 1
            print(f"  saved {name}")
    print("For Gmail, sign in again with: jarvis --gmail-login")
    return 0


def blank_env_values(env_path: Path, names: list[str]) -> None:
    """Set NAME= (empty) for the given names in .env, leaving every other line untouched."""
    pattern = re.compile(rf"^\s*({'|'.join(map(re.escape, names))})\s*=.*$")
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for line in lines:
        match = pattern.match(line.rstrip("\r\n"))
        out.append(f"{match.group(1)}=\n" if match else line)
    env_path.write_text("".join(out), encoding="utf-8")
