"""Nemotron writes the words; the rules have already made the decision.

Given the facts of a proposal (chosen by deterministic code), Nemotron writes the short rationale
the voice agent reads out and the body of the rescheduling email. If the model is unavailable or
returns something unusable, a deterministic template is used instead, so a proposal never depends
on the LLM being up.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field

MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"


class ProposalText(BaseModel):
    rationale: str = Field(description="2-3 short spoken sentences explaining why this alternative was chosen")
    email_body: str = Field(description="Plain-text rescheduling request email body, signed with the passenger name")


SYSTEM = (
    "You write for TravelGuardian, a travel assistant. A deterministic rules engine has ALREADY chosen the "
    "replacement flight; never suggest a different one and never contradict the facts you are given. "
    "Use only the facts provided: do not invent fares, seat availability, loyalty status, compensation or "
    "airline policies. Fares and seats are NOT verified; do not claim the change is free or guaranteed. "
    "rationale: 2-3 short sentences a voice agent will say on the phone, plain words, no lists, no markdown. "
    "email_body: a concise, polite plain-text email requesting the rebooking, quoting the booking reference, "
    "the disrupted flight, the missed connection and the requested replacement flight with its times, and "
    "stating that the traveler approved this change (quote the proposal id). Sign with the passenger name."
)


def template_text(facts: dict[str, Any]) -> ProposalText:
    alt = facts["alternative"]
    meet = facts.get("calendar_note")
    rationale = (
        f"{alt['flight_number']} leaves {alt['origin']} at {alt['departs_short']} and lands at "
        f"{alt['destination']} at {alt['arrives_short']}. "
        + (f"{meet} " if meet else "")
        + "It was the best-ranked option that still works with your connection and calendar."
    )
    replaces = " and ".join(facts["replaces"])
    body = (
        "Hello,\n\n"
        f"This is a rescheduling request for booking {facts['confirmation']} ({facts['traveler']}).\n\n"
        f"Flight {facts['delayed_flight']} is delayed {facts['delay_minutes']} minutes and now arrives "
        f"{facts['new_arrival']}. That leaves {facts['layover_minutes']} minutes to connect at "
        f"{facts['connection_airport']}, below the {facts['mct_minutes']}-minute minimum connection time, so the "
        f"connection to {facts['missed_flight']} ({facts['missed_departure']}) will be missed.\n\n"
        f"Please rebook {replaces} onto {alt['airline']} {alt['flight_number']} {alt['origin']}-{alt['destination']}, "
        f"departing {alt['departs']} and arriving {alt['arrives']}.\n\n"
        f"The traveler has approved this change (TravelGuardian proposal {facts['proposal_id']}).\n\n"
        f"Thank you,\n{facts['traveler']}"
    )
    return ProposalText(rationale=rationale, email_body=body)


def write_proposal_text(client: Optional[Any], facts: dict[str, Any]) -> tuple[ProposalText, str]:
    """Returns (text, source) where source is 'nemotron' or 'template'."""
    fallback = template_text(facts)
    if client is None:
        return fallback, "template"
    try:
        response = client.beta.chat.completions.parse(
            model=MODEL,
            max_tokens=2000,
            temperature=0.4,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps(facts, default=str, indent=1)}],
            response_format=ProposalText,
        )
        parsed = response.choices[0].message.parsed
    except Exception:
        return fallback, "template"
    if parsed is None or not parsed.rationale.strip() or not parsed.email_body.strip():
        return fallback, "template"
    # Guard against the model naming a different flight than the one the rules chose.
    if facts["alternative"]["flight_number"] not in parsed.email_body:
        return fallback, "template"
    return parsed, "nemotron"
