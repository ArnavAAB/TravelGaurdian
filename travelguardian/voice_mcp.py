"""MCP server the ElevenLabs voice agent connects to (Streamable HTTP, mounted at /mcp by server.py).

Three tools, all backed by the approval gate in approval.py:
  get_proposal(proposal_id)                      read the proposal to explain it
  approve_proposal(proposal_id, traveler_said)   only after an explicit spoken "yes"
  decline_proposal(proposal_id, reason)          on "no"
The /mcp endpoint requires `Authorization: Bearer <TG_API_TOKEN>` (ElevenLabs "Secret Token").
"""

from __future__ import annotations

import json
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import approval
from .proposals import view
from .services import Services, build_services

# Replaced in tests; the server builds fresh services per call so config changes apply immediately.
services_factory: Callable[[], Services] = build_services

mcp = MCPServer(
    name="travelguardian-voice",
    instructions=(
        "Tools for a phone call about a flight disruption. Call get_proposal first and explain it. "
        "Call approve_proposal only after the traveler clearly says yes to this specific change; "
        "call decline_proposal if they say no. If the answer is unclear, ask again; never guess."
    ),
)


def _dump(data: Any) -> str:
    return json.dumps(data, default=str)


@mcp.tool(description=(
    "Read rebooking proposal details: the delayed flight, the connection that will be missed, the "
    "recommended replacement flight with times, why it was chosen, and the proposal status."
))
def get_proposal(proposal_id: str) -> str:
    svc = services_factory()
    prop = svc.store.load().proposal(proposal_id)
    if prop is None:
        return _dump({"ok": False, "error": f"No proposal {proposal_id}"})
    v = view(prop)
    keep = ("id", "status", "summary", "rationale", "context", "decided_by", "complete")
    return _dump({"ok": True, **{k: v[k] for k in keep}})


@mcp.tool(description=(
    "Approve the rebooking proposal. ONLY call this after the traveler has explicitly said yes to this "
    "exact change on this call. traveler_said must quote the traveler's words of approval."
))
def approve_proposal(proposal_id: str, traveler_said: str) -> str:
    if not traveler_said.strip():
        return _dump({"ok": False, "error": "traveler_said is required: quote the traveler's explicit yes."})
    try:
        res = approval.approve(services_factory(), proposal_id, channel="voice agent (MCP)",
                               spoken_confirmation=traveler_said.strip())
    except approval.GateError as e:
        return _dump({"ok": False, "error": e.message})
    return _dump({"ok": True, "status": res["status"], "complete": res["complete"], "errors": res["errors"],
                  "already_approved": res["idempotent_replay"]})


@mcp.tool(description="Decline the rebooking proposal because the traveler said no. Nothing is changed.")
def decline_proposal(proposal_id: str, reason: str = "") -> str:
    try:
        res = approval.decline(services_factory(), proposal_id, channel="voice agent (MCP)", reason=reason)
    except approval.GateError as e:
        return _dump({"ok": False, "error": e.message})
    return _dump({"ok": True, "status": res["status"]})
