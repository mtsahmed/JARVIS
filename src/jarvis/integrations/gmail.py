"""Gmail, read-only. The only module that imports Google's auth libraries.

- Scope gmail.readonly: Google itself refuses any change (no sending, deleting, or marking read).
- Sign-in: OAuth "Desktop app" flow (google-auth-oauthlib InstalledAppFlow.run_local_server). The token is
  saved to a file in the gitignored data folder; in Google's "Testing" mode it expires after 7 days.
- REST (developers.google.com/workspace/gmail/api): GET users/me/messages?q=... and
  GET users/me/messages/{id}?format=full. Message text is returned to the caller, never stored.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from jarvis.integrations.credentials import SecretStore, SecretStoreError, default_store
from jarvis.tools.file_text import html_to_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
# Google OAuth endpoints, as listed in the downloaded Desktop-app client JSON.
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Owner decision: "important" = unread, marked Important by Gmail, Primary inbox, last 24 hours.
IMPORTANT_QUERY = "is:unread is:important category:primary newer_than:1d"
MAX_EMAILS = 10
MAX_TEXT_CHARS = 1500
TIMEOUT_SECONDS = 15


class GmailError(Exception):
    """Gmail could not be read. The message is safe to show the user."""


class GmailAuthError(GmailError):
    """Sign-in missing or expired: the user must run `jarvis --gmail-login`."""


@dataclass(frozen=True)
class Email:
    sender: str  # display name, or address if there is none
    subject: str
    received: datetime  # local time
    text: str  # start of the plain text, at most MAX_TEXT_CHARS


class GmailClient:
    def __init__(self, client_file: Path, token_file: Path, store: SecretStore | None = None) -> None:
        self._client_file = client_file
        self._token_file = token_file  # legacy location; after `jarvis --secrets import` it is not used
        self._store = store or default_store()
        self._cached: Credentials | None = None

    @property
    def configured(self) -> bool:
        return bool(self._store.get("GOOGLE_CLIENT_ID")) or self._client_file.is_file()

    def login(self) -> None:
        """Open the browser for Google sign-in (read-only) and save the sign-in to Credential Manager."""
        flow = InstalledAppFlow.from_client_config(self._client_config(), SCOPES)
        credentials = flow.run_local_server(port=0)  # random free local port for Google's redirect
        self._save(credentials)

    def _client_config(self) -> dict[str, Any]:
        """The OAuth client: from Credential Manager, else from the downloaded client JSON file."""
        client_id, secret = self._store.get("GOOGLE_CLIENT_ID"), self._store.get("GOOGLE_CLIENT_SECRET")
        if client_id and secret:
            return {"installed": {"client_id": client_id, "client_secret": secret, "auth_uri": AUTH_URI,
                                  "token_uri": TOKEN_URI, "redirect_uris": ["http://localhost"]}}
        if not self._client_file.is_file():
            raise GmailError(f"Google client not set up: {self._client_file} not found. See the setup steps.")
        try:
            return json.loads(self._client_file.read_text(encoding="utf-8"))
        except ValueError:
            raise GmailError("The Google client file is not valid JSON. Download it again.") from None

    def important_unread(self) -> list[Email]:
        session = AuthorizedSession(self._credentials())
        listing = self._get(session, f"{API}/messages", {"q": IMPORTANT_QUERY, "maxResults": MAX_EMAILS})
        emails = []
        for item in listing.get("messages", [])[:MAX_EMAILS]:
            message = self._get(session, f"{API}/messages/{item['id']}", {"format": "full"})
            emails.append(_parse(message))
        return emails

    # --- internals ----------------------------------------------------------------------------

    def _credentials(self) -> Credentials:
        if self._cached is not None and self._cached.valid:
            return self._cached  # access token still fresh (kept in memory only)
        refresh_token = self._store.get("GOOGLE_REFRESH_TOKEN")
        if not refresh_token and not self._token_file.is_file():
            raise GmailAuthError("Gmail is not signed in yet. Run: jarvis --gmail-login")
        try:
            if refresh_token:
                installed = self._client_config()["installed"]
                credentials = Credentials(None, refresh_token=refresh_token, token_uri=TOKEN_URI,
                                          client_id=installed["client_id"], client_secret=installed["client_secret"],
                                          scopes=SCOPES)
            else:
                credentials = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise GmailAuthError("Gmail sign-in is incomplete. Run: jarvis --gmail-login")
                credentials.refresh(Request())
                self._save(credentials)
        except RefreshError:
            # Expected about every 7 days while the Google project is in Testing mode.
            logger.warning("Gmail sign-in expired")
            raise GmailAuthError("Gmail sign-in has expired. Run: jarvis --gmail-login") from None
        except TransportError:
            raise GmailError("Could not reach Google. Check your internet connection.") from None
        except (GoogleAuthError, ValueError) as e:
            logger.error("Gmail token problem: %s", type(e).__name__)
            raise GmailAuthError("The Gmail sign-in is not valid. Run: jarvis --gmail-login") from None
        self._cached = credentials
        return credentials

    def _save(self, credentials: Credentials) -> None:
        """Keep the small, long-lived parts in Credential Manager; no token file on disk."""
        try:
            if credentials.refresh_token:
                self._store.set("GOOGLE_REFRESH_TOKEN", credentials.refresh_token)
            self._store.set("GOOGLE_CLIENT_ID", credentials.client_id)
            self._store.set("GOOGLE_CLIENT_SECRET", credentials.client_secret)
        except SecretStoreError as e:  # Credential Manager unavailable: fall back to the token file
            logger.warning("Saving Google sign-in to a file instead: %s", e)
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(credentials.to_json(), encoding="utf-8")
        # An old plain-text token file is left alone (owner decision: plain-text copies are only removed
        # by `jarvis --secrets import` after confirmation). Jarvis reads the vault first, so it is unused.

    @staticmethod
    def _get(session: AuthorizedSession, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = session.request("GET", url, params=params, timeout=TIMEOUT_SECONDS)
        except Exception as e:  # requests/transport errors: no details (they can include URLs/tokens)
            logger.warning("Gmail request failed: %s", type(e).__name__)
            raise GmailError("Could not reach Gmail. Check your internet connection.") from None
        if response.status_code == 401:
            raise GmailAuthError("Gmail sign-in is no longer valid. Run: jarvis --gmail-login")
        if response.status_code != 200:
            logger.error("Gmail HTTP %s", response.status_code)
            raise GmailError(f"Gmail returned an error (HTTP {response.status_code}).")
        return response.json()


def _parse(message: dict[str, Any]) -> Email:
    payload = message.get("payload", {})
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
    name, address = parseaddr(headers.get("from", ""))
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000).astimezone()
    text = _plain_text(payload) or message.get("snippet", "")
    return Email(sender=name or address or "unknown sender", subject=headers.get("subject", "(no subject)"),
                 received=received, text=" ".join(text.split())[:MAX_TEXT_CHARS])


def _plain_text(part: dict[str, Any]) -> str:
    """Prefer text/plain anywhere in the MIME tree; fall back to text/html converted to text."""
    plain, html = _find(part, "text/plain"), _find(part, "text/html")
    if plain:
        return plain
    return html_to_text(html) if html else ""


def _find(part: dict[str, Any], mime_type: str) -> str:
    if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
        data = part["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for child in part.get("parts", []) or []:
        if found := _find(child, mime_type):
            return found
    return ""
