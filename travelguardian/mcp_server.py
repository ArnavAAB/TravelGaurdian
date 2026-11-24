"""TravelGuardian as an MCP server (stdio).

Add it to an MCP host that has email and calendar connectors.
The host reads your inbox and passes confirmation emails to
`ingest_email`; TravelGuardian keeps the itinerary, watches flights and plans fixes.

Booking changes requested over MCP are always left *pending*: an MCP client may answer
prompts on its own, so approval happens out of band with `tg approve <id>`.

    tg mcp
"""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from .providers import SimulatedProvider
from .store import Store
from .tools import ToolBox, defer_all

server = MCPServer(
    name="travelguardian",
    instructions=(
        "Trip itinerary, flight-status tracking, conflict detection, rebooking search and drafts. "
        "Booking changes are proposals that the traveler approves outside this chat with `tg approve <id>`."
    ),
)


def _tb() -> ToolBox:
    store = Store()
    if not store.scenario_path.exists():
        store.reset()
    provider = SimulatedProvider(store.scenario_path)
    return ToolBox(store, provider, approver=defer_all, now=provider.now)


def _run(name: str, **args) -> str:
    text, _ = _tb().call(name, {k: v for k, v in args.items() if v is not None})
    return text


@server.tool(description="Full itinerary (flights, hotels, calendar) with detected conflicts and pending changes.")
def get_itinerary() -> str:
    return _run("get_itinerary")


@server.tool(description="Bookings grouped by confirmation number, plus saved drafts.")
def list_bookings() -> str:
    return _run("list_bookings")


@server.tool(description="Fetch live status for every booked flight and report changes since the last check.")
def check_flight_statuses() -> str:
    return _run("check_flight_statuses")


@server.tool(description="Risk (low/medium/high/missed) of missing the connection after an inbound flight segment.")
def assess_connection(inbound_segment_id: str) -> str:
    return _run("assess_connection", inbound_segment_id=inbound_segment_id)


@server.tool(description="Search flights origin->destination departing at or after earliest_departure (ISO 8601 with offset).")
def search_alternatives(origin: str, destination: str, earliest_departure: str, latest_arrival: Optional[str] = None) -> str:
    return _run("search_alternatives", origin=origin, destination=destination, earliest_departure=earliest_departure, latest_arrival=latest_arrival)


@server.tool(description="Revised itinerary and conflicts if segments were replaced by an alternative. Changes nothing.")
def preview_change(replace_segment_ids: list[str], alternative_id: str) -> str:
    return _run("preview_change", replace_segment_ids=replace_segment_ids, alternative_id=alternative_id)


@server.tool(description="Estimate minutes from landing to reaching a destination (arrival processing + ground transfer).")
def estimate_ground_transfer(segment_id: str, destination: str) -> str:
    return _run("estimate_ground_transfer", segment_id=segment_id, destination=destination)


@server.tool(description="Propose a booking change. It stays pending until the traveler runs `tg approve <proposal_id>`.")
def request_booking_change(replace_segment_ids: list[str], alternative_id: str, reason: str) -> str:
    return _run("request_booking_change", replace_segment_ids=replace_segment_ids, alternative_id=alternative_id, reason=reason)


@server.tool(description="Save a draft (rebooking_request, cancellation_request, hotel_notice, meeting_reschedule, other). Never sent automatically.")
def save_draft(kind: str, to: str, subject: str, body: str, related_ids: Optional[list[str]] = None) -> str:
    return _run("save_draft", kind=kind, to=to, subject=subject, body=body, related_ids=related_ids)


@server.tool(description="Extract flight and hotel bookings from the text of a confirmation email and add them to the trip. Pass the full email body.")
def ingest_email(email_text: str, source: str = "mcp") -> str:
    import json
    import os

    from openai import OpenAI

    from .ingest import ingest_text

    store = Store()
    if not store.scenario_path.exists():
        store.reset(with_bookings=False)
    trip = store.load()
    api_key = os.environ.get("NVIDIA_API_KEY", "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")
    base_url = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    client = OpenAI(base_url=base_url, api_key=api_key)
    result = ingest_text(client, trip, email_text, source)
    store.save(trip)
    return json.dumps(result)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
