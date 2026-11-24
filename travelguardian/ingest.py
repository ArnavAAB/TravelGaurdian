"""Read confirmation emails and extract bookings with Nemotron structured outputs."""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from openai import OpenAI
from pydantic import BaseModel, Field

from .itinerary import AIRPORTS
from .models import Flight, HotelStay
from .store import Trip

MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"


@dataclass
class Message:
    id: str
    sender: str
    subject: str
    date: str
    body: str
    thread_id: str = ""  # Gmail thread id (or a local stand-in) for replying in-thread
    rfc_message_id: str = ""  # Message-ID header


def message_from_bytes(raw: bytes, msg_id: str, thread_id: str = "") -> Message:
    """Parse an RFC 822 email into the text the extractor needs (HTML is reduced to text)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        body = html_to_text(body)
    return Message(
        id=msg_id,
        sender=str(msg["From"] or ""),
        subject=str(msg["Subject"] or ""),
        date=str(msg["Date"] or ""),
        body=body,
        thread_id=thread_id or f"local-{msg_id}",
        rfc_message_id=str(msg["Message-ID"] or "").strip(),
    )


def html_to_text(html: str) -> str:
    import re
    from html import unescape

    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def read_inbox(folder: Path) -> list[Message]:
    """Local stand-in for Gmail: a folder of .eml files."""
    return [message_from_bytes(p.read_bytes(), f"{folder.name}/{p.name}") for p in sorted(folder.glob("*.eml"))]


# ---- extraction schema (what the model returns)


class ExtractedFlight(BaseModel):
    confirmation: str
    airline: str
    flight_number: str = Field(description="Carrier code + number, no spaces, e.g. UA958")
    origin: str = Field(description="IATA airport code")
    destination: str = Field(description="IATA airport code")
    departs_local: str = Field(description="Local departure time at origin, YYYY-MM-DDTHH:MM")
    arrives_local: str = Field(description="Local arrival time at destination, YYYY-MM-DDTHH:MM")
    origin_timezone: str = Field(description="IANA timezone of origin airport, e.g. America/Chicago")
    destination_timezone: str = Field(description="IANA timezone of destination airport")
    passenger: str
    cabin: Optional[str] = None
    seat: Optional[str] = None


class ExtractedHotel(BaseModel):
    confirmation: str
    name: str
    city: str
    address: Optional[str] = None
    check_in: str = Field(description="YYYY-MM-DD")
    check_out: str = Field(description="YYYY-MM-DD")
    check_in_time: Optional[str] = Field(description="HH:MM, 24h", default=None)
    guest: str


class Extraction(BaseModel):
    is_travel_booking: bool = Field(description="False for newsletters, promotions, and anything that is not a confirmed booking")
    flights: list[ExtractedFlight]
    hotels: list[ExtractedHotel]


SYSTEM = (
    "You extract travel bookings from confirmation emails. Return one flight entry per flight "
    "segment and one hotel entry per reservation. "
    "origin and destination MUST be standard 3-letter IATA airport codes (e.g. SFO, ORD, LHR). "
    "If the email lists an airport by name (such as 'London Heathrow (T5)' or 'San Francisco International'), convert it to its 3-letter IATA code. "
    "Always combine the flight date and time into full ISO format 'YYYY-MM-DDTHH:MM' for departs_local and arrives_local (e.g. '2026-12-09T14:10'). "
    "Resolve '+1 day' style arrivals to the correct calendar date. Times in airline emails are local "
    "to each airport; give the IANA timezone of each airport. If the email is not a booking confirmation, "
    "set is_travel_booking to false and return empty lists. Never invent a field that is not in the email; use null."
)


def extract(client: OpenAI, msg: Message) -> Extraction:
    response = client.beta.chat.completions.parse(
        model=MODEL,
        max_tokens=16000,
        temperature=1,
        top_p=0.95,
        seed=42,
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": f"From: {msg.sender}\nSubject: {msg.subject}\nDate: {msg.date}\n\n{msg.body}",
            },
        ],
        response_format=Extraction,
    )
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "refusal":
        raise RuntimeError(f"Model declined to process {msg.id}")
    if choice.message.parsed is None:
        raise RuntimeError(f"No structured output for {msg.id}")
    return choice.message.parsed


def _iata(value: str) -> str:
    cleaned = value.strip().upper()
    if cleaned in AIRPORTS:
        return cleaned
    for code, (city, _, _) in AIRPORTS.items():
        if code in cleaned or city.upper() in cleaned:
            return code
    if "HEATHROW" in cleaned:
        return "LHR"
    if "SAN FRANCISCO" in cleaned:
        return "SFO"
    if "O'HARE" in cleaned or "OHARE" in cleaned:
        return "ORD"
    return cleaned[:3]


def _local(ts: str, tz: str, airport: str) -> datetime:
    iata = _iata(airport)
    if iata in AIRPORTS:
        zone = ZoneInfo(AIRPORTS[iata][1])
    else:
        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = ZoneInfo("UTC")
    clean_ts = ts.strip().replace(" ", "T")
    return datetime.fromisoformat(clean_ts).replace(tzinfo=zone)


def to_bookings(x: Extraction, source: str) -> tuple[list[Flight], list[HotelStay]]:
    flights = []
    for f in x.flights:
        orig = _iata(f.origin)
        dest = _iata(f.destination)
        dep = _local(f.departs_local, f.origin_timezone, orig)
        arr = _local(f.arrives_local, f.destination_timezone, dest)
        num = f.flight_number.replace(" ", "").upper()
        flights.append(
            Flight(
                id=f"seg-{num}-{dep:%Y%m%d}",
                confirmation=f.confirmation,
                airline=f.airline,
                flight_number=num,
                origin=orig,
                destination=dest,
                departs=dep,
                arrives=arr,
                passenger=f.passenger.title(),
                cabin=f.cabin,
                seat=f.seat,
                source=source,
            )
        )
    hotels = []
    for h in x.hotels:
        ci_str = h.check_in.split("T")[0].split()[0]
        co_str = h.check_out.split("T")[0].split()[0]
        ci = date.fromisoformat(ci_str)
        co = date.fromisoformat(co_str)
        hotels.append(
            HotelStay(
                id=f"htl-{hashlib.sha1(h.confirmation.encode()).hexdigest()[:6]}-{ci:%Y%m%d}",
                confirmation=h.confirmation,
                name=h.name,
                city=h.city,
                address=h.address,
                check_in=ci,
                check_out=co,
                check_in_time=h.check_in_time or "15:00",
                guest=h.guest,
                source=source,
            )
        )
    return flights, hotels


def merge(trip: Trip, flights: list[Flight], hotels: list[HotelStay]) -> tuple[int, int]:
    """Add new bookings; replace existing ones with the same id (newer email wins)."""
    added = updated = 0
    for f in flights:
        idx = next((i for i, g in enumerate(trip.flights) if g.id == f.id), None)
        if idx is None:
            trip.flights.append(f)
            added += 1
        else:
            trip.flights[idx] = f
            updated += 1
    for h in hotels:
        idx = next((i for i, g in enumerate(trip.hotels) if g.confirmation == h.confirmation), None)
        if idx is None:
            trip.hotels.append(h)
            added += 1
        else:
            trip.hotels[idx] = h
            updated += 1
    return added, updated


def ingest_text(client: OpenAI, trip: Trip, text: str, source: str) -> dict:
    """Used by the MCP server: a host passes raw email text."""
    msg = Message(id=source, sender="", subject="", date="", body=text)
    x = extract(client, msg)
    if not x.is_travel_booking:
        return {"is_travel_booking": False, "added": 0, "updated": 0}
    flights, hotels = to_bookings(x, source)
    added, updated = merge(trip, flights, hotels)
    return {"is_travel_booking": True, "added": added, "updated": updated, "flights": [f.id for f in flights], "hotels": [h.id for h in hotels]}
