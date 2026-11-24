import json
from types import SimpleNamespace as NS

import pytest

from travelguardian import agent
from travelguardian.providers import SimulatedProvider
from travelguardian.store import Store
from travelguardian.tools import ToolBox, defer_all, deny_all


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.reset()
    provider = SimulatedProvider(store.scenario_path)
    return store, provider


def tb(env, approver=deny_all):
    store, provider = env
    return ToolBox(store, provider, approver=approver, now=provider.now)


def call(t, name, **args):
    text, err = t.call(name, args)
    assert not err, text
    return json.loads(text)


def kinds(conflicts):
    return sorted(c["kind"] for c in conflicts)


def test_baseline_conflicts(env):
    d = call(tb(env), "get_itinerary")
    assert kinds(d["conflicts"]) == ["early_arrival", "hotel_gap"]
    assert "Tue 08 Dec" in next(c for c in d["conflicts"] if c["kind"] == "hotel_gap")["message"]


def test_delay_detected_once(env):
    store, provider = env
    t = tb(env)
    assert call(t, "check_flight_statuses")["changed_since_last_check"] == []
    provider.set_disruption("UA1542", "2026-12-03", 110)
    changes = call(t, "check_flight_statuses")["changed_since_last_check"]
    assert [c["flight_number"] for c in changes] == ["UA1542"]
    assert call(t, "check_flight_statuses")["changed_since_last_check"] == []


@pytest.mark.parametrize("delay,risk", [(0, "low"), (50, "medium"), (110, "high"), (160, "missed")])
def test_connection_risk(env, delay, risk):
    _, provider = env
    if delay:
        provider.set_disruption("UA1542", "2026-12-03", delay)
    assert call(tb(env), "assess_connection", inbound_segment_id="seg-UA1542-20261203")["risk"] == risk


def test_alternatives_respect_mct_and_rank_protected_first(env):
    _, provider = env
    provider.set_disruption("UA1542", "2026-12-03", 110)
    t = tb(env)
    hint = call(t, "assess_connection", inbound_segment_id="seg-UA1542-20261203")["rebook_search_hint"]
    assert hint["earliest_departure"] == "2026-12-03T16:35:00-06:00"
    alts = call(t, "search_alternatives", **hint)["alternatives"]
    assert [a["id"] for a in alts] == ["alt-UA938", "alt-UA926", "alt-BA296"]


def test_preview_flags_missed_meeting(env):
    t = tb(env)
    pv = call(t, "preview_change", replace_segment_ids=["seg-UA958-20261203"], alternative_id="alt-UA926")
    assert any(c["kind"] == "calendar" and c["severity"] == "critical" for c in pv["new_conflicts"])
    pv = call(t, "preview_change", replace_segment_ids=["seg-UA958-20261203"], alternative_id="alt-UA938")
    assert not [c for c in pv["new_conflicts"] if c["kind"] == "calendar"]


def test_booking_change_requires_approval(env):
    store, _ = env
    r = call(tb(env, deny_all), "request_booking_change", replace_segment_ids=["seg-UA958-20261203"], alternative_id="alt-UA938", reason="r")
    assert r["status"] == "declined"
    assert store.load().flight("seg-UA958-20261203") is not None

    r = call(tb(env, defer_all), "request_booking_change", replace_segment_ids=["seg-UA958-20261203"], alternative_id="alt-UA938", reason="r")
    assert r["status"] == "pending"
    assert store.load().flight("seg-UA958-20261203") is not None

    seen = []
    r = call(tb(env, lambda s: seen.append(s) or True), "request_booking_change", replace_segment_ids=["seg-UA958-20261203"], alternative_id="alt-UA938", reason="r")
    assert r["status"] == "applied" and seen and seen[0]["cost"].startswith("no charge")
    trip = store.load()
    assert trip.flight("seg-UA958-20261203") is None
    assert trip.flight("seg-UA938-20261203").confirmation == "K7Q2LM"


def test_nonstop_replaces_both_legs(env):
    store, _ = env
    call(tb(env, lambda s: True), "request_booking_change",
         replace_segment_ids=["seg-UA1542-20261203", "seg-UA958-20261203"], alternative_id="alt-UA901", reason="r")
    assert [f.flight_number for f in store.load().sorted_flights()] == ["UA901", "BA287"]


def test_sold_out_fails_cleanly(env):
    store, _ = env
    r = tb(env, lambda s: True).call("request_booking_change", {"replace_segment_ids": ["seg-UA1542-20261203"], "alternative_id": "alt-UA1188", "reason": "r"})
    assert json.loads(r[0])["status"] == "failed"
    assert store.load().flight("seg-UA1542-20261203") is not None


def test_bad_tool_input_is_an_error(env):
    t = tb(env)
    assert t.call("search_alternatives", {"origin": "ORD"})[1] is True
    assert t.call("search_alternatives", {"origin": "ORD", "destination": "LHR", "earliest_departure": "tomorrow"})[1] is True
    assert t.call("nope", {})[1] is True


def test_offline_playbook_end_to_end(env, capsys):
    from travelguardian import offline

    store, provider = env
    provider.set_disruption("UA1542", "2026-12-03", 110)
    offline.respond(tb(env, lambda s: True))
    trip = store.load()
    assert "UA938" in [f.flight_number for f in trip.flights]
    assert [d.kind for d in trip.drafts] == ["hotel_notice"]
    assert "Tue 08 Dec" in trip.drafts[0].body


# ---- Agent event loop against a fake client


class FakeClient:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []
        self.base_url = "https://integrate.api.nvidia.com/v1"
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.streams:
            return iter(self.streams.pop(0))
        return iter([])


def test_agent_loop_runs_custom_tools_locally(env, tmp_path, monkeypatch):
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({"model": "nvidia/nemotron-3.5-super-vl-120b-a12b"}))
    monkeypatch.setattr(agent, "CONFIG", cfg)

    stream1 = [
        NS(
            choices=[
                NS(
                    delta=NS(
                        reasoning_content="Assessing the flight...",
                        content=None,
                        tool_calls=[
                            NS(
                                index=0,
                                id="call_1",
                                function=NS(
                                    name="assess_connection",
                                    arguments='{"inbound_segment_id": "seg-UA1542-20261203"}',
                                ),
                            )
                        ],
                    )
                )
            ]
        )
    ]
    stream2 = [
        NS(
            choices=[
                NS(
                    delta=NS(
                        reasoning_content=None,
                        content="All good.",
                        tool_calls=None,
                    )
                )
            ]
        )
    ]

    client = FakeClient([stream1, stream2])
    out = []
    reasoning_out = []
    agent.run(
        client,
        tb(env),
        "check",
        on_text=out.append,
        on_reasoning=reasoning_out.append,
    )

    assert len(client.calls) == 2
    messages_call2 = client.calls[1]["messages"]
    tool_resp = next(m for m in messages_call2 if m.get("role") == "tool")
    assert tool_resp["tool_call_id"] == "call_1"
    assert json.loads(tool_resp["content"])["risk"] == "low"
    assert "Assessing the flight..." in reasoning_out
    assert "All good." in out


def test_ingest_extract_mocked():
    from travelguardian import ingest
    from travelguardian.ingest import Extraction, ExtractedFlight, Message

    fake_parsed = Extraction(
        is_travel_booking=True,
        flights=[
            ExtractedFlight(
                confirmation="CONF123",
                airline="United Airlines",
                flight_number="UA100",
                origin="SFO",
                destination="JFK",
                departs_local="2026-12-03 08:00",
                arrives_local="2026-12-03 16:00",
                origin_timezone="America/Los_Angeles",
                destination_timezone="America/New_York",
                passenger="Maya Rao",
            )
        ],
        hotels=[],
    )

    class FakeIngestClient:
        def __init__(self):
            self.beta = NS(
                chat=NS(
                    completions=NS(
                        parse=lambda **kwargs: NS(
                            choices=[NS(message=NS(parsed=fake_parsed), finish_reason="stop")]
                        )
                    )
                )
            )

    client = FakeIngestClient()
    msg = Message(id="msg_1", sender="airline@example.com", subject="Confirmation", date="2026-11-20", body="Booking confirmed")
    ext = ingest.extract(client, msg)
    assert ext.is_travel_booking is True
    assert len(ext.flights) == 1

    flights, hotels = ingest.to_bookings(ext, "msg_1")
    assert len(flights) == 1
    assert flights[0].flight_number == "UA100"
