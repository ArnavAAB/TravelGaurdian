"""Wires the configured providers together: real ones where credentials exist, simulated otherwise."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .calendar_sync import CalendarProvider, GoogleCalendarProvider, LocalCalendarProvider
from .mail import GmailProvider, LocalMailProvider, MailProvider
from .providers import FlightProvider, SimulatedProvider
from .settings import Settings, load_settings
from .store import SAMPLES, Store


def flight_provider(store: Store) -> FlightProvider:
    """AirLabs live status when AIRLABS_API_KEY is set (USE_LIVE_AIRLABS can force either way)."""
    use_live_env = os.getenv("USE_LIVE_AIRLABS", "")
    has_key = bool(os.getenv("AIRLABS_API_KEY", "").strip())
    use_live = use_live_env.lower() in {"1", "true", "yes"} if use_live_env else has_key
    if use_live and has_key:
        try:
            from .airlabs_provider import AirLabsProvider

            return AirLabsProvider(store.scenario_path, ttl_seconds=300, allow_live_date_mismatch=True)
        except ImportError:
            pass
    return SimulatedProvider(store.scenario_path)


@dataclass
class Services:
    settings: Settings
    store: Store
    provider: FlightProvider
    mail: MailProvider
    calendar: CalendarProvider
    notes: list[str] = field(default_factory=list)
    _llm: Any = None

    def now(self) -> datetime:
        fn = getattr(self.provider, "now", None)
        return fn() if fn else datetime.now(timezone.utc)

    def llm(self) -> Optional[Any]:
        """Nemotron client, or None when TG_LLM=off."""
        if not self.settings.llm_enabled:
            return None
        if self._llm is None:
            from .cli import _client

            self._llm = _client()
        return self._llm

    def rebook_recipient(self) -> str:
        """The controlled address that receives rescheduling requests. Never an airline by default."""
        if self.settings.rebook_email_to:
            return self.settings.rebook_email_to
        if isinstance(self.mail, GmailProvider):
            # Default to the traveler's own mailbox: the request lands in the original thread.
            return self.mail.svc.users().getProfile(userId="me").execute()["emailAddress"]
        return "rebooking-desk@example.test"


def build_services(store: Optional[Store] = None, settings: Optional[Settings] = None) -> Services:
    settings = settings or load_settings()
    store = store or Store()
    if not store.scenario_path.exists():
        store.reset()
    notes: list[str] = []
    mail: MailProvider = LocalMailProvider(SAMPLES / "inbox", store.root / "outbox")
    calendar: CalendarProvider = LocalCalendarProvider(store.root / "flight_events.json", store.calendar)
    if settings.google_ready:
        try:
            from .google_auth import service

            mail = GmailProvider(service("gmail", "v1", settings), settings.gmail_query)
            calendar = GoogleCalendarProvider(service("calendar", "v3", settings), settings.calendar_id)
        except Exception as e:  # missing extras, revoked token, offline: keep the simulated providers
            notes.append(f"Google unavailable, using local inbox/calendar: {e}")
    return Services(settings=settings, store=store, provider=flight_provider(store), mail=mail,
                    calendar=calendar, notes=notes)
