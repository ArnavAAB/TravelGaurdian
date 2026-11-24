"""ElevenLabs Conversational AI: outbound calls over Twilio, transcripts, post-call webhooks.

API used (https://elevenlabs.io/docs):
  POST /v1/convai/twilio/outbound-call      {agent_id, agent_phone_number_id, to_number,
                                             conversation_initiation_client_data.dynamic_variables}
  GET  /v1/convai/conversations/{id}        -> status, transcript[{role, message, time_in_call_secs}]
  Post-call webhook "post_call_transcription", signed with header ElevenLabs-Signature: t=<ts>,v0=<hex>
  where hex = HMAC-SHA256(secret, f"{ts}.{raw_body}") (same check as the official SDK, 30 min tolerance).

The agent's prompt reads the dynamic variables below as {{variable}}; it acts only through the
MCP tools get_proposal / approve_proposal / decline_proposal (see voice_mcp.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional

import httpx

from .models import CallRecord, ProposedChange, TranscriptTurn
from .settings import Settings


class VoiceError(RuntimeError):
    pass


def dynamic_variables(prop: ProposedChange) -> dict[str, str]:
    c = prop.context
    alt = c.get("alternative", {})
    values = {
        "proposal_id": prop.id,
        "traveler_name": c.get("traveler", ""),
        "delayed_flight": c.get("delayed_flight", ""),
        "delay_minutes": c.get("delay_minutes", ""),
        "new_arrival": c.get("new_arrival", ""),
        "connection_airport": c.get("connection_airport", ""),
        "missed_flight": c.get("missed_flight", ""),
        "missed_departure": c.get("missed_departure", ""),
        "layover_minutes": c.get("layover_minutes", ""),
        "mct_minutes": c.get("mct_minutes", ""),
        "alternative_flight": f"{alt.get('airline', '')} {alt.get('flight_number', '')}".strip(),
        "alternative_route": f"{alt.get('origin', '')} to {alt.get('destination', '')}",
        "alternative_departs": alt.get("departs", ""),
        "alternative_arrives": alt.get("arrives", ""),
        "rationale": prop.rationale or "",
        "fare_note": c.get("fare_note", ""),
    }
    return {k: str(v) for k, v in values.items()}


def transcript_from(conversation: dict[str, Any]) -> list[TranscriptTurn]:
    turns = []
    for item in conversation.get("transcript") or []:
        msg = item.get("message")
        if not msg or item.get("role") not in ("agent", "user"):
            continue  # tool-call-only entries carry no spoken text
        turns.append(TranscriptTurn(role=item["role"], message=msg, time_in_call_secs=item.get("time_in_call_secs")))
    return turns


def verify_signature(raw_body: bytes, header: Optional[str], secret: str, now: Optional[float] = None,
                     tolerance_secs: int = 30 * 60) -> bool:
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig or not ts.isdigit():
        return False
    if int(ts) < (now if now is not None else time.time()) - tolerance_secs:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


class ElevenLabsVoice:
    def __init__(self, settings: Settings, http: Optional[httpx.Client] = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=20)

    @property
    def configured(self) -> bool:
        return self.s.elevenlabs_configured

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.s.elevenlabs_api_key, "Content-Type": "application/json"}

    def start_call(self, prop: ProposedChange, to_number: str, now) -> CallRecord:
        if not self.configured:
            raise VoiceError("ElevenLabs is not configured: set ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID and "
                             "ELEVENLABS_PHONE_NUMBER_ID.")
        if not to_number:
            raise VoiceError("No phone number to call: set TRAVELER_PHONE (E.164, e.g. +14155550123).")
        body = {
            "agent_id": self.s.elevenlabs_agent_id,
            "agent_phone_number_id": self.s.elevenlabs_phone_number_id,
            "to_number": to_number,
            "conversation_initiation_client_data": {"dynamic_variables": dynamic_variables(prop)},
        }
        r = self.http.post(f"{self.s.elevenlabs_base_url}/v1/convai/twilio/outbound-call", json=body,
                           headers=self._headers())
        if r.status_code >= 400:
            raise VoiceError(f"ElevenLabs outbound call failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        if not data.get("success"):
            raise VoiceError(f"ElevenLabs did not start the call: {data.get('message')}")
        return CallRecord(provider="elevenlabs", status="initiated", started_at=now, to_number=to_number,
                          conversation_id=data.get("conversation_id"), call_sid=data.get("callSid"),
                          message=data.get("message"))

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        r = self.http.get(f"{self.s.elevenlabs_base_url}/v1/convai/conversations/{conversation_id}",
                          headers=self._headers())
        if r.status_code >= 400:
            raise VoiceError(f"ElevenLabs conversation lookup failed ({r.status_code}): {r.text[:300]}")
        return r.json()
