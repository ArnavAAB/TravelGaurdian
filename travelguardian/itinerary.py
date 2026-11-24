"""Deterministic itinerary building and conflict detection.

Nothing here calls a model: risk and conflict decisions are plain code so they are
testable and repeatable. Nemotron decides what to do about them.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .models import CalendarEvent, Conflict, Flight, FlightStatus, HotelStay
from .store import Trip

# city, IANA timezone, country
AIRPORTS: dict[str, tuple[str, str, str]] = {
    "SFO": ("San Francisco", "America/Los_Angeles", "US"),
    "LAX": ("Los Angeles", "America/Los_Angeles", "US"),
    "SEA": ("Seattle", "America/Los_Angeles", "US"),
    "ORD": ("Chicago", "America/Chicago", "US"),
    "DFW": ("Dallas", "America/Chicago", "US"),
    "DEN": ("Denver", "America/Denver", "US"),
    "JFK": ("New York", "America/New_York", "US"),
    "EWR": ("New York", "America/New_York", "US"),
    "BOS": ("Boston", "America/New_York", "US"),
    "IAD": ("Washington", "America/New_York", "US"),
    "LHR": ("London", "Europe/London", "GB"),
    "LGW": ("London", "Europe/London", "GB"),
    "CDG": ("Paris", "Europe/Paris", "FR"),
    "AMS": ("Amsterdam", "Europe/Amsterdam", "NL"),
    "FRA": ("Frankfurt", "Europe/Berlin", "DE"),
    "DXB": ("Dubai", "Asia/Dubai", "AE"),
    "SIN": ("Singapore", "Asia/Singapore", "SG"),
    "BLR": ("Bengaluru", "Asia/Kolkata", "IN"),
    "BOM": ("Mumbai", "Asia/Kolkata", "IN"),
    "DEL": ("Delhi", "Asia/Kolkata", "IN"),
    "NRT": ("Tokyo", "Asia/Tokyo", "JP"),
    "HND": ("Tokyo", "Asia/Tokyo", "JP"),
}

# Minimum connection times in minutes: (domestic->domestic, any international leg).
MCT: dict[str, tuple[int, int]] = {
    "ORD": (50, 90),
    "JFK": (60, 90),
    "LHR": (60, 90),
    "CDG": (60, 90),
    "FRA": (45, 60),
}
DEFAULT_MCT = (45, 75)
TIGHT_BUFFER_MIN = 30  # a connection within MCT + this is flagged as tight
# Simulated maps data: typical airport -> city-centre transfer, minutes. A real deployment
# replaces ground_transfer() with a maps provider (e.g. a Google Maps MCP server).
GROUND_TRANSFER_MIN: dict[str, int] = {"LHR": 60, "LGW": 70, "ORD": 45, "SFO": 35, "JFK": 60, "CDG": 50}
ARRIVAL_PROCESSING_MIN = (20, 45)  # deplane + bags: domestic, international (immigration)


def city_of(airport: str) -> str:
    return AIRPORTS.get(airport, (airport, "UTC", ""))[0]


def tz_of(airport: str) -> ZoneInfo:
    return ZoneInfo(AIRPORTS.get(airport, ("", "UTC", ""))[1])


def is_international(f: Flight) -> bool:
    a = AIRPORTS.get(f.origin, ("", "", "?"))[2]
    b = AIRPORTS.get(f.destination, ("", "", "?"))[2]
    return a != b


def mct_minutes(airport: str, inbound: Flight, outbound: Flight) -> int:
    dom, intl = MCT.get(airport, DEFAULT_MCT)
    return intl if (is_international(inbound) or is_international(outbound)) else dom


def parse_iso(value: str) -> datetime:
    """datetime.fromisoformat that also accepts a trailing Z (Python 3.10)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt(dt: datetime, airport: Optional[str] = None) -> str:
    """Format a time, converted to the airport's local zone when given (so %Z reads CST, GMT...)."""
    if airport:
        dt = dt.astimezone(tz_of(airport))
    return dt.strftime("%a %d %b %H:%M %Z")


def ground_transfer(arrival: Flight, destination: str) -> dict:
    """Minutes from wheels-down to reaching `destination` in the arrival city."""
    processing = ARRIVAL_PROCESSING_MIN[1 if is_international(arrival) else 0]
    drive = GROUND_TRANSFER_MIN.get(arrival.destination, 60)
    return {
        "from": arrival.destination,
        "to": destination,
        "arrival_processing_minutes": processing,
        "transfer_minutes": drive,
        "total_minutes": processing + drive,
        "source": "simulated maps estimate",
    }


def minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


# ---------------------------------------------------------------- connections


def connections(trip: Trip) -> list[tuple[Flight, Flight]]:
    """Consecutive flights where the second leaves from where the first lands within 24h."""
    fl = trip.sorted_flights()
    out = []
    for a, b in zip(fl, fl[1:]):
        if a.destination == b.origin and timedelta(hours=-6) < b.departs - a.arrives < timedelta(hours=24):
            out.append((a, b))
    return out


def assess_connection(
    inbound: Flight,
    outbound: Flight,
    inbound_status: Optional[FlightStatus] = None,
    outbound_status: Optional[FlightStatus] = None,
) -> dict:
    """Risk of missing `outbound` given the latest known times of both legs."""
    arr = inbound_status.estimated_arrival if inbound_status else inbound.arrives
    dep = outbound_status.estimated_departure if outbound_status else outbound.departs
    layover = minutes(dep - arr)
    mct = mct_minutes(inbound.destination, inbound, outbound)
    if outbound_status and outbound_status.status == "cancelled":
        risk = "missed"
        why = f"{outbound.flight_number} is cancelled."
    elif layover < 0:
        risk, why = "missed", f"{inbound.flight_number} now lands {-layover} min after {outbound.flight_number} departs."
    elif layover < mct:
        risk, why = "high", f"Only {layover} min to connect; minimum connection time at {inbound.destination} is {mct} min."
    elif layover < mct + TIGHT_BUFFER_MIN:
        risk, why = "medium", f"{layover} min to connect against a {mct} min minimum; little slack."
    else:
        risk, why = "low", f"{layover} min to connect; minimum is {mct} min."
    return {
        "inbound": inbound.id,
        "outbound": outbound.id,
        "airport": inbound.destination,
        "inbound_arrival": arr.isoformat(),
        "outbound_departure": dep.isoformat(),
        "layover_minutes": layover,
        "minimum_connection_minutes": mct,
        "risk": risk,
        "explanation": why,
    }


# ---------------------------------------------------------------- timeline


def timeline(trip: Trip, calendar: list[CalendarEvent]) -> list[dict]:
    """Every item on one time-sorted list, for display and for the agent."""
    items: list[dict] = []
    for f in trip.flights:
        detail = f"departs {fmt(f.departs, f.origin)}, arrives {fmt(f.arrives, f.destination)} · conf {f.confirmation}"
        if f.seat:
            detail += f" · seat {f.seat}"
        st = trip.last_status.get(f.id)
        status = None
        if st and st.status == "cancelled":
            status = "CANCELLED"
        elif st and st.status == "delayed":
            status = (
                f"DELAYED {st.delay_minutes} min: now departs {fmt(st.estimated_departure, f.origin)}, "
                f"arrives {fmt(st.estimated_arrival, f.destination)}"
            )
        items.append(
            {
                "at": f.departs,
                "type": "flight",
                "id": f.id,
                "title": f"{f.airline} {f.flight_number} {f.origin}->{f.destination}",
                "detail": detail,
                "status": status,
            }
        )
    for h in trip.hotels:
        tz = _city_tz(h.city)
        hh, mm = (int(x) for x in h.check_in_time.split(":"))
        items.append(
            {
                "at": datetime.combine(h.check_in, time(hh, mm), tz),
                "type": "hotel",
                "id": h.id,
                "title": f"Check in: {h.name}",
                "detail": f"{h.city}, {h.check_in:%d %b} -> {h.check_out:%d %b} ({(h.check_out - h.check_in).days} nights) · conf {h.confirmation}",
            }
        )
        items.append(
            {
                "at": datetime.combine(h.check_out, time(11, 0), tz),
                "type": "hotel",
                "id": h.id,
                "title": f"Check out: {h.name}",
                "detail": f"{h.city}",
            }
        )
    for e in calendar:
        items.append(
            {
                "at": e.start,
                "type": "event",
                "id": e.id,
                "title": e.title,
                "detail": f"{fmt(e.start.astimezone(_city_tz(e.city)) if e.city else e.start)} - {e.end:%H:%M}" + (f" · {e.location}" if e.location else ""),
            }
        )
    items.sort(key=lambda i: i["at"])
    return items


def _city_tz(city: str) -> ZoneInfo:
    for c, tz, _ in AIRPORTS.values():
        if c.lower() == city.lower():
            return ZoneInfo(tz)
    return ZoneInfo("UTC")


# ---------------------------------------------------------------- conflicts


def detect_conflicts(
    trip: Trip,
    calendar: list[CalendarEvent],
    statuses: Optional[dict[str, FlightStatus]] = None,
) -> list[Conflict]:
    statuses = statuses if statuses is not None else trip.last_status
    out: list[Conflict] = []
    fl = trip.sorted_flights()

    def arr(f: Flight) -> datetime:
        s = statuses.get(f.id)
        return s.estimated_arrival if s else f.arrives

    def dep(f: Flight) -> datetime:
        s = statuses.get(f.id)
        return s.estimated_departure if s else f.departs

    # Cancelled flights
    for f in fl:
        s = statuses.get(f.id)
        if s and s.status == "cancelled":
            out.append(Conflict(severity="critical", kind="cancelled", message=f"{f.flight_number} is cancelled.", items=[f.id]))

    # Overlapping flights (double-booked)
    for a, b in zip(fl, fl[1:]):
        if a.destination != b.origin and dep(b) < arr(a):
            out.append(
                Conflict(
                    severity="critical",
                    kind="overlap",
                    message=f"{a.flight_number} and {b.flight_number} overlap in time.",
                    items=[a.id, b.id],
                )
            )

    # Connections
    for a, b in connections(trip):
        r = assess_connection(a, b, statuses.get(a.id), statuses.get(b.id))
        if r["risk"] in ("missed", "high"):
            out.append(Conflict(severity="critical", kind="connection", message=r["explanation"], items=[a.id, b.id]))
        elif r["risk"] == "medium":
            out.append(Conflict(severity="warning", kind="connection", message=r["explanation"], items=[a.id, b.id]))

    # Hotel coverage: every night spent in a city between landing and leaving needs a room.
    for a, b in zip(fl, fl[1:]):
        if city_of(a.destination) != city_of(b.origin):
            continue
        city = city_of(a.destination)
        land = arr(a).astimezone(tz_of(a.destination))
        leave = dep(b).astimezone(tz_of(b.origin))
        # A night is needed for each date from landing day (or the day before if landing
        # before 05:00) up to the day before leaving.
        first_night = land.date() - timedelta(days=1) if land.hour < 5 else land.date()
        night = first_night
        missing: list[date] = []
        while night < leave.date():
            if not any(h.city.lower() == city.lower() and h.check_in <= night < h.check_out for h in trip.hotels):
                missing.append(night)
            night += timedelta(days=1)
        if missing:
            nights = ", ".join(n.strftime("%a %d %b") for n in missing)
            out.append(
                Conflict(
                    severity="warning",
                    kind="hotel_gap",
                    message=f"No hotel booked in {city} for the night(s) of {nights}.",
                    items=[a.id, b.id],
                )
            )
        for h in trip.hotels:
            if h.city.lower() != city.lower():
                continue
            if h.check_in > land.date() + timedelta(days=1) or h.check_out < leave.date() - timedelta(days=1):
                continue
            hh, mm = (int(x) for x in h.check_in_time.split(":"))
            check_in_at = datetime.combine(h.check_in, time(hh, mm), land.tzinfo)
            if h.check_in == land.date() and land < check_in_at - timedelta(hours=3):
                out.append(
                    Conflict(
                        severity="info",
                        kind="early_arrival",
                        message=f"You land at {land:%H:%M} but check-in at {h.name} is {h.check_in_time}. Consider requesting early check-in.",
                        items=[a.id, h.id],
                    )
                )
            if h.check_in == land.date() and land.hour >= 22:
                out.append(
                    Conflict(
                        severity="warning",
                        kind="late_arrival",
                        message=f"You land at {land:%H:%M}; tell {h.name} to hold the room for a late arrival.",
                        items=[a.id, h.id],
                    )
                )

    # Hotels with no flight bringing you to that city before check-in
    for h in trip.hotels:
        arrivals = [f for f in fl if city_of(f.destination).lower() == h.city.lower()]
        if fl and not arrivals:
            out.append(
                Conflict(severity="warning", kind="hotel_unreachable", message=f"No flight to {h.city} for {h.name}.", items=[h.id])
            )

    # Calendar: events you'd miss because you're in the air or not in that city yet.
    for e in calendar:
        for f in fl:
            if dep(f) < e.end and arr(f) > e.start:
                out.append(
                    Conflict(
                        severity="critical",
                        kind="calendar",
                        message=f"'{e.title}' ({fmt(e.start, f.destination)}) is while you are on {f.flight_number}.",
                        items=[e.id, f.id],
                    )
                )
        if e.city:
            inbound = [f for f in fl if city_of(f.destination).lower() == e.city.lower()]
            prior = [f for f in inbound if dep(f) < e.start]
            if prior:
                f = max(prior, key=dep)
                gap = minutes(e.start - arr(f))
                need = ground_transfer(f, e.location or e.city)["total_minutes"]
                if 0 <= gap < need:
                    out.append(
                        Conflict(
                            severity="critical" if gap < need - 30 else "warning",
                            kind="calendar",
                            message=f"'{e.title}' starts {gap} min after {f.flight_number} lands; getting there from {f.destination} takes about {need} min.",
                            items=[e.id, f.id],
                        )
                    )
                elif gap < 0 and not any(c.items == [e.id, f.id] for c in out):
                    out.append(
                        Conflict(
                            severity="critical",
                            kind="calendar",
                            message=f"'{e.title}' starts {fmt(e.start, f.destination)}, but {f.flight_number} now lands {fmt(arr(f), f.destination)}.",
                            items=[e.id, f.id],
                        )
                    )

    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda c: order[c.severity])
    return out
