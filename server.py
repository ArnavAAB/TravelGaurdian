"""FastAPI backend for TravelGuardian: dashboard API, approval gate, voice-agent MCP server, webhooks.

Run:
    python server.py            # http://127.0.0.1:8000
    ngrok http 8000             # or: cloudflared tunnel --url http://localhost:8000
Then point the ElevenLabs agent's MCP server at https://<tunnel>/mcp with the TG_API_TOKEN as its
secret token, and its post-call webhook at https://<tunnel>/api/webhooks/elevenlabs.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.routing import Route

from travelguardian import approval, proposals, voice_mcp
from travelguardian.agent import MODEL, SYSTEM, _tools
from travelguardian.cli import _client
from travelguardian.models import CallRecord
from travelguardian.services import Services, build_services, flight_provider
from travelguardian.settings import load_dotenv, load_settings
from travelguardian.store import Store
from travelguardian.sync import sync_inbox
from travelguardian.tools import ToolBox, defer_all
from travelguardian.voice import ElevenLabsVoice, VoiceError, transcript_from, verify_signature

try:
    from travelguardian.airlabs_provider import AirLabsProvider
except ImportError:
    AirLabsProvider = None  # type: ignore[assignment]

load_dotenv()
log = logging.getLogger("travelguardian")

# Point this to the folder containing index.html and api.js
BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "web" if (BASE_DIR / "web" / "index.html").exists() else BASE_DIR


# ------------------------------------------------------------------
# Auth: a shared bearer token protects everything an outside caller can use to approve.
# ------------------------------------------------------------------
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
FORWARDED = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip")


def _token_ok(auth_header: Optional[str]) -> bool:
    expected = load_settings().api_token
    if not expected or not auth_header:
        return False
    given = auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header
    return hmac.compare_digest(given.strip().encode(), expected.encode())


def require_token(request: Request) -> None:
    if not load_settings().api_token:
        raise HTTPException(503, "Set TG_API_TOKEN to enable the approval endpoints.")
    if not _token_ok(request.headers.get("authorization")):
        raise HTTPException(401, "Missing or invalid bearer token.")


def require_dashboard(request: Request) -> None:
    """Dashboard buttons: allowed from this machine (not through the tunnel) or with the token."""
    mode = load_settings().dashboard_approvals
    if mode == "open" or _token_ok(request.headers.get("authorization")):
        return
    if mode == "local":
        host = request.client.host if request.client else ""
        if host in LOOPBACK and not any(h in request.headers for h in FORWARDED):
            return
    raise HTTPException(403, "Dashboard approvals are only accepted from the local machine.")


class _BearerGuard:
    """ASGI wrapper for the MCP endpoint: rejects requests without the TG_API_TOKEN."""

    def __init__(self) -> None:
        self.app: Any = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if not load_settings().api_token:
                await JSONResponse({"error": "Set TG_API_TOKEN to enable /mcp"}, 503)(scope, receive, send)
                return
            if not _token_ok(headers.get("authorization")):
                await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp_guard = _BearerGuard()


def _build_mcp_endpoint() -> Any:
    # Stateless JSON mode: each tool call is a single POST, which suits a phone agent behind a tunnel.
    # DNS-rebinding checks are off because the Host is the tunnel's; the bearer token guards access.
    starlette_app = voice_mcp.mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return next(r.endpoint for r in starlette_app.routes if getattr(r, "path", None) == "/mcp")


async def _monitor_loop(interval: int) -> None:
    """Proactive mode: poll flight status, create proposals, optionally phone the traveler."""
    while True:
        try:
            result = await asyncio.to_thread(_evaluate_and_maybe_call, load_settings().auto_call)
            if result["created"]:
                log.info("monitor created %s", result["created"])
        except Exception:  # keep watching; one failed poll must not stop the monitor
            log.exception("monitor iteration failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    mcp_guard.app = _build_mcp_endpoint()  # fresh session manager per app start
    async with voice_mcp.mcp.session_manager.run():
        interval = load_settings().watch_interval
        task = asyncio.create_task(_monitor_loop(interval)) if interval > 0 else None
        try:
            yield
        finally:
            if task:
                task.cancel()


app = FastAPI(title="TravelGuardian API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Request schemas (matched to api.js payloads)
# ------------------------------------------------------------------
class DelayRequest(BaseModel):
    flight: str = "UA1542"
    delay: int = 110
    reason: str = "Late-arriving aircraft"


class RespondRequest(BaseModel):
    offline: bool = False


class ApproveRequest(BaseModel):
    id: str
    option: str | None = None


class DeclineRequest(BaseModel):
    id: str


class AskRequest(BaseModel):
    question: str


class DecisionRequest(BaseModel):
    traveler_said: str | None = None  # the spoken approval, for the audit record
    reason: str | None = None


class EvaluateRequest(BaseModel):
    call: bool = False  # also phone the traveler about any new proposal


class CallRequest(BaseModel):
    to_number: str | None = None  # defaults to TRAVELER_PHONE


class ResetRequest(BaseModel):
    empty: bool = False  # True: no bookings, fill from Gmail with /api/sync


# ------------------------------------------------------------------
# Backend helpers
# ------------------------------------------------------------------
def get_store() -> Store:
    store = Store()
    if not store.scenario_path.exists():
        store.reset()
    return store


def get_provider(store: Store) -> Any:
    return flight_provider(store)


def get_services() -> Services:
    return build_services(get_store())


def get_toolbox(store: Store, provider: Any | None = None) -> ToolBox:
    prov = provider or get_provider(store)
    return ToolBox(store, prov, approver=defer_all, now=prov.now)


def _call_json_tool(tb: ToolBox, name: str, args: dict[str, Any]) -> Any:
    raw, is_err = tb.call(name, args)
    if is_err:
        raise HTTPException(status_code=500, detail=raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _serialize_draft(d: Any) -> dict[str, Any]:
    if hasattr(d, "model_dump"):
        return d.model_dump(mode="json")
    return {
        "id": getattr(d, "id", ""),
        "to": getattr(d, "to", ""),
        "subject": getattr(d, "subject", ""),
        "kind": getattr(d, "kind", "email"),
        "body": getattr(d, "body", ""),
    }


def _run_agent(store: Store, prompt: str, max_turns: int = 10) -> dict[str, Any]:
    """Run the NVIDIA Nemotron tool-calling loop and return structured output."""
    client = _client()
    tb = get_toolbox(store)
    tools = _tools()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]

    executed_tools: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []
    final_content = ""

    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=1,
            top_p=0.95,
            max_tokens=4096,
            parallel_tool_calls=True,
            seed=42,
            extra_body={"chat_template_kwargs": {"enable_thinking": True}},
            stream=False,
            tools=tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            reasoning_parts.append(reasoning)

        if msg.content:
            final_content = msg.content

        if not msg.tool_calls:
            break

        serialized_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in msg.tool_calls
        ]
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": serialized_calls,
            }
        )

        for tc in serialized_calls:
            fn_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            result_text, is_err = tb.call(fn_name, args)
            executed_tools.append(
                {
                    "name": fn_name,
                    "args": args,
                    "is_error": is_err,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                }
            )

    # Return fresh state alongside the agent response so the UI can sync immediately
    itinerary = _call_json_tool(tb, "get_itinerary", {})
    drafts = [_serialize_draft(d) for d in store.load().drafts]
    return {
        "answer": final_content,
        "reply": final_content,
        "reasoning": "\n".join(reasoning_parts),
        "tool_calls": executed_tools,
        "itinerary": itinerary,
        "drafts": drafts,
    }


# ------------------------------------------------------------------
# API Routes (10 endpoints matching api.js)
# ------------------------------------------------------------------
@app.get("/api/status")
def api_status() -> dict[str, Any]:
    store = get_store()
    prov = get_provider(store)
    tb = get_toolbox(store, prov)
    data = _call_json_tool(tb, "check_flight_statuses", {})
    is_live = AirLabsProvider is not None and isinstance(prov, AirLabsProvider)
    return {
        "ok": True,
        "simulated": not is_live,
        "provider": "airlabs" if is_live else "simulated",
        **data,
    }


@app.get("/api/itinerary")
def api_itinerary() -> dict[str, Any]:
    store = get_store()
    tb = get_toolbox(store)
    return _call_json_tool(tb, "get_itinerary", {})


@app.post("/api/simulate-delay")
def api_simulate_delay(req: DelayRequest) -> dict[str, Any]:
    store = get_store()
    prov = get_provider(store)
    trip = store.load()

    match = next(
        (f for f in trip.flights if f.flight_number.upper() == req.flight.upper()),
        None,
    )
    flight_date = match.departs.date().isoformat() if match else "2026-12-03"

    prov.set_disruption(
        req.flight.upper(),
        flight_date,
        delay_minutes=req.delay,
        reason=req.reason,
    )

    tb = get_toolbox(store, prov)
    return {
        "ok": True,
        "flight": req.flight.upper(),
        "date": flight_date,
        "delay": req.delay,
        "reason": req.reason,
        "itinerary": _call_json_tool(tb, "get_itinerary", {}),
    }


@app.post("/api/clear-disruptions")
def api_clear_disruptions() -> dict[str, Any]:
    store = get_store()
    prov = get_provider(store)
    prov.clear_disruptions()
    tb = get_toolbox(store, prov)
    return {
        "ok": True,
        "message": "Cleared all disruptions.",
        "itinerary": _call_json_tool(tb, "get_itinerary", {}),
    }


@app.post("/api/respond")
def api_respond(req: RespondRequest) -> dict[str, Any]:
    store = get_store()
    if req.offline:
        tb = get_toolbox(store)
        statuses = _call_json_tool(tb, "check_flight_statuses", {})
        itinerary = _call_json_tool(tb, "get_itinerary", {})
        drafts = [_serialize_draft(d) for d in store.load().drafts]
        return {
            "mode": "offline",
            "statuses": statuses,
            "itinerary": itinerary,
            "drafts": drafts,
            "tool_calls": [
                {"name": "check_flight_statuses", "args": {}},
                {"name": "get_itinerary", "args": {}},
            ],
        }

    prompt = (
        "Check my flights. If anything is disrupted, handle it: check my connection, "
        "find options, show me the revised itinerary and ask me before changing anything."
    )
    try:
        result = _run_agent(store, prompt)
        return {"mode": "live", **result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent execution failed: {exc}") from exc


def _gate(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except approval.GateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _dashboard_target(svc: Services, requested_id: str) -> str:
    """The proposal a dashboard button refers to: the given id, else the open one (creating it if needed)."""
    trip = svc.store.load()
    if trip.proposal(requested_id):
        return requested_id
    pending = next((p for p in trip.proposals if p.status == "pending"), None)
    if pending is None:
        proposals.evaluate(svc)
        pending = next((p for p in svc.store.load().proposals if p.status == "pending"), None)
    if pending is None:
        raise HTTPException(404, "No pending proposal. Nothing needs rebooking right now.")
    return pending.id


def _with_itinerary(svc: Services, result: dict[str, Any]) -> dict[str, Any]:
    tb = get_toolbox(svc.store, svc.provider)
    return {
        "status": result["status"],
        "result": result,
        "itinerary": _call_json_tool(tb, "get_itinerary", {}),
        "drafts": [_serialize_draft(d) for d in svc.store.load().drafts],
    }


@app.post("/api/approve")
def api_approve(req: ApproveRequest, request: Request) -> dict[str, Any]:
    """Dashboard approval (local machine only by default). Same gate as the voice agent."""
    require_dashboard(request)
    svc = get_services()
    pid = _dashboard_target(svc, req.id)
    prop = svc.store.load().proposal(pid)
    if req.option and prop.alternative_id.removeprefix("alt-") != req.option.removeprefix("alt-"):
        raise HTTPException(409, f"Proposal {pid} is for {prop.alternative_id}; select that option to approve it.")
    res = _gate(approval.approve, svc, pid, channel="traveler (dashboard)")
    return _with_itinerary(svc, res)


@app.post("/api/decline")
def api_decline(req: DeclineRequest, request: Request) -> dict[str, Any]:
    require_dashboard(request)
    svc = get_services()
    pid = _dashboard_target(svc, req.id)
    res = _gate(approval.decline, svc, pid, channel="traveler (dashboard)")
    return _with_itinerary(svc, res)


@app.post("/api/ask")
def api_ask(req: AskRequest) -> dict[str, Any]:
    store = get_store()
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    try:
        return _run_agent(store, req.question.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Agent execution failed: {exc}") from exc


@app.post("/api/ingest")
@app.post("/api/sync")
def api_sync() -> dict[str, Any]:
    """Gmail (or sample inbox) -> Nemotron extraction -> trip store -> calendar events."""
    svc = get_services()
    try:
        result = sync_inbox(svc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc
    tb = get_toolbox(svc.store, svc.provider)
    return {"ok": True, **result, "notes": svc.notes, "itinerary": _call_json_tool(tb, "get_itinerary", {})}


@app.get("/api/drafts")
def api_drafts() -> dict[str, Any]:
    store = get_store()
    trip = store.load()
    drafts = [_serialize_draft(d) for d in trip.drafts]
    return {"count": len(drafts), "drafts": drafts}


# ------------------------------------------------------------------
# Proposals, calls and the voice approval loop
# ------------------------------------------------------------------
def _start_call(svc: Services, proposal_id: str, to_number: Optional[str] = None) -> dict[str, Any]:
    trip = svc.store.load()
    prop = trip.proposal(proposal_id)
    if prop is None:
        raise HTTPException(404, f"No proposal {proposal_id}.")
    if prop.status != "pending":
        raise HTTPException(409, f"Proposal {prop.id} is {prop.status}; calls are only placed for pending proposals.")
    try:
        record = ElevenLabsVoice(svc.settings).start_call(prop, to_number or svc.settings.traveler_phone, svc.now())
    except VoiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    trip = svc.store.load()
    prop = trip.proposal(proposal_id)
    prop.call = record
    svc.store.save(trip)
    return proposals.view(prop)


def _evaluate_and_maybe_call(call: bool) -> dict[str, Any]:
    svc = get_services()
    result = proposals.evaluate(svc)
    calls = []
    if call:
        for pid in result["created"]:
            try:
                calls.append(_start_call(svc, pid)["call"])
            except HTTPException as exc:
                calls.append({"proposal_id": pid, "error": exc.detail})
    return {**result, "calls": calls}


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    """Which integrations are live vs simulated (no secrets)."""
    s = load_settings()
    svc = get_services()
    return {
        "mail": svc.mail.name,
        "calendar": svc.calendar.name,
        "flight_status": "airlabs" if AirLabsProvider is not None and isinstance(svc.provider, AirLabsProvider) else "simulated",
        "voice": "elevenlabs" if s.elevenlabs_configured else "not configured",
        "traveler_phone_set": bool(s.traveler_phone),
        "approval_token_set": bool(s.api_token),
        "webhook_secret_set": bool(s.elevenlabs_webhook_secret),
        "llm": "nemotron" if s.llm_enabled else "off",
        "public_base_url": s.public_base_url or None,
        "notes": svc.notes,
    }


@app.post("/api/reset")
def api_reset(req: ResetRequest, request: Request) -> dict[str, Any]:
    """Start over: the sample trip, or an empty trip to fill from Gmail. Local machine only."""
    require_dashboard(request)
    get_store().reset(with_bookings=not req.empty)
    return {"ok": True, "empty": req.empty}


@app.post("/api/evaluate")
def api_evaluate(req: EvaluateRequest, request: Request) -> dict[str, Any]:
    """Run the risk engine now; creates a proposal (e.g. TG-402) for any connection that will be missed."""
    if req.call:
        require_dashboard(request)
    result = _evaluate_and_maybe_call(req.call)
    trip = get_store().load()
    return {**result, "proposals": [proposals.view(p) for p in trip.proposals]}


@app.get("/api/proposals")
def api_proposals() -> dict[str, Any]:
    trip = get_store().load()
    return {"proposals": [proposals.view(p) for p in reversed(trip.proposals)]}


@app.get("/api/proposals/{proposal_id}")
def api_proposal(proposal_id: str) -> dict[str, Any]:
    prop = get_store().load().proposal(proposal_id)
    if prop is None:
        raise HTTPException(404, f"No proposal {proposal_id}.")
    return proposals.view(prop)


@app.post("/api/proposals/{proposal_id}/approve")
def api_proposal_approve(proposal_id: str, req: DecisionRequest, request: Request) -> dict[str, Any]:
    """Authenticated approval endpoint (Bearer TG_API_TOKEN), for voice-agent tools and integrations."""
    require_token(request)
    if not (req.traveler_said or "").strip():
        raise HTTPException(422, "traveler_said is required: quote the traveler's explicit approval.")
    return _gate(approval.approve, get_services(), proposal_id, channel="voice agent (REST)",
                 spoken_confirmation=req.traveler_said.strip())


@app.post("/api/proposals/{proposal_id}/decline")
def api_proposal_decline(proposal_id: str, req: DecisionRequest, request: Request) -> dict[str, Any]:
    require_token(request)
    return _gate(approval.decline, get_services(), proposal_id, channel="voice agent (REST)",
                 reason=req.reason or "")


@app.post("/api/proposals/{proposal_id}/call")
def api_proposal_call(proposal_id: str, req: CallRequest, request: Request) -> dict[str, Any]:
    require_dashboard(request)
    return _start_call(get_services(), proposal_id, req.to_number)


@app.post("/api/proposals/{proposal_id}/transcript")
def api_proposal_transcript(proposal_id: str) -> dict[str, Any]:
    """Pull the latest call status and transcript from ElevenLabs (the webhook also does this)."""
    svc = get_services()
    trip = svc.store.load()
    prop = trip.proposal(proposal_id)
    if prop is None:
        raise HTTPException(404, f"No proposal {proposal_id}.")
    if not prop.call or not prop.call.conversation_id:
        raise HTTPException(409, f"No call has been placed for {prop.id}.")
    try:
        conv = ElevenLabsVoice(svc.settings).conversation(prop.call.conversation_id)
    except VoiceError as exc:
        raise HTTPException(502, str(exc)) from exc
    prop.call.status = conv.get("status", prop.call.status)
    prop.transcript = transcript_from(conv)
    svc.store.save(trip)
    return proposals.view(prop)


@app.post("/api/webhooks/elevenlabs")
async def api_elevenlabs_webhook(request: Request) -> dict[str, Any]:
    """Post-call webhook: stores the transcript on the proposal as its approval record."""
    secret = load_settings().elevenlabs_webhook_secret
    raw = await request.body()
    if not secret:
        raise HTTPException(503, "Set ELEVENLABS_WEBHOOK_SECRET to accept webhooks.")
    if not verify_signature(raw, request.headers.get("elevenlabs-signature"), secret):
        raise HTTPException(401, "Invalid webhook signature.")
    event = json.loads(raw)
    if event.get("type") != "post_call_transcription":
        return {"ok": True, "ignored": event.get("type")}
    data = event.get("data", {})
    conv_id = data.get("conversation_id")
    dyn = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}

    def store_transcript() -> Optional[str]:
        store = get_store()
        trip = store.load()
        prop = next((p for p in trip.proposals if p.call and p.call.conversation_id == conv_id), None)
        if prop is None and dyn.get("proposal_id"):
            prop = trip.proposal(str(dyn["proposal_id"]))
        if prop is None:
            return None
        prop.transcript = transcript_from(data)
        if prop.call is None:  # call placed outside this server (e.g. `tg call`, or from the ElevenLabs UI)
            started = (data.get("metadata") or {}).get("start_time_unix_secs") or event.get("event_timestamp")
            prop.call = CallRecord(provider="elevenlabs", status="done",
                                   started_at=datetime.fromtimestamp(started or time.time(), timezone.utc),
                                   conversation_id=conv_id)
        prop.call.status = data.get("status", "done")
        store.save(trip)
        return prop.id

    pid = await asyncio.to_thread(store_transcript)
    return {"ok": True, "proposal_id": pid}


# The voice agent's MCP server (Streamable HTTP), guarded by the bearer token.
app.router.routes.append(Route("/mcp", endpoint=mcp_guard))


# Mount static frontend LAST so it never shadows /api/* routes
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


def run():
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    run()
