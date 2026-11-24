"""Inbox -> trip -> calendar.

Reads confirmation emails (Gmail, or the sample inbox), extracts bookings with Nemotron (only the
email text is sent to the model), records each source email so replies can go to its thread, keeps
one calendar event per flight, and (with Google Calendar) pulls the traveler's meetings around the
trip so the risk engine can check calendar reachability.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .calendar_sync import GoogleCalendarProvider
from .ingest import extract, merge, to_bookings
from .mail import ref_for
from .services import Services
from .store import ChangeLogEntry


def sync_inbox(svc: Services, max_results: int = 25) -> dict[str, Any]:
    client = svc.llm()
    if client is None:
        raise RuntimeError("Email extraction needs Nemotron; TG_LLM is off.")
    trip = svc.store.load()
    processed = []
    for msg in svc.mail.fetch_confirmations(max_results):
        if msg.id in trip.emails:
            processed.append({"subject": msg.subject, "status": "already synced"})
            continue
        x = extract(client, msg)
        if not x.is_travel_booking:
            processed.append({"subject": msg.subject, "status": "skipped (not a booking)"})
            continue
        flights, hotels = to_bookings(x, msg.id)
        added, updated = merge(trip, flights, hotels)
        if not trip.traveler and flights:
            trip.traveler = flights[0].passenger
        trip.emails[msg.id] = ref_for(msg, svc.mail.name)
        processed.append({"subject": msg.subject, "status": "ingested", "flights": len(flights),
                          "hotels": len(hotels), "added": added, "updated": updated})
    svc.store.save(trip)
    calendar = sync_calendar(svc)
    trip = svc.store.load()
    trip.log.append(ChangeLogEntry(at=svc.now(), action="sync",
                                   detail=f"{svc.mail.name} inbox: {len(processed)} message(s)"))
    svc.store.save(trip)
    return {"mail_provider": svc.mail.name, "calendar_provider": svc.calendar.name, "processed": processed,
            **calendar}


def sync_calendar(svc: Services) -> dict[str, Any]:
    trip = svc.store.load()
    known = trip.calendar_ids_for(svc.calendar.name)
    events = {}
    for f in trip.sorted_flights():
        events[f.id] = svc.calendar.upsert_flight(f, known.get(f.id))
        known[f.id] = events[f.id]
        svc.store.save(trip)  # keep ids already created if a later call fails
    meetings = 0
    flights = trip.sorted_flights()
    if isinstance(svc.calendar, GoogleCalendarProvider) and flights:
        start = flights[0].departs - timedelta(days=1)
        end = max(f.arrives for f in flights) + timedelta(days=1)
        found = svc.calendar.meetings(start, end)
        svc.store.calendar_path.write_text("[" + ",".join(m.model_dump_json() for m in found) + "]")
        meetings = len(found)
    return {"flight_events": len(events), "meetings_imported": meetings}
