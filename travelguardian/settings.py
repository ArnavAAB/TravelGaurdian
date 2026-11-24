"""Runtime configuration, read from the environment (and an optional .env file in the project root).

Every external service is optional: when its settings are missing TravelGuardian uses the local,
simulated implementation instead, so the demo still runs end to end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Never overrides variables already set."""
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:  # blank lines in .env mean "not set"
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Shared secret for the approval endpoints and the /mcp server (Authorization: Bearer <token>).
    api_token: str
    public_base_url: str
    # Google (Gmail + Calendar) via OAuth 2.0 installed-app flow.
    google_enabled: bool
    google_client_secrets: Path
    google_token_path: Path
    gmail_query: str
    calendar_id: str
    # Where approved rescheduling requests are emailed. A controlled test inbox, never an airline.
    rebook_email_to: str
    # ElevenLabs Conversational AI outbound calls over Twilio.
    elevenlabs_api_key: str
    elevenlabs_agent_id: str
    elevenlabs_phone_number_id: str
    elevenlabs_webhook_secret: str
    elevenlabs_base_url: str
    traveler_phone: str
    # Nemotron for rationale + email drafting (extraction has its own switch in the CLI/server).
    llm_enabled: bool
    # Dashboard approve/decline buttons: "local" (loopback only), "open", or "off".
    dashboard_approvals: str
    # Background monitor: seconds between flight-status checks (0 = off), and whether to auto-call.
    watch_interval: int
    auto_call: bool

    @property
    def elevenlabs_configured(self) -> bool:
        return bool(self.elevenlabs_api_key and self.elevenlabs_agent_id and self.elevenlabs_phone_number_id)

    @property
    def google_ready(self) -> bool:
        return self.google_enabled and self.google_token_path.exists()


def load_settings() -> Settings:
    from .store import data_dir

    token_default = data_dir() / "google_token.json"
    return Settings(
        api_token=_env("TG_API_TOKEN"),
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        google_enabled=_flag("TG_GOOGLE", True),
        google_client_secrets=Path(_env("GOOGLE_CLIENT_SECRETS", str(PROJECT_ROOT / "credentials.json"))),
        google_token_path=Path(_env("GOOGLE_TOKEN", str(token_default))),
        gmail_query=_env(
            "GMAIL_QUERY",
            "newer_than:120d (subject:(itinerary OR confirmation OR booking OR e-ticket) OR "
            "\"confirmation number\" OR \"booking reference\")",
        ),
        calendar_id=_env("GOOGLE_CALENDAR_ID", "primary"),
        rebook_email_to=_env("REBOOK_EMAIL_TO"),
        elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
        elevenlabs_agent_id=_env("ELEVENLABS_AGENT_ID"),
        elevenlabs_phone_number_id=_env("ELEVENLABS_PHONE_NUMBER_ID"),
        elevenlabs_webhook_secret=_env("ELEVENLABS_WEBHOOK_SECRET"),
        elevenlabs_base_url=_env("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/"),
        traveler_phone=_env("TRAVELER_PHONE"),
        llm_enabled=_flag("TG_LLM", True),
        dashboard_approvals=_env("TG_DASHBOARD_APPROVALS", "local").lower(),
        watch_interval=int(_env("TG_WATCH_INTERVAL", "0") or 0),
        auto_call=_flag("TG_AUTO_CALL", False),
    )
