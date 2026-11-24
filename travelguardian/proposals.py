"""Disruption -> rebooking proposal, decided by rules.

`evaluate()` refreshes flight status, runs the deterministic risk engine on every connection and,
for a connection that will be missed (risk high or missed), picks the best alternative and stores a
pending proposal (TG-402, ...). Ranking is plain code: fewest critical conflicts after the change
(missed meetings, overlaps), then protected rebookings, then earliest arrival. Nemotron only writes
the explanation and the email text for the option the rules picked.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from .ingest import message_from_bytes
from .itinerary import assess_connection, city_of, connections, fmt, mct_minutes, tz_of
from .llm import write_proposal_text
from .mail import ref_for, reply_subject
from .models import Alternative, EmailRef, Flight, ProposedChange
from .services import Services
from .store import SAMPLES, ChangeLogEntry, Trip
from .tools import ToolBox, defer_all, refresh_statuses

OPEN = ("pending", "approved")


def _short(dt, airport: str) -> str:
    return dt.astimezone(tz_of(airport)).strftime("%H:%M %a %d %b")


def email_ref_for(trip: Trip, flight: Flight) -> Optional[EmailRef]:
    """The confirmation email a flight was read from (synced Gmail message, or a sample .eml)."""
    if flight.source and flight.source in trip.emails:
        return trip.emails[flight.source]
    if flight.source and flight.source.startswith("inbox/"):
        path: Path = SAMPLES / flight.source
        if path.exists():
            return ref_for(message_from_bytes(path.read_bytes(), flight.source), "local")
    return None


def _usable(alt: Alternative, origin: str, destination: str, earliest) -> bool:
    """Never trust a provider blindly: right route, catchable, and a sane schedule."""
    return (alt.origin == origin and alt.destination == destination and alt.departs >= earliest
            and alt.arrives > alt.departs and alt.seats_available > 0)


def _rank(svc: Services, inbound: Flight, outbound: Flight, inbound_status, now) -> Optional[tuple]:
    tb = ToolBox(svc.store, svc.provider, approver=defer_all, now=svc.now)
    arrival = inbound_status.estimated_arrival if inbound_status else inbound.arrives
    earliest = arrival + timedelta(minutes=mct_minutes(inbound.destination, inbound, outbound))
    options: list[tuple[list[str], Alternative]] = [
        ([outbound.id], a) for a in svc.provider.search(outbound.origin, outbound.destination, earliest)
        if _usable(a, outbound.origin, outbound.destination, earliest)
    ]
    departs = inbound_status.estimated_departure if inbound_status else inbound.departs
    if now < departs:  # not yet left: a direct flight from the origin can replace both legs
        options += [([inbound.id, outbound.id], a)
                    for a in svc.provider.search(inbound.origin, outbound.destination, now)
                    if _usable(a, inbound.origin, outbound.destination, now)]
    scored = []
    for replace, alt in options:
        pv = tb.t_preview_change(replace, alt.id)
        critical = [c for c in pv["conflicts_after_change"] if c["severity"] == "critical"]
        scored.append((len(critical), not alt.protected, alt.arrives, replace, alt, pv))
    if not scored:
        return None
    return min(scored, key=lambda s: s[:3])


def _facts(trip: Trip, pid: str, inbound: Flight, outbound: Flight, st, risk: dict, replace: list[str],
           alt: Alternative, preview: dict) -> dict[str, Any]:
    replaced = [trip.flight(s) for s in replace]
    cal_now = [c["message"] for c in preview["conflicts_after_change"] if c["kind"] == "calendar"]
    resolved = [m for m in preview["resolved_conflicts"]]
    if cal_now:
        calendar_note = "Heads up: " + cal_now[0]
    elif resolved:
        calendar_note = "It keeps your calendar commitments reachable."
    else:
        calendar_note = None
    arrival = st.estimated_arrival if st else inbound.arrives
    return {
        "proposal_id": pid,
        "traveler": trip.traveler or inbound.passenger,
        "confirmation": outbound.confirmation,
        "delayed_flight": inbound.flight_number,
        "delayed_route": f"{inbound.origin}-{inbound.destination}",
        "delay_minutes": st.delay_minutes if st else 0,
        "delay_reason": (st.reason if st else None) or "",
        "new_arrival": fmt(arrival, inbound.destination),
        "connection_airport": inbound.destination,
        "connection_city": city_of(inbound.destination),
        "missed_flight": outbound.flight_number,
        "missed_departure": fmt(outbound.departs, outbound.origin),
        "layover_minutes": risk["layover_minutes"],
        "mct_minutes": risk["minimum_connection_minutes"],
        "risk": risk["risk"],
        "risk_explanation": risk["explanation"],
        "replaces": [f"{f.flight_number} {f.origin}-{f.destination}" for f in replaced if f],
        "alternative": {
            "id": alt.id,
            "airline": alt.airline,
            "flight_number": alt.flight_number,
            "origin": alt.origin,
            "destination": alt.destination,
            "departs": fmt(alt.departs, alt.origin),
            "arrives": fmt(alt.arrives, alt.destination),
            "departs_short": _short(alt.departs, alt.origin),
            "arrives_short": _short(alt.arrives, alt.destination),
        },
        "calendar_note": calendar_note,
        "calendar_conflicts_after_change": cal_now,
        "fare_note": "Fare and seat availability are not verified until the airline confirms.",
    }


def evaluate(svc: Services) -> dict[str, Any]:
    """Run the risk engine now. Creates at most one open proposal per threatened connection."""
    now = svc.now()
    trip = svc.store.load()
    changes = refresh_statuses(trip, svc.provider, now)
    svc.store.save(trip)

    created: list[str] = []
    existing: list[str] = []
    assessments: list[dict] = []
    for inbound, outbound in connections(trip):
        st_in, st_out = trip.last_status.get(inbound.id), trip.last_status.get(outbound.id)
        risk = assess_connection(inbound, outbound, st_in, st_out)
        assessments.append(risk)
        if risk["risk"] not in ("high", "missed"):
            continue
        open_prop = next((p for p in trip.proposals if p.status in OPEN and outbound.id in p.segment_ids), None)
        if open_prop:
            existing.append(open_prop.id)
            continue
        best = _rank(svc, inbound, outbound, st_in, now)
        if best is None:
            trip.log.append(ChangeLogEntry(at=now, action="no_alternative",
                                           detail=f"No alternative with seats for {outbound.flight_number}"))
            continue
        _, _, _, replace, alt, preview = best

        trip = svc.store.load()  # preview read the store; reload before writing
        pid = trip.new_proposal_id()
        facts = _facts(trip, pid, inbound, outbound, st_in, risk, replace, alt, preview)
        text, source = write_proposal_text(svc.llm(), facts)
        ref = email_ref_for(trip, outbound)
        prop = ProposedChange(
            id=pid,
            segment_ids=replace,
            alternative_id=alt.id,
            reason=risk["explanation"],
            created_at=now,
            context={**facts, "text_source": source},
            rationale=text.rationale,
            email_ref=ref,
            email_draft={"subject": reply_subject(ref.subject if ref else f"Booking {outbound.confirmation}"),
                         "body": text.email_body},
        )
        trip.proposals.append(prop)
        trip.log.append(ChangeLogEntry(at=now, action="proposed",
                                       detail=f"{pid}: {', '.join(facts['replaces'])} -> {alt.flight_number}"))
        svc.store.save(trip)
        created.append(pid)
    return {"status_changes": changes, "connections": assessments, "created": created, "existing": existing}


def view(prop: ProposedChange) -> dict[str, Any]:
    """Everything the dashboard and the voice agent need about one proposal."""
    alt = prop.context.get("alternative", {})
    summary = None
    if prop.context:
        c = prop.context
        summary = (
            f"{c['delayed_flight']} is delayed {c['delay_minutes']} minutes, leaving {c['layover_minutes']} minutes "
            f"to connect at {c['connection_airport']} against a {c['mct_minutes']}-minute minimum, so "
            f"{c['missed_flight']} will be missed. Proposed replacement: {alt.get('airline')} "
            f"{alt.get('flight_number')}, departing {alt.get('departs')}, arriving {alt.get('arrives')}."
        )
    data = prop.model_dump(mode="json")
    data["summary"] = summary
    data["complete"] = prop.status == "applied" and {"email", "calendar"} <= set(prop.actions)
    return data
