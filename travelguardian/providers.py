"""Travel provider interface plus a simulated implementation for demos and tests.

A real deployment would implement FlightProvider against a flight-status API
(e.g. FlightAware, Cirium, OAG) and the airline/OTA booking API. The simulated
provider reads data/scenario.json, which `tg simulate-delay` edits.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

from .models import Alternative, Flight, FlightStatus


class FlightProvider(Protocol):
    def status(self, flight: Flight) -> FlightStatus: ...

    def search(
        self, origin: str, destination: str, earliest_departure: datetime, latest_arrival: Optional[datetime] = None
    ) -> list[Alternative]: ...

    def offer(self, alternative_id: str) -> Optional[Alternative]: ...

    def rebook(self, flight: Flight, alternative_id: str) -> Flight: ...


class SimulatedProvider:
    def __init__(self, scenario_path: Path):
        self.path = scenario_path

    # -- scenario file helpers
    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def now(self) -> datetime:
        return datetime.fromisoformat(self._load()["now"])

    def set_disruption(self, flight_number: str, date: str, delay_minutes: int = 0, cancelled: bool = False, reason: str = "") -> None:
        data = self._load()
        data["disruptions"] = [d for d in data.get("disruptions", []) if not (d["flight_number"] == flight_number and d["date"] == date)]
        data["disruptions"].append(
            {"flight_number": flight_number, "date": date, "delay_minutes": delay_minutes, "cancelled": cancelled, "reason": reason}
        )
        self._save(data)

    def clear_disruptions(self) -> None:
        data = self._load()
        data["disruptions"] = []
        self._save(data)

    # -- FlightProvider
    def status(self, flight: Flight) -> FlightStatus:
        day = flight.departs.date().isoformat()
        d = next(
            (x for x in self._load().get("disruptions", []) if x["flight_number"] == flight.flight_number and x["date"] == day),
            None,
        )
        delay = timedelta(minutes=d["delay_minutes"]) if d else timedelta(0)
        if d and d.get("cancelled"):
            st = "cancelled"
        elif delay > timedelta(0):
            st = "delayed"
        else:
            st = "on_time"
        return FlightStatus(
            flight_number=flight.flight_number,
            status=st,
            scheduled_departure=flight.departs,
            scheduled_arrival=flight.arrives,
            estimated_departure=flight.departs + delay,
            estimated_arrival=flight.arrives + delay,
            delay_minutes=int(delay.total_seconds() // 60),
            reason=(d or {}).get("reason") or None,
        )

    def search(
        self, origin: str, destination: str, earliest_departure: datetime, latest_arrival: Optional[datetime] = None
    ) -> list[Alternative]:
        out = []
        for raw in self._load()["inventory"]:
            alt = Alternative.model_validate(raw)
            if alt.origin != origin or alt.destination != destination:
                continue
            if alt.departs < earliest_departure or alt.seats_available <= 0:
                continue
            if latest_arrival and alt.arrives > latest_arrival:
                continue
            out.append(alt)
        # Protected (free) rebookings first, then earliest arrival.
        out.sort(key=lambda a: (not a.protected, a.arrives))
        return out

    def offer(self, alternative_id: str) -> Optional[Alternative]:
        raw = next((a for a in self._load()["inventory"] if a["id"] == alternative_id), None)
        return Alternative.model_validate(raw) if raw else None

    def rebook(self, flight: Flight, alternative_id: str) -> Flight:
        data = self._load()
        raw = next((a for a in data["inventory"] if a["id"] == alternative_id), None)
        if raw is None:
            raise ValueError(f"Unknown alternative {alternative_id}")
        if raw["seats_available"] <= 0:
            raise ValueError(f"{raw['flight_number']} has no seats left")
        raw["seats_available"] -= 1
        self._save(data)
        alt = Alternative.model_validate(raw)
        return Flight(
            id=f"seg-{alt.flight_number.replace(' ', '')}-{alt.departs:%Y%m%d}",
            confirmation=flight.confirmation if alt.protected else secrets.token_hex(3).upper(),
            airline=alt.airline,
            flight_number=alt.flight_number,
            origin=alt.origin,
            destination=alt.destination,
            departs=alt.departs,
            arrives=alt.arrives,
            passenger=flight.passenger,
            cabin=flight.cabin,
            seat=None,
            source=f"rebooked from {flight.id}",
        )


def __getattr__(name: str):
    if name == "AirLabsProvider":
        from .airlabs_provider import AirLabsProvider

        return AirLabsProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
