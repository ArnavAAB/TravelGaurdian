"""Google OAuth 2.0 (installed-app flow) shared by Gmail and Google Calendar.

One-time consent:  `tg google-auth`  (opens a browser, saves the token to GOOGLE_TOKEN).
Needs `pip install -e ".[google]"` and an OAuth client JSON (Desktop app) at GOOGLE_CLIENT_SECRETS.
"""

from __future__ import annotations

from typing import Any

from .settings import Settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",  # read flight confirmations
    "https://www.googleapis.com/auth/gmail.send",  # reply in the confirmation thread after approval
    "https://www.googleapis.com/auth/calendar.events",  # create/update flight events, read meetings
]


class GoogleAuthError(RuntimeError):
    pass


def _libs():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:  # pragma: no cover - depends on optional extras
        raise GoogleAuthError('Google libraries missing. Run: pip install -e ".[google]"') from e
    return Request, Credentials, InstalledAppFlow


def credentials(settings: Settings, interactive: bool = False) -> Any:
    Request, Credentials, InstalledAppFlow = _libs()
    creds = None
    if settings.google_token_path.exists():
        creds = Credentials.from_authorized_user_file(str(settings.google_token_path), SCOPES)
        if not set(SCOPES).issubset(set(creds.scopes or [])):
            creds = None  # token predates a scope we need (e.g. gmail.send): consent again
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        settings.google_token_path.write_text(creds.to_json())
    if creds and creds.valid:
        return creds
    if not interactive:
        raise GoogleAuthError("No valid Google token. Run `tg google-auth` once.")
    if not settings.google_client_secrets.exists():
        raise GoogleAuthError(f"OAuth client file not found: {settings.google_client_secrets}")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.google_client_secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    settings.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.google_token_path.write_text(creds.to_json())
    return creds


def service(name: str, version: str, settings: Settings) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as e:  # pragma: no cover
        raise GoogleAuthError('Google libraries missing. Run: pip install -e ".[google]"') from e
    return build(name, version, credentials=credentials(settings), cache_discovery=False)
