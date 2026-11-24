"""The tools TravelGuardian exposes, shared by the agent and the MCP server.

Booking changes go through `request_booking_change`, which calls an Approver.
The model never gets a path that changes a booking without a human decision.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .itinerary import assess_connection, connections, detect_conflicts, fmt, ground_transfer, mct_minutes, timeline
from .models import Draft, FlightStatus, ProposedChange
from .providers import FlightProvider
from .store import ChangeLogEntry, Store, Trip

# Returns True (approved), False (declined) or None (deferred: leave pending for `tg approve`).
Approver = Callable[[dict], Optional[bool]]


def deny_all(_: dict) -> Optional[bool]:
    return False


def defer_all(_: dict) -> Optional[bool]:
    return None


class ToolError(Exception):
    pass


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o))


def _parse_dt(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be an ISO 8601 datetime with offset, got {value!r}")
    if dt.tzinfo is None:
        raise ToolError(f"{field} must include a UTC offset, e.g. 2026-12-03T16:35:00-06:00")
    return dt


TOOL_SPECS: list[dict] = [
    {
        "name": "get_itinerary",
        "description": "Return the traveler's full itinerary (flights, hotels, calendar events, time-sorted) and every detected conflict, using the latest known flight times.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_flight_statuses",
        "description": "Fetch live status for every booked flight. Reports delays/cancellations and which ones changed since the last check.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "assess_connection",
        "description": "For an inbound flight segment, compute whether the traveler can still make the onward connection given live times and the airport's minimum connection time. Returns risk: low, medium, high or missed.",
        "input_schema": {
            "type": "object",
            "properties": {"inbound_segment_id": {"type": "string", "description": "Segment id of the arriving flight, e.g. seg-UA1542-20261203"}},
            "required": ["inbound_segment_id"],
        },
    },
    {
        "name": "search_alternatives",
        "description": "Search flights between two airports departing no earlier than a given time. Results list protected (free, same-carrier) options first, then by arrival.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "IATA code"},
                "destination": {"type": "string", "description": "IATA code"},
                "earliest_departure": {"type": "string", "description": "ISO 8601 with offset. For a missed connection use inbound estimated arrival + minimum connection time."},
                "latest_arrival": {"type": "string", "description": "Optional ISO 8601 with offset"},
            },
            "required": ["origin", "destination", "earliest_departure"],
        },
    },
    {
        "name": "preview_change",
        "description": "Show the revised itinerary and its conflicts if the given segments were replaced by an alternative flight. Changes nothing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "replace_segment_ids": {"type": "array", "items": {"type": "string"}, "description": "Segments the alternative replaces (one leg, or both legs for a nonstop)."},
                "alternative_id": {"type": "string"},
            },
            "required": ["replace_segment_ids", "alternative_id"],
        },
    },
    {
        "name": "request_booking_change",
        "description": "Ask the traveler to approve replacing segments with an alternative flight. The change is applied only if they approve; otherwise it is declined or left pending. Call preview_change first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "replace_segment_ids": {"type": "array", "items": {"type": "string"}},
                "alternative_id": {"type": "string"},
                "reason": {"type": "string", "description": "One or two sentences the traveler will read when deciding."},
            },
            "required": ["replace_segment_ids", "alternative_id", "reason"],
        },
    },
    {
        "name": "estimate_ground_transfer",
        "description": "Estimate minutes from a flight landing to reaching an address in the arrival city (arrival processing plus road transfer). Use it to judge whether a revised arrival still works for a meeting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string", "description": "Arriving flight segment id, or an alternative id"},
                "destination": {"type": "string", "description": "Address or place name"},
            },
            "required": ["segment_id", "destination"],
        },
    },
    {
        "name": "list_bookings",
        "description": "Bookings organised by confirmation number: which flights and hotels each confirmation covers, and who to contact to change it.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_draft",
        "description": "Save a draft message (rebooking or cancellation request, hotel notice, meeting reschedule). Drafts are never sent automatically; the traveler reviews and sends them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["rebooking_request", "cancellation_request", "hotel_notice", "meeting_reschedule", "other"]},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "related_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "to", "subject", "body"],
        },
    },
]


class ToolBox:
    def __init__(self, store: Store, provider: FlightProvider, approver: Approver = deny_all, now: Optional[Callable[[], datetime]] = None):
        self.store = store
        self.provider = provider
        self.approver = approver
        self.now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool. Returns (json_text, is_error)."""
        fn = getattr(self, f"t_{name}", None)
        if fn is None or not any(s["name"] == name for s in TOOL_SPECS):
            return _dump({"error": f"unknown tool {name}"}), True
        spec = next(s for s in TOOL_SPECS if s["name"] == name)
        if not isinstance(args, dict):
            return _dump({"error": "tool input must be a JSON object"}), True
        missing = [k for k in spec["input_schema"]["required"] if k not in args]
        if missing:
            return _dump({"error": f"missing required field(s): {', '.join(missing)}"}), True
        try:
            return _dump(fn(**args)), False
        except ToolError as e:
            return _dump({"error": str(e)}), True
        except TypeError as e:
            return _dump({"error": f"bad arguments: {e}"}), True

    # ------------------------------------------------------------ tools
    def t_get_itinerary(self) -> dict:
        trip = self.store.load()
        cal = self.store.calendar()
        return {
            "traveler": trip.traveler,
            "items": timeline(trip, cal),
            "flights": [f.model_dump(mode="json") for f in trip.sorted_flights()],
            "hotels": [h.model_dump(mode="json") for h in trip.hotels],
            "latest_status": {k: v.model_dump(mode="json") for k, v in trip.last_status.items()},
            "conflicts": [c.model_dump() for c in detect_conflicts(trip, cal)],
            "pending_changes": [p.model_dump(mode="json") for p in trip.proposals if p.status == "pending"],
        }

    def t_check_flight_statuses(self) -> dict:
        trip = self.store.load()
        changes = refresh_statuses(trip, self.provider, self.now())
        self.store.save(trip)
        return {
            "statuses": {k: v.model_dump(mode="json") for k, v in trip.last_status.items()},
            "changed_since_last_check": changes,
        }

    def t_assess_connection(self, inbound_segment_id: str) -> dict:
        trip = self.store.load()
        inbound = trip.flight(inbound_segment_id)
        if inbound is None:
            raise ToolError(f"no segment {inbound_segment_id}")
        pair = next(((a, b) for a, b in connections(trip) if a.id == inbound.id), None)
        if pair is None:
            return {"inbound": inbound.id, "connection": None, "note": "No onward connection from this flight."}
        a, b = pair
        sa, sb = self.provider.status(a), self.provider.status(b)
        r = assess_connection(a, b, sa, sb)
        r["rebook_search_hint"] = {
            "origin": b.origin,
            "destination": b.destination,
            "earliest_departure": (sa.estimated_arrival + timedelta(minutes=mct_minutes(a.destination, a, b))).isoformat(),
        }
        return r

    def t_search_alternatives(self, origin: str, destination: str, earliest_departure: str, latest_arrival: Optional[str] = None) -> dict:
        ed = _parse_dt(earliest_departure, "earliest_departure")
        la = _parse_dt(latest_arrival, "latest_arrival") if latest_arrival else None
        alts = self.provider.search(origin.upper(), destination.upper(), ed, la)
        return {"count": len(alts), "alternatives": [a.model_dump(mode="json") for a in alts]}

    def t_preview_change(self, replace_segment_ids: list[str], alternative_id: str) -> dict:
        trip = self.store.load()
        revised = _apply_preview(trip, self.provider, replace_segment_ids, alternative_id)
        cal = self.store.calendar()
        before = detect_conflicts(trip, cal)
        after = detect_conflicts(revised, cal)
        before_keys = {_conflict_key(c) for c in before}
        after_keys = {_conflict_key(c) for c in after}
        return {
            "revised_items": timeline(revised, cal),
            "conflicts_after_change": [c.model_dump() for c in after],
            "new_conflicts": [c.model_dump() for c in after if _conflict_key(c) not in before_keys],
            "resolved_conflicts": [c.message for c in before if _conflict_key(c) not in after_keys],
        }

    def t_request_booking_change(self, replace_segment_ids: list[str], alternative_id: str, reason: str) -> dict:
        trip = self.store.load()
        old = [trip.flight(s) for s in replace_segment_ids]
        if not old or any(f is None for f in old):
            raise ToolError(f"unknown segment(s) in {replace_segment_ids}")
        alt = self.provider.offer(alternative_id)
        if alt is None:
            raise ToolError(f"unknown alternative {alternative_id}")
        prop = ProposedChange(
            id=trip.new_proposal_id(),
            segment_ids=replace_segment_ids,
            alternative_id=alternative_id,
            reason=reason,
            created_at=self.now(),
        )
        trip.proposals.append(prop)
        self.store.save(trip)
        summary = {
            "proposal_id": prop.id,
            "replace": [f"{f.flight_number} {f.origin}->{f.destination}, departs {fmt(f.departs, f.origin)}" for f in old],
            "with": f"{alt.airline} {alt.flight_number} {alt.origin}->{alt.destination}, departs {fmt(alt.departs, alt.origin)}, arrives {fmt(alt.arrives, alt.destination)}",
            "cost": "no charge (protected rebooking)" if alt.protected else f"{alt.fare_difference:,.0f} {alt.currency} fare difference",
            "reason": reason,
        }
        decision = self.approver(summary)
        if decision is None:
            return {"proposal_id": prop.id, "status": "pending", "note": "Waiting for the traveler to approve with `tg approve`."}
        return decide(self.store, self.provider, prop.id, decision, decided_by="traveler", now=self.now())

    def t_estimate_ground_transfer(self, segment_id: str, destination: str) -> dict:
        from .models import Flight

        trip = self.store.load()
        f = trip.flight(segment_id)
        if f is None:
            alt = self.provider.offer(segment_id)
            if alt is None:
                raise ToolError(f"no segment or alternative {segment_id}")
            f = Flight(id=alt.id, confirmation="", airline=alt.airline, flight_number=alt.flight_number, origin=alt.origin,
                       destination=alt.destination, departs=alt.departs, arrives=alt.arrives, passenger="")
        st = trip.last_status.get(f.id)
        landing = st.estimated_arrival if st else f.arrives
        r = ground_transfer(f, destination)
        r["lands"] = fmt(landing, f.destination)
        r["earliest_at_destination"] = fmt(landing + timedelta(minutes=r["total_minutes"]), f.destination)
        return r

    def t_list_bookings(self) -> dict:
        trip = self.store.load()
        groups: dict[str, dict] = {}
        for f in trip.sorted_flights():
            g = groups.setdefault(f.confirmation, {"confirmation": f.confirmation, "provider": f.airline, "type": "flight", "items": []})
            g["items"].append(f"{f.flight_number} {f.origin}->{f.destination} {fmt(f.departs, f.origin)}")
        for h in trip.hotels:
            groups[h.confirmation] = {
                "confirmation": h.confirmation, "provider": h.name, "type": "hotel",
                "items": [f"{h.city}: {h.check_in:%a %d %b} -> {h.check_out:%a %d %b} ({(h.check_out - h.check_in).days} nights)"],
            }
        return {"traveler": trip.traveler, "bookings": list(groups.values()), "drafts": [d.model_dump(mode="json") for d in trip.drafts]}

    def t_save_draft(self, kind: str, to: str, subject: str, body: str, related_ids: Optional[list[str]] = None) -> dict:
        trip = self.store.load()
        d = Draft(id=f"drf-{secrets.token_hex(3)}", kind=kind, to=to, subject=subject, body=body, created_at=self.now(), related=related_ids or [])
        trip.drafts.append(d)
        trip.log.append(ChangeLogEntry(at=self.now(), action="draft", detail=f"{kind}: {subject}"))
        self.store.save(trip)
        return {"draft_id": d.id, "status": "saved as draft, not sent"}


# ---------------------------------------------------------------- helpers


def _conflict_key(c) -> tuple:
    """Identity of a conflict that survives swapping one flight for another."""
    return (c.kind, tuple(sorted(i for i in c.items if not i.startswith(("seg-", "preview-")))))


def _apply_preview(trip: Trip, provider: FlightProvider, replace_ids: list[str], alternative_id: str) -> Trip:
    from .models import Flight

    alt = provider.offer(alternative_id)
    if alt is None:
        raise ToolError(f"unknown alternative {alternative_id}")
    old = [trip.flight(s) for s in replace_ids]
    if not old or any(f is None for f in old):
        raise ToolError(f"unknown segment(s) in {replace_ids}")
    revised = trip.model_copy(deep=True)
    revised.flights = [f for f in revised.flights if f.id not in replace_ids]
    revised.flights.append(
        Flight(
            id=f"preview-{alt.flight_number}",
            confirmation=old[0].confirmation,
            airline=alt.airline,
            flight_number=alt.flight_number,
            origin=alt.origin,
            destination=alt.destination,
            departs=alt.departs,
            arrives=alt.arrives,
            passenger=old[0].passenger,
        )
    )
    for s in replace_ids:
        revised.last_status.pop(s, None)
    return revised


def refresh_statuses(trip: Trip, provider: FlightProvider, now: datetime) -> list[dict]:
    """Update trip.last_status; return the flights whose status or times changed."""
    changes = []
    for f in trip.sorted_flights():
        new = provider.status(f)
        old: Optional[FlightStatus] = trip.last_status.get(f.id)
        if old is None or old.status != new.status or old.estimated_departure != new.estimated_departure or old.estimated_arrival != new.estimated_arrival:
            if old is not None or new.status != "on_time":
                changes.append(
                    {
                        "segment_id": f.id,
                        "flight_number": f.flight_number,
                        "status": new.status,
                        "delay_minutes": new.delay_minutes,
                        "estimated_departure": new.estimated_departure.isoformat(),
                        "estimated_arrival": new.estimated_arrival.isoformat(),
                        "reason": new.reason,
                    }
                )
                trip.log.append(ChangeLogEntry(at=now, action="status", detail=f"{f.flight_number}: {new.status} ({new.delay_minutes} min)"))
        trip.last_status[f.id] = new
    return changes


def decide(store: Store, provider: FlightProvider, proposal_id: str, approved: bool, decided_by: str, now: datetime) -> dict:
    """Record a human decision on a pending proposal and apply it if approved."""
    trip = store.load()
    prop = next((p for p in trip.proposals if p.id == proposal_id), None)
    if prop is None:
        raise ToolError(f"no proposal {proposal_id}")
    if prop.status != "pending":
        return {"proposal_id": prop.id, "status": prop.status, "note": "already decided"}
    prop.decided_by = decided_by
    if not approved:
        prop.status = "declined"
        trip.log.append(ChangeLogEntry(at=now, action="declined", detail=f"{prop.id} by {decided_by}"))
        store.save(trip)
        return {"proposal_id": prop.id, "status": "declined", "note": "The traveler declined. Nothing was changed."}
    old = [trip.flight(s) for s in prop.segment_ids]
    if any(f is None for f in old):
        prop.status, prop.result = "failed", "segments changed since proposal"
        store.save(trip)
        return {"proposal_id": prop.id, "status": "failed", "error": prop.result}
    try:
        new = provider.rebook(old[0], prop.alternative_id)
    except ValueError as e:
        prop.status, prop.result = "failed", str(e)
        store.save(trip)
        return {"proposal_id": prop.id, "status": "failed", "error": str(e)}
    trip.flights = [f for f in trip.flights if f.id not in prop.segment_ids] + [new]
    for s in prop.segment_ids:
        trip.last_status.pop(s, None)
    prop.status = "applied"
    prop.result = f"Booked {new.flight_number}, confirmation {new.confirmation}"
    trip.log.append(ChangeLogEntry(at=now, action="rebooked", detail=f"{', '.join(f.flight_number for f in old)} -> {new.flight_number} ({decided_by} approved)"))
    store.save(trip)
    return {"proposal_id": prop.id, "status": "applied", "new_segment": new.model_dump(mode="json")}
