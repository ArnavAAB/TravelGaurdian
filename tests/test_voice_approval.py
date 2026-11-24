"""The voice approval loop: risk engine -> proposal -> call -> MCP/REST approval gate -> email + calendar."""

import base64
import hashlib
import hmac
import json
import time
from email import message_from_bytes, policy

import httpx
import pytest
from fastapi.testclient import TestClient

from travelguardian import voice
from travelguardian.calendar_sync import GoogleCalendarProvider
from travelguardian.ingest import Extraction, ExtractedFlight
from travelguardian.mail import GmailProvider
from travelguardian.models import EmailRef
from travelguardian.store import SAMPLES, Store

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
UNITED_MSG_ID = "<united-K7Q2LM@united.example>"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("AIRLABS_API_KEY", "USE_LIVE_AIRLABS", "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID",
                "ELEVENLABS_PHONE_NUMBER_ID", "ELEVENLABS_WEBHOOK_SECRET", "TRAVELER_PHONE",
                "TG_DASHBOARD_APPROVALS", "TG_WATCH_INTERVAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TG_HOME", str(tmp_path))
    monkeypatch.setenv("TG_LLM", "off")
    monkeypatch.setenv("TG_GOOGLE", "off")
    monkeypatch.setenv("TG_API_TOKEN", TOKEN)
    monkeypatch.setenv("REBOOK_EMAIL_TO", "desk@test.example")
    store = Store(tmp_path)
    store.reset()
    import server

    with TestClient(server.app) as c:
        yield c, store


def disrupt(c):
    assert c.post("/api/simulate-delay", json={"flight": "UA1542", "delay": 110}).status_code == 200
    return c.post("/api/evaluate", json={}).json()


def approve(c, said="Yes, go ahead and rebook me.", pid="TG-402", headers=AUTH):
    return c.post(f"/api/proposals/{pid}/approve", json={"traveler_said": said}, headers=headers)


def mcp_call(c, tool, args, headers=MCP_HEADERS):
    r = c.post("/mcp", headers=headers,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    return r, (json.loads(r.json()["result"]["content"][0]["text"]) if r.status_code == 200 else None)


def outbox(store):
    return sorted((store.root / "outbox").glob("*.eml")) if (store.root / "outbox").exists() else []


# ---------------------------------------------------------------- risk engine -> proposal


def test_delay_creates_tg402_once(env):
    c, store = env
    res = disrupt(c)
    assert res["created"] == ["TG-402"]
    prop = store.load().proposal("TG-402")
    assert prop.status == "pending"
    assert prop.segment_ids == ["seg-UA958-20261203"] and prop.alternative_id == "alt-UA938"
    assert prop.context["layover_minutes"] == 45 and prop.context["mct_minutes"] == 90
    assert prop.email_ref.rfc_message_id == UNITED_MSG_ID
    assert "UA938" in prop.email_draft["body"] and "K7Q2LM" in prop.email_draft["body"]
    assert prop.email_draft["subject"].startswith("Re: eTicket Itinerary")
    again = c.post("/api/evaluate", json={}).json()
    assert again["created"] == [] and again["existing"] == ["TG-402"]


def test_on_time_trip_creates_no_proposal(env):
    c, store = env
    assert c.post("/api/evaluate", json={}).json()["created"] == []
    assert store.load().proposals == []


# ---------------------------------------------------------------- authenticated approval endpoint


def test_approval_endpoint_needs_token_and_words(env):
    c, _ = env
    disrupt(c)
    assert approve(c, headers={}).status_code == 401
    assert approve(c, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert approve(c, said="  ").status_code == 422


def test_approval_endpoint_disabled_without_token(env, monkeypatch):
    c, _ = env
    disrupt(c)
    monkeypatch.delenv("TG_API_TOKEN")
    assert approve(c).status_code == 503
    assert c.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 503


def test_approval_replies_in_thread_updates_calendar_and_is_idempotent(env):
    c, store = env
    disrupt(c)
    r = approve(c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied" and body["complete"] and not body["idempotent_replay"]
    assert body["spoken_confirmation"] == "Yes, go ahead and rebook me."

    sent = outbox(store)
    assert len(sent) == 1
    mail = message_from_bytes(sent[0].read_bytes(), policy=policy.default)
    assert mail["To"] == "desk@test.example"
    assert mail["In-Reply-To"] == UNITED_MSG_ID and mail["References"] == UNITED_MSG_ID
    assert mail["Subject"].startswith("Re: eTicket Itinerary")
    assert body["actions"]["email"]["thread_id"] == store.load().proposal("TG-402").email_ref.thread_id

    events = json.loads((store.root / "flight_events.json").read_text())
    assert any(e["summary"].startswith("✈ UA938") for e in events.values())
    trip = store.load()
    assert trip.flight("seg-UA938-20261203") and not trip.flight("seg-UA958-20261203")

    again = approve(c)
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True
    assert len(outbox(store)) == 1  # no second email


def test_declined_proposal_cannot_be_approved(env):
    c, store = env
    disrupt(c)
    r = c.post("/api/proposals/TG-402/decline", json={"reason": "I'll wait"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert approve(c).status_code == 409
    assert c.post("/api/proposals/TG-402/decline", json={}, headers=AUTH).json()["idempotent_replay"] is True
    assert outbox(store) == [] and store.load().flight("seg-UA958-20261203") is not None


def test_approved_proposal_cannot_be_declined(env):
    c, _ = env
    disrupt(c)
    approve(c)
    assert c.post("/api/proposals/TG-402/decline", json={}, headers=AUTH).status_code == 409


def test_reset_from_dashboard_is_local_only(env):
    c, store = env
    assert c.post("/api/reset", json={"empty": True}).status_code == 403  # not loopback
    assert c.post("/api/reset", json={"empty": True}, headers=AUTH).status_code == 200
    trip = store.load()
    assert trip.flights == [] and trip.traveler == ""


def test_unknown_proposal_is_404(env):
    c, _ = env
    assert approve(c, pid="TG-999").status_code == 404


def test_dashboard_buttons_refused_through_tunnel(env):
    c, _ = env
    disrupt(c)
    # TestClient is not a loopback address, like a request arriving through ngrok.
    assert c.post("/api/approve", json={"id": "TG-402"}).status_code == 403
    assert c.post("/api/approve", json={"id": "TG-402"}, headers=AUTH).status_code == 200


# ---------------------------------------------------------------- MCP tools for the voice agent


def test_mcp_requires_token(env):
    c, _ = env
    r, _ = mcp_call(c, "get_proposal", {"proposal_id": "TG-402"}, headers={k: v for k, v in MCP_HEADERS.items()
                                                                           if k != "Authorization"})
    assert r.status_code == 401


def test_mcp_get_approve_flow(env):
    c, store = env
    disrupt(c)
    _, got = mcp_call(c, "get_proposal", {"proposal_id": "tg-402"})
    assert got["ok"] and got["status"] == "pending" and "UA938" in got["summary"]
    _, refused = mcp_call(c, "approve_proposal", {"proposal_id": "TG-402", "traveler_said": " "})
    assert refused["ok"] is False and store.load().proposal("TG-402").status == "pending"
    _, ok = mcp_call(c, "approve_proposal", {"proposal_id": "TG-402", "traveler_said": "Yes please"})
    assert ok == {"ok": True, "status": "applied", "complete": True, "errors": [], "already_approved": False}
    _, replay = mcp_call(c, "approve_proposal", {"proposal_id": "TG-402", "traveler_said": "Yes please"})
    assert replay["already_approved"] is True and len(outbox(store)) == 1
    assert store.load().proposal("TG-402").decided_by == "voice agent (MCP)"


def test_mcp_decline_then_approve_is_rejected(env):
    c, _ = env
    disrupt(c)
    _, declined = mcp_call(c, "decline_proposal", {"proposal_id": "TG-402", "reason": "No"})
    assert declined == {"ok": True, "status": "declined"}
    _, refused = mcp_call(c, "approve_proposal", {"proposal_id": "TG-402", "traveler_said": "yes"})
    assert refused["ok"] is False and "declined" in refused["error"]


# ---------------------------------------------------------------- ElevenLabs outbound call + transcript


def _fake_voice(monkeypatch, handler):
    import server

    real = voice.ElevenLabsVoice
    monkeypatch.setattr(server, "ElevenLabsVoice",
                        lambda settings: real(settings, http=httpx.Client(transport=httpx.MockTransport(handler))))


def test_call_and_transcript_via_elevenlabs(env, monkeypatch):
    c, store = env
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-key")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_1")
    monkeypatch.setenv("ELEVENLABS_PHONE_NUMBER_ID", "phnum_1")
    monkeypatch.setenv("TRAVELER_PHONE", "+14155550123")
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == "/v1/convai/twilio/outbound-call":
            return httpx.Response(200, json={"success": True, "message": "ok", "conversation_id": "conv_1",
                                             "callSid": "CA1"})
        assert req.url.path == "/v1/convai/conversations/conv_1"
        return httpx.Response(200, json={"status": "done", "transcript": [
            {"role": "agent", "message": "UA1542 is 110 minutes late. Shall I request UA938?", "time_in_call_secs": 3},
            {"role": "agent", "message": None, "time_in_call_secs": 9, "tool_calls": [{}]},
            {"role": "user", "message": "Yes, go ahead.", "time_in_call_secs": 8}]})

    _fake_voice(monkeypatch, handler)
    disrupt(c)
    r = c.post("/api/proposals/TG-402/call", json={}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["call"]["conversation_id"] == "conv_1"
    body = json.loads(seen[0].content)
    assert seen[0].headers["xi-api-key"] == "xi-key"
    assert body["agent_id"] == "agent_1" and body["agent_phone_number_id"] == "phnum_1"
    assert body["to_number"] == "+14155550123"
    dyn = body["conversation_initiation_client_data"]["dynamic_variables"]
    assert dyn["proposal_id"] == "TG-402" and "UA938" in dyn["alternative_flight"] and dyn["delay_minutes"] == "110"

    t = c.post("/api/proposals/TG-402/transcript").json()
    assert t["call"]["status"] == "done"
    assert [x["role"] for x in t["transcript"]] == ["agent", "user"]


def test_call_without_elevenlabs_config_is_503(env):
    c, _ = env
    disrupt(c)
    r = c.post("/api/proposals/TG-402/call", json={}, headers=AUTH)
    assert r.status_code == 503 and "ELEVENLABS" in r.json()["detail"]


def _signed(payload: dict, secret: str, ts: int | None = None):
    raw = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, f"t={ts},v0={sig}"


def test_post_call_webhook_stores_transcript(env, monkeypatch):
    c, store = env
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "whsec")
    disrupt(c)
    payload = {"type": "post_call_transcription", "event_timestamp": int(time.time()), "data": {
        "conversation_id": "conv_9", "status": "done",
        "conversation_initiation_client_data": {"dynamic_variables": {"proposal_id": "TG-402"}},
        "transcript": [{"role": "agent", "message": "Shall I rebook you on UA938?", "time_in_call_secs": 4},
                       {"role": "user", "message": "Yes.", "time_in_call_secs": 6}]}}
    raw, sig = _signed(payload, "whsec")
    bad = c.post("/api/webhooks/elevenlabs", content=raw, headers={"ElevenLabs-Signature": sig.replace("v0=", "v0=0")})
    assert bad.status_code == 401
    ok = c.post("/api/webhooks/elevenlabs", content=raw, headers={"ElevenLabs-Signature": sig})
    assert ok.status_code == 200 and ok.json()["proposal_id"] == "TG-402"
    prop = store.load().proposal("TG-402")
    assert [t.message for t in prop.transcript] == ["Shall I rebook you on UA938?", "Yes."]
    assert prop.call.conversation_id == "conv_9" and prop.call.status == "done"


def test_background_monitor_creates_proposal(tmp_path, monkeypatch):
    for var in ("AIRLABS_API_KEY", "USE_LIVE_AIRLABS", "ELEVENLABS_API_KEY", "TG_AUTO_CALL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TG_HOME", str(tmp_path))
    monkeypatch.setenv("TG_LLM", "off")
    monkeypatch.setenv("TG_GOOGLE", "off")
    monkeypatch.setenv("TG_WATCH_INTERVAL", "1")
    store = Store(tmp_path)
    store.reset()
    from travelguardian.providers import SimulatedProvider

    SimulatedProvider(store.scenario_path).set_disruption("UA1542", "2026-12-03", delay_minutes=110)
    import server

    with TestClient(server.app):
        deadline = time.time() + 5
        while time.time() < deadline and not store.load().proposals:
            time.sleep(0.1)
    assert [p.id for p in store.load().proposals] == ["TG-402"]


def test_verify_signature_rejects_stale_and_tampered():
    raw, sig = _signed({"a": 1}, "s", ts=1_000_000)
    assert voice.verify_signature(raw, sig, "s", now=1_000_100)
    assert not voice.verify_signature(raw, sig, "s", now=1_000_000 + 31 * 60)  # older than 30 min
    assert not voice.verify_signature(raw + b" ", sig, "s", now=1_000_100)
    assert not voice.verify_signature(raw, sig, "other", now=1_000_100)
    assert not voice.verify_signature(raw, None, "s")


# ---------------------------------------------------------------- Gmail / Calendar providers


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    def __init__(self, raws: dict[str, bytes]):
        self.raws, self.sent = raws, []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        return _Exec({"messages": [{"id": i} for i in self.raws]})

    def get(self, userId, id, format):
        return _Exec({"raw": base64.urlsafe_b64encode(self.raws[id]).decode(), "threadId": f"thr-{id}"})

    def send(self, userId, body):
        self.sent.append(body)
        return _Exec({"id": "sent-1", "threadId": body.get("threadId")})


def test_gmail_reads_confirmations_and_replies_in_thread():
    svc = FakeGmail({"m1": (SAMPLES / "inbox" / "01-united-itinerary.eml").read_bytes()})
    gmail = GmailProvider(svc, "subject:itinerary")
    [msg] = gmail.fetch_confirmations()
    assert msg.id == "gmail:m1" and msg.thread_id == "thr-m1" and msg.rfc_message_id == UNITED_MSG_ID
    assert "K7Q2LM" in msg.body
    ref = EmailRef(provider="gmail", message_id=msg.id, thread_id=msg.thread_id,
                   rfc_message_id=msg.rfc_message_id, subject=msg.subject)
    out = gmail.reply(ref, "desk@test.example", "Please rebook.")
    assert out["thread_id"] == "thr-m1" and svc.sent[0]["threadId"] == "thr-m1"
    sent = message_from_bytes(base64.urlsafe_b64decode(svc.sent[0]["raw"]), policy=policy.default)
    assert sent["In-Reply-To"] == UNITED_MSG_ID and sent["Subject"] == f"Re: {msg.subject}"


class FakeCalendar:
    def __init__(self, items=None):
        self.calls, self.items = [], items or []

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.calls.append(("insert", body))
        return _Exec({"id": "evt-new"})

    def patch(self, calendarId, eventId, body):
        self.calls.append(("patch", eventId, body))
        return _Exec({"id": eventId})

    def delete(self, calendarId, eventId):
        self.calls.append(("delete", eventId))
        return _Exec({})

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Exec({"items": [] if "privateExtendedProperty" in kw else self.items})


def test_google_calendar_replaces_flight_events_and_reads_meetings(tmp_path):
    store = Store(tmp_path)
    trip = store.reset()
    ua1542, ua958 = trip.flights[0], trip.flights[1]
    fake = FakeCalendar(items=[
        {"id": "tg", "summary": "✈ UA958", "start": {"dateTime": "2026-12-03T15:50:00-06:00"},
         "end": {"dateTime": "2026-12-04T06:05:00Z"}, "extendedProperties": {"private": {"tg_segment": "x"}}},
        {"id": "allday", "summary": "Holiday", "start": {"date": "2026-12-05"}, "end": {"date": "2026-12-06"}},
        {"id": "kick", "summary": "Client kickoff", "location": "30 Fenchurch St, London",
         "start": {"dateTime": "2026-12-04T10:00:00Z"}, "end": {"dateTime": "2026-12-04T12:00:00Z"},
         "attendees": [{"email": "a@example.com"}]}])
    cal = GoogleCalendarProvider(fake)
    assert cal.upsert_flight(ua958) == "evt-new"
    body = fake.calls[-1][1]
    assert body["extendedProperties"]["private"]["tg_segment"] == ua958.id
    assert body["start"]["timeZone"] == "America/Chicago" and body["end"]["timeZone"] == "Europe/London"
    res = cal.replace_flights(["evt-a", "evt-b"], ua1542, "note")
    assert res == {"updated": "evt-a", "deleted": ["evt-b"]}
    assert ("delete", "evt-b") in fake.calls
    [m] = cal.meetings(ua1542.departs, ua958.arrives)
    assert m.title == "Client kickoff" and m.city == "London" and m.attendees == ["a@example.com"]


class _NotFound(Exception):
    def __init__(self):
        super().__init__("Not Found")
        self.resp = type("Resp", (), {"status": 404})()


class GoneCalendar(FakeCalendar):
    """Events in `gone` were deleted in Google Calendar (patch/delete -> 404)."""

    def __init__(self, gone):
        super().__init__()
        self.gone = set(gone)

    def patch(self, calendarId, eventId, body):
        if eventId in self.gone:
            self.calls.append(("patch-404", eventId))
            raise _NotFound()
        return super().patch(calendarId, eventId, body)

    def delete(self, calendarId, eventId):
        if eventId in self.gone:
            raise _NotFound()
        return super().delete(calendarId, eventId)


def test_google_calendar_survives_stale_and_local_ids(tmp_path):
    trip = Store(tmp_path).reset()
    flight = trip.flights[0]
    fake = GoneCalendar(gone={"deleted-by-user"})
    cal = GoogleCalendarProvider(fake)
    # An id from the local demo calendar is never sent to Google; a new event is created.
    assert cal.upsert_flight(flight, "local-evt-1") == "evt-new"
    assert not any(c[0] == "patch" and c[1] == "local-evt-1" for c in fake.calls)
    # An event the user deleted in Google: recreated instead of crashing.
    assert cal.upsert_flight(flight, "deleted-by-user") == "evt-new"
    # Replacing: skip the gone event, reuse the next one that exists, ignore gone deletes.
    res = cal.replace_flights(["deleted-by-user", "evt-b", "deleted-by-user"], flight, "note")
    assert res == {"updated": "evt-b", "deleted": []}


def test_calendar_ids_are_scoped_to_their_provider(tmp_path):
    trip = Store(tmp_path).reset()
    trip.calendar_ids_for("local")["seg-x"] = "local-evt-1"
    assert trip.calendar_ids_for("local") == {"seg-x": "local-evt-1"}
    assert trip.calendar_ids_for("google") == {}  # switching calendars drops ids from the other one
    assert trip.calendar_provider == "google"


def test_sync_records_email_threads_and_calendar_events(env, monkeypatch):
    c, store = env
    store.reset(with_bookings=False)
    from travelguardian import services, sync

    def fake_extract(_client, msg):
        if "K7Q2LM" not in msg.body:
            return Extraction(is_travel_booking=False, flights=[], hotels=[])
        legs = [("UA1542", "SFO", "ORD", "2026-12-03T07:00", "2026-12-03T13:15", "America/Los_Angeles", "America/Chicago"),
                ("UA958", "ORD", "LHR", "2026-12-03T15:50", "2026-12-04T06:05", "America/Chicago", "Europe/London")]
        return Extraction(is_travel_booking=True, hotels=[], flights=[
            ExtractedFlight(confirmation="K7Q2LM", airline="United Airlines", flight_number=n, origin=o, destination=d,
                            departs_local=dep, arrives_local=arr, origin_timezone=tzo, destination_timezone=tzd,
                            passenger="MAYA RAO") for n, o, d, dep, arr, tzo, tzd in legs])

    monkeypatch.setattr(sync, "extract", fake_extract)
    monkeypatch.setattr(services.Services, "llm", lambda self: object())
    res = c.post("/api/sync").json()
    assert res["mail_provider"] == "local" and res["flight_events"] == 2
    trip = store.load()
    ref = trip.emails["inbox/01-united-itinerary.eml"]
    assert ref.rfc_message_id == UNITED_MSG_ID
    assert set(trip.calendar_events) == {"seg-UA1542-20261203", "seg-UA958-20261203"}
    again = c.post("/api/sync").json()
    assert again["flight_events"] == 2
    assert len(json.loads((store.root / "flight_events.json").read_text())) == 2  # updated, not duplicated


def test_proposal_ignores_alternatives_that_cannot_be_caught(tmp_path, monkeypatch):
    from datetime import timedelta

    from travelguardian import proposals
    from travelguardian.calendar_sync import LocalCalendarProvider
    from travelguardian.mail import LocalMailProvider
    from travelguardian.providers import SimulatedProvider
    from travelguardian.services import Services
    from travelguardian.settings import load_settings

    monkeypatch.setenv("TG_LLM", "off")

    class StaleFirst(SimulatedProvider):
        """Puts an earlier-than-possible flight first, like a live feed returning today's schedule."""

        def search(self, origin, destination, earliest_departure, latest_arrival=None):
            out = super().search(origin, destination, earliest_departure, latest_arrival)
            if out:
                stale = out[0].model_copy(update={"id": "alt-STALE", "flight_number": "XX1",
                                                  "departs": earliest_departure - timedelta(days=9),
                                                  "arrives": earliest_departure - timedelta(days=8)})
                out.insert(0, stale)
            return out

    store = Store(tmp_path)
    store.reset()
    provider = StaleFirst(store.scenario_path)
    provider.set_disruption("UA1542", "2026-12-03", delay_minutes=110)
    svc = Services(settings=load_settings(), store=store, provider=provider,
                   mail=LocalMailProvider(SAMPLES / "inbox", tmp_path / "outbox"),
                   calendar=LocalCalendarProvider(tmp_path / "ev.json", store.calendar))
    assert proposals.evaluate(svc)["created"] == ["TG-402"]
    assert store.load().proposal("TG-402").alternative_id == "alt-UA938"
