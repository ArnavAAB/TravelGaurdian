"""The disruption playbook without a model: same tools, fixed decision rules, template drafts.

Used by `tg respond --offline` / `tg demo --offline` so the demo works with no API key or
network, and as a baseline to compare the agent against.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import render
from datetime import timedelta

from .itinerary import detect_conflicts, fmt, ground_transfer, parse_iso, tz_of
from .tools import ToolBox


def _j(tb: ToolBox, name: str, **args) -> dict:
    text, err = tb.call(name, args)
    data = json.loads(text)
    if err:
        raise RuntimeError(f"{name}: {data.get('error')}")
    return data


def respond(tb: ToolBox) -> None:
    render.step(1, "Detect: checking live flight status")
    st = _j(tb, "check_flight_statuses")
    disrupted = [c for c in st["changed_since_last_check"] if c["status"] != "on_time"]
    if not disrupted:
        disrupted = [
            {"segment_id": k, **v} for k, v in st["statuses"].items() if v["status"] != "on_time"
        ]
    if not disrupted:
        print("  All flights on time. Nothing to do.")
        return
    for d in disrupted:
        why = f" ({d['reason']})" if d.get("reason") else ""
        print(f"  {render.c(d['flight_number'], '1;31')} is {d['status']}, {d['delay_minutes']} min{why}")

    trip = tb.store.load()
    for d in disrupted:
        inbound = trip.flight(d["segment_id"])
        render.step(2, f"Check the connection after {inbound.flight_number}")
        conn = _j(tb, "assess_connection", inbound_segment_id=inbound.id)
        if conn.get("connection", True) is None:
            print("  No onward connection.")
            continue
        outbound = trip.flight(conn["outbound"])
        print(f"  {inbound.flight_number} now lands {fmt(parse_iso(conn['inbound_arrival']), inbound.destination)}")
        print(f"  {outbound.flight_number} departs {fmt(parse_iso(conn['outbound_departure']), outbound.origin)}")

        render.step(3, "Will you make it?")
        color = {"low": "32", "medium": "33", "high": "1;31", "missed": "1;31"}[conn["risk"]]
        print(f"  Risk: {render.c(conn['risk'].upper(), color)}. {conn['explanation']}")
        if conn["risk"] in ("low", "medium"):
            print("  Keeping the current booking.")
            continue

        render.step(4, "Search alternatives")
        options: list[tuple[list[str], dict]] = []
        hint = conn["rebook_search_hint"]
        for a in _j(tb, "search_alternatives", **hint)["alternatives"]:
            options.append(([outbound.id], a))
        now = tb.provider.now() if hasattr(tb.provider, "now") else None
        if now and now < parse_iso(st["statuses"][inbound.id]["estimated_departure"]):
            # Traveler hasn't left yet: a nonstop from the origin can replace both legs.
            for a in _j(tb, "search_alternatives", origin=inbound.origin, destination=outbound.destination, earliest_departure=now.isoformat())["alternatives"]:
                options.append(([inbound.id, outbound.id], a))
        if not options:
            print("  No alternatives with seats. Drafting a request to the airline instead.")
            _draft_airline_request(tb, inbound, outbound, None)
            continue

        scored = []
        for replace, a in options:
            pv = _j(tb, "preview_change", replace_segment_ids=replace, alternative_id=a["id"])
            crit = [c for c in pv["conflicts_after_change"] if c["severity"] == "critical"]
            scored.append((len(crit), not a["protected"], a["arrives"], replace, a, pv))
        scored.sort(key=lambda x: x[:3])
        for n_crit, _, _, replace, a, pv in scored:
            dep = fmt(parse_iso(a["departs"]), a["origin"])
            arr = fmt(parse_iso(a["arrives"]), a["destination"])
            cost = "free" if a["protected"] else f"+{a['fare_difference']:,.0f} {a['currency']}"
            flag = render.c(f"{n_crit} critical conflict(s)", "31") if n_crit else render.c("no critical conflicts", "32")
            print(f"  {a['flight_number']:7} {a['origin']}->{a['destination']}  dep {dep}  arr {arr}  {cost:>10}  seats {a['seats_available']}  · {flag}")

        best = scored[0]
        _, _, _, replace, alt, pv = best
        render.step(5, f"Revised itinerary with {alt['flight_number']}")
        for it in pv["revised_items"]:
            if it["type"] == "flight":
                print(f"  ✈ {it['title']}  {render.dim(it['detail'].split(' · ')[0])}")
                if it.get("status"):
                    print(f"      {render.c(it['status'], '33')}")
        if pv["resolved_conflicts"]:
            print(render.c("  Resolves: ", "32") + "; ".join(pv["resolved_conflicts"]))
        for c in pv["new_conflicts"]:
            print(render.c("  New: ", "33") + c["message"])

        reason = (
            f"{inbound.flight_number} is {d['delay_minutes']} min late, leaving {conn['layover_minutes']} min for a "
            f"{conn['minimum_connection_minutes']} min connection. {alt['flight_number']} is the earliest "
            f"{'free ' if alt['protected'] else ''}option that keeps your calendar intact."
        )
        result = _j(tb, "request_booking_change", replace_segment_ids=replace, alternative_id=alt["id"], reason=reason)
        status = result["status"]
        print(f"  Booking change: {render.bold(status)}" + (f" ({result.get('error') or result.get('note', '')})" if status != "applied" else ""))

        # Drafts the traveler sends themselves.
        arrival = parse_iso(alt["arrives"])
        trip = tb.store.load()
        gaps = [c for c in detect_conflicts(trip, tb.store.calendar()) if c.kind == "hotel_gap"]
        if status == "applied":
            for h in trip.hotels:
                if h.check_in == arrival.astimezone(tz_of(alt["destination"])).date():
                    eta = ground_transfer(_as_flight(alt), h.address or h.city)["total_minutes"]
                    extra = (
                        f"\n\nI also need one more night: {gaps[0].message.split('night(s) of ')[-1].rstrip('.')}. "
                        "Could you extend the reservation if a room is available?"
                        if gaps and h.city in gaps[0].message
                        else ""
                    )
                    _j(
                        tb,
                        "save_draft",
                        kind="hotel_notice",
                        to=f"Reservations, {h.name}",
                        subject=f"Reservation {h.confirmation}: revised arrival time",
                        body=(
                            f"Hello,\n\nMy flight has changed. I now land at {alt['destination']} at "
                            f"{fmt(arrival, alt['destination'])} and expect to reach the hotel around "
                            f"{(arrival + timedelta(minutes=eta)).astimezone(tz_of(alt['destination'])):%H:%M} on {h.check_in:%A %d %B}. Could you note this on reservation {h.confirmation}, and let me "
                            f"know if early check-in is possible?{extra}\n\nThank you,\n{trip.traveler}"
                        ),
                        related_ids=[h.id],
                    )
        cal_conflicts = [c for c in pv["new_conflicts"] if c["kind"] == "calendar"]
        if status == "applied" and cal_conflicts:
            ev = next(e for e in tb.store.calendar() if e.id in cal_conflicts[0]["items"])
            _j(
                tb,
                "save_draft",
                kind="meeting_reschedule",
                to=", ".join(ev.attendees),
                subject=f"Reschedule request: {ev.title}",
                body=f"Hi all,\n\nMy flight has been disrupted and I now land at {fmt(arrival, alt['destination'])}. "
                f"Could we move '{ev.title}' to later the same day?\n\nApologies,\n{trip.traveler}",
                related_ids=[ev.id],
            )
        if status != "applied":
            _draft_airline_request(tb, inbound, outbound, alt)


def _draft_airline_request(tb: ToolBox, inbound, outbound, alt) -> None:
    trip = tb.store.load()
    ask = (
        f"Please rebook me on {alt['flight_number']} departing {fmt(parse_iso(alt['departs']), alt['origin'])}."
        if alt
        else f"Please rebook me on the next available flight from {outbound.origin} to {outbound.destination}."
    )
    tb.call(
        "save_draft",
        {
            "kind": "rebooking_request",
            "to": f"{outbound.airline} customer service",
            "subject": f"Confirmation {outbound.confirmation}: delayed {inbound.flight_number}, at risk of missing {outbound.flight_number}",
            "body": f"Hello,\n\nMy flight {inbound.flight_number} is delayed and I will likely miss my connection "
            f"{outbound.flight_number} ({outbound.origin}->{outbound.destination}). {ask} "
            f"As the delay is on your side, I understand this is at no additional cost.\n\n"
            f"Passenger: {outbound.passenger}\nConfirmation: {outbound.confirmation}\n\nThank you,\n{trip.traveler}",
            "related_ids": [inbound.id, outbound.id],
        },
    )


def _as_flight(alt: dict):
    from .models import Flight

    return Flight(id=alt["id"], confirmation="", airline=alt["airline"], flight_number=alt["flight_number"], origin=alt["origin"],
                  destination=alt["destination"], departs=parse_iso(alt["departs"]), arrives=parse_iso(alt["arrives"]), passenger="")
