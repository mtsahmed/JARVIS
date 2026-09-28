"""Gmail, read-only. The only module that imports Google's auth libraries.

- Scope gmail.readonly: Google itself refuses any change (no sending, deleting, or marking read).
- Sign-in: OAuth "Desktop app" flow (google-auth-oauthlib InstalledAppFlow.run_local_server). The token is
  saved to a file in the gitignored data folder; in Google's "Testing" mode it expires after 7 days.
- REST (developers.google.com/workspace/gmail/api): GET users/me/messages?q=... and
  GET users/me/messages/{id}?format=full. Message text is returned to the caller, never stored.
"""

from __future__ import annotations

import base64
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

from jarvis.tools.file_text import html_to_text

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
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
    def __init__(self, client_file: Path, token_file: Path) -> None:
        self._client_file = client_file
        self._token_file = token_file

    @property
    def configured(self) -> bool:
        return self._client_file.is_file()

    def login(self) -> None:
        """Open the browser for Google sign-in (read-only) and save the token."""
        if not self._client_file.is_file():
            raise GmailError(f"Google client file not found: {self._client_file}. See the setup steps.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self._client_file), SCOPES)
        credentials = flow.run_local_server(port=0)  # random free local port for Google's redirect
        self._save(credentials)

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
        if not self._token_file.is_file():
            raise GmailAuthError("Gmail is not signed in yet. Run: jarvis --gmail-login")
        try:
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
            raise GmailAuthError("The Gmail sign-in file is not valid. Run: jarvis --gmail-login") from None
        return credentials

    def _save(self, credentials: Credentials) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(credentials.to_json(), encoding="utf-8")

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
