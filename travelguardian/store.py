"""Trip state persisted as one JSON file in the data directory (TG_HOME, default ./data)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .models import CalendarEvent, Draft, EmailRef, Flight, FlightStatus, HotelStay, ProposedChange

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PACKAGE_ROOT / "samples"


def data_dir() -> Path:
    d = Path(os.environ.get("TG_HOME", PACKAGE_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


class ChangeLogEntry(BaseModel):
    at: datetime
    action: str
    detail: str


class Trip(BaseModel):
    traveler: str = ""
    home_city: str = ""
    flights: list[Flight] = Field(default_factory=list)
    hotels: list[HotelStay] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    proposals: list[ProposedChange] = Field(default_factory=list)
    # Last flight status seen per segment id; used to detect schedule changes.
    last_status: dict[str, FlightStatus] = Field(default_factory=dict)
    log: list[ChangeLogEntry] = Field(default_factory=list)
    # Source emails by message id (Flight.source / HotelStay.source point here).
    emails: dict[str, EmailRef] = Field(default_factory=dict)
    # Calendar event id per flight segment id, for the calendar provider in use.
    calendar_events: dict[str, str] = Field(default_factory=dict)
    calendar_provider: str = ""  # which calendar ("google"/"local") the ids above belong to
    next_proposal_number: int = 402

    def calendar_ids_for(self, provider: str) -> dict[str, str]:
        """Event ids for `provider`; ids recorded for a different calendar are dropped."""
        if self.calendar_provider != provider:
            self.calendar_events = {}
            self.calendar_provider = provider
        return self.calendar_events

    def flight(self, segment_id: str) -> Optional[Flight]:
        return next((f for f in self.flights if f.id == segment_id), None)

    def proposal(self, proposal_id: str) -> Optional[ProposedChange]:
        return next((p for p in self.proposals if p.id.upper() == proposal_id.strip().upper()), None)

    def new_proposal_id(self) -> str:
        """Short ids the voice agent can read out: TG-402, TG-403, ..."""
        pid = f"TG-{self.next_proposal_number}"
        self.next_proposal_number += 1
        return pid

    def sorted_flights(self) -> list[Flight]:
        return sorted(self.flights, key=lambda f: f.departs)


class Store:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.trip_path = self.root / "trip.json"
        self.calendar_path = self.root / "calendar.json"
        self.scenario_path = self.root / "scenario.json"

    def load(self) -> Trip:
        if not self.trip_path.exists():
            return Trip()
        return Trip.model_validate_json(self.trip_path.read_text())

    def save(self, trip: Trip) -> None:
        tmp = self.trip_path.with_suffix(".tmp")
        tmp.write_text(trip.model_dump_json(indent=2))
        tmp.replace(self.trip_path)

    def calendar(self) -> list[CalendarEvent]:
        if not self.calendar_path.exists():
            return []
        return [CalendarEvent.model_validate(e) for e in json.loads(self.calendar_path.read_text())]

    def reset(self, with_bookings: bool = True) -> Trip:
        """Copy sample calendar + simulated provider scenario into the data dir.

        with_bookings=True loads samples/bookings.json, a hand-written fixture of what
        `tg ingest` extracts from samples/inbox. False starts empty so ingest can fill it.
        """
        shutil.copy(SAMPLES / "calendar.json", self.calendar_path)
        shutil.copy(SAMPLES / "scenario.json", self.scenario_path)
        trip = Trip.model_validate_json((SAMPLES / "bookings.json").read_text())
        if not with_bookings:
            trip = Trip()  # live mode: the traveler's name comes from their own confirmation emails
        self.save(trip)
        return trip
