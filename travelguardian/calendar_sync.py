"""Calendar provider interface: keep one event per flight segment, read meetings for reachability.

GoogleCalendarProvider uses the Google Calendar API. LocalCalendarProvider is the simulated
fallback: flight events go to data/flight_events.json and meetings come from data/calendar.json.
Events created here carry a private extended property `tg_segment` so they can be found again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .itinerary import AIRPORTS, city_of, tz_of
from .models import CalendarEvent, Flight


class CalendarProvider(Protocol):
    name: str

    def upsert_flight(self, flight: Flight, event_id: Optional[str] = None) -> str: ...

    def replace_flights(self, old_event_ids: list[str], new_flight: Flight, note: str) -> dict: ...

    def meetings(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


def flight_event_body(flight: Flight, note: str = "") -> dict:
    description = [
        f"{flight.airline} {flight.flight_number}",
        f"Confirmation: {flight.confirmation}",
        f"Passenger: {flight.passenger}",
    ]
    if flight.seat:
        description.append(f"Seat: {flight.seat}")
    if note:
        description.append(note)
    description.append("Managed by TravelGuardian.")
    return {
        "status": "confirmed",  # also restores an event the user deleted (Google keeps it as "cancelled")
        "summary": f"✈ {flight.flight_number} {flight.origin}→{flight.destination}",
        "location": f"{city_of(flight.origin)} ({flight.origin})",
        "description": "\n".join(description),
        "start": {"dateTime": flight.departs.isoformat(), "timeZone": tz_of(flight.origin).key},
        "end": {"dateTime": flight.arrives.isoformat(), "timeZone": tz_of(flight.destination).key},
        "extendedProperties": {"private": {"tg_segment": flight.id, "tg_confirmation": flight.confirmation}},
    }


def city_from_location(location: Optional[str]) -> Optional[str]:
    if not location:
        return None
    low = location.lower()
    for city, _, _ in AIRPORTS.values():
        if city.lower() in low:
            return city
    return None


def _gone(err: Exception) -> bool:
    """A Google API error meaning the event no longer exists (deleted, or an id from another calendar)."""
    return getattr(getattr(err, "resp", None), "status", None) in (404, 410)


class GoogleCalendarProvider:
    name = "google"

    def __init__(self, service: Any, calendar_id: str = "primary"):
        self.svc = service
        self.cal = calendar_id

    def _find(self, segment_id: str) -> Optional[str]:
        res = self.svc.events().list(calendarId=self.cal, privateExtendedProperty=f"tg_segment={segment_id}",
                                     maxResults=1, singleEvents=True).execute()
        items = res.get("items", [])
        return items[0]["id"] if items else None

    def _patch(self, event_id: Optional[str], body: dict) -> Optional[str]:
        """Update an event; None if it no longer exists. Ids from the local calendar are never sent."""
        if not event_id or event_id.startswith("local-"):
            return None
        try:
            return self.svc.events().patch(calendarId=self.cal, eventId=event_id, body=body).execute()["id"]
        except Exception as e:
            if _gone(e):
                return None
            raise

    def upsert_flight(self, flight: Flight, event_id: Optional[str] = None) -> str:
        body = flight_event_body(flight)
        return (self._patch(event_id, body) or self._patch(self._find(flight.id), body)
                or self.svc.events().insert(calendarId=self.cal, body=body).execute()["id"])

    def replace_flights(self, old_event_ids: list[str], new_flight: Flight, note: str) -> dict:
        body = flight_event_body(new_flight, note)
        ids = [e for e in old_event_ids if e and not e.startswith("local-")]
        updated = None
        while ids and updated is None:  # reuse the first old event that still exists
            updated = self._patch(ids.pop(0), body)
        updated = updated or self.svc.events().insert(calendarId=self.cal, body=body).execute()["id"]
        deleted = []
        for eid in ids:
            try:
                self.svc.events().delete(calendarId=self.cal, eventId=eid).execute()
                deleted.append(eid)
            except Exception as e:
                if not _gone(e):
                    raise
        return {"updated": updated, "deleted": deleted}

    def meetings(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        res = self.svc.events().list(calendarId=self.cal, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                     singleEvents=True, orderBy="startTime", maxResults=100).execute()
        out = []
        for ev in res.get("items", []):
            if (ev.get("extendedProperties", {}).get("private", {}) or {}).get("tg_segment"):
                continue  # our own flight events
            s, e = ev.get("start", {}), ev.get("end", {})
            if "dateTime" not in s or "dateTime" not in e:
                continue  # all-day events carry no time to miss
            out.append(CalendarEvent(
                id=f"gcal-{ev['id']}",
                title=ev.get("summary", "(no title)"),
                start=datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")),
                end=datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00")),
                location=ev.get("location"),
                city=city_from_location(ev.get("location")),
                attendees=[a["email"] for a in ev.get("attendees", []) if a.get("email")],
            ))
        return out


def _next_local_id(data: dict) -> str:
    nums = [int(k.rsplit("-", 1)[1]) for k in data if k.rsplit("-", 1)[-1].isdigit()]
    return f"local-evt-{max(nums, default=0) + 1}"


class LocalCalendarProvider:
    name = "local"

    def __init__(self, events_path: Path, meetings: Callable[[], list[CalendarEvent]]):
        self.path = events_path
        self._meetings = meetings

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def upsert_flight(self, flight: Flight, event_id: Optional[str] = None) -> str:
        data = self._load()
        if event_id not in data:  # unknown here (e.g. a Google event id): look it up by segment instead
            event_id = next((k for k, v in data.items()
                             if v["extendedProperties"]["private"]["tg_segment"] == flight.id), None)
        event_id = event_id or _next_local_id(data)
        data[event_id] = flight_event_body(flight)
        self._save(data)
        return event_id

    def replace_flights(self, old_event_ids: list[str], new_flight: Flight, note: str) -> dict:
        data = self._load()
        ids = [e for e in old_event_ids if e and e in data]
        updated = ids[0] if ids else _next_local_id(data)
        data[updated] = flight_event_body(new_flight, note)
        for eid in ids[1:]:
            data.pop(eid, None)
        self._save(data)
        return {"updated": updated, "deleted": ids[1:]}

    def meetings(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [e for e in self._meetings() if e.end >= start and e.start <= end]
