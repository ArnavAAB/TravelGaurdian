"""The approval gate. Every approve/decline (voice agent over MCP, REST, dashboard, CLI) goes through here.

Rules:
- Only a *pending* proposal can be approved or declined. A declined or failed one is rejected (409).
- Approval is idempotent: approving an already-approved proposal returns the recorded outcome and only
  retries side effects that have not completed yet, so an email is never sent twice.
- Side effects after approval: the local itinerary is updated, a rescheduling email goes out as a reply
  in the original confirmation thread (to the controlled REBOOK_EMAIL_TO address), and the calendar
  event is updated. Each step is recorded in `proposal.actions`.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from .models import Flight
from .proposals import view
from .services import Services
from .store import ChangeLogEntry
from .tools import ToolError, decide

_LOCK = threading.RLock()  # one decision at a time: two "yes" calls must not both send email


class GateError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _find(svc: Services, proposal_id: str):
    trip = svc.store.load()
    prop = trip.proposal(proposal_id)
    if prop is None:
        raise GateError(404, f"No proposal {proposal_id}.")
    return trip, prop


def approve(svc: Services, proposal_id: str, channel: str, spoken_confirmation: Optional[str] = None) -> dict[str, Any]:
    with _LOCK:
        trip, prop = _find(svc, proposal_id)
        pid = prop.id
        if prop.status in ("declined", "failed"):
            raise GateError(409, f"Proposal {pid} is {prop.status}; only a pending proposal can be approved.")
        replay = prop.status != "pending"
        now = svc.now()
        if not replay:
            known = trip.calendar_ids_for(svc.calendar.name)
            old_events = [known.get(s) for s in prop.segment_ids]
            try:
                res = decide(svc.store, svc.provider, pid, True, decided_by=channel, now=now)
            except ToolError as e:
                raise GateError(404, str(e)) from e
            trip, prop = _find(svc, pid)
            if res["status"] == "failed":
                raise GateError(409, f"Proposal {pid} could not be applied: {res.get('error')}")
            prop.decided_at = now
            prop.spoken_confirmation = spoken_confirmation
            prop.new_segment_id = res["new_segment"]["id"]
            # Nothing is booked with the airline: a rescheduling request is emailed after approval.
            prop.result = (f"Approved by {channel}. Rescheduling to {res['new_segment']['flight_number']} "
                           "requested; awaiting airline confirmation.")
            prop.actions["itinerary"] = {"at": now.isoformat(), "replaced": prop.segment_ids,
                                         "new_segment": prop.new_segment_id, "old_event_ids": old_events}
            svc.store.save(trip)

        errors = []
        done_now = []
        for step, fn in (("email", _send_email), ("calendar", _update_calendar)):
            if step in prop.actions:
                continue
            try:
                prop.actions[step] = fn(svc, trip, prop)
                done_now.append(step)
            except Exception as e:  # recorded; a repeated approval retries just this step
                errors.append(f"{step}: {e}")
            svc.store.save(trip)
        if done_now:
            trip.log.append(ChangeLogEntry(at=now, action="approval_actions", detail=f"{pid}: {', '.join(done_now)}"))
            svc.store.save(trip)
        out = view(prop)
        out.update({"idempotent_replay": replay, "errors": errors})
        return out


def decline(svc: Services, proposal_id: str, channel: str, reason: str = "") -> dict[str, Any]:
    with _LOCK:
        trip, prop = _find(svc, proposal_id)
        if prop.status == "declined":
            out = view(prop)
            out["idempotent_replay"] = True
            return out
        if prop.status != "pending":
            raise GateError(409, f"Proposal {prop.id} is {prop.status}; only a pending proposal can be declined.")
        now = svc.now()
        decide(svc.store, svc.provider, prop.id, False, decided_by=channel, now=now)
        trip, prop = _find(svc, prop.id)
        prop.decided_at = now
        prop.actions["decline"] = {"at": now.isoformat(), "channel": channel, "reason": reason}
        svc.store.save(trip)
        out = view(prop)
        out["idempotent_replay"] = False
        return out


def _send_email(svc: Services, trip, prop) -> dict:
    if not prop.email_ref or not prop.email_draft:
        raise RuntimeError("no original confirmation email recorded for this booking")
    to = svc.rebook_recipient()
    sent = svc.mail.reply(prop.email_ref, to, prop.email_draft["body"])
    return {"at": svc.now().isoformat(), **sent}


def _update_calendar(svc: Services, trip, prop) -> dict:
    new: Optional[Flight] = trip.flight(prop.new_segment_id or "")
    if new is None:
        raise RuntimeError("new flight segment missing from itinerary")
    old_ids = prop.actions.get("itinerary", {}).get("old_event_ids", [])
    note = f"Rebooking requested via TravelGuardian proposal {prop.id}; awaiting airline confirmation."
    if trip.calendar_provider != svc.calendar.name:
        old_ids = []  # recorded for another calendar; the provider looks events up itself
    res = svc.calendar.replace_flights(old_ids, new, note)
    known = trip.calendar_ids_for(svc.calendar.name)
    for seg in prop.segment_ids:
        known.pop(seg, None)
    known[new.id] = res["updated"]
    return {"at": svc.now().isoformat(), "provider": svc.calendar.name, **res}
