import json
from pathlib import Path
import pytest

from travelguardian.airlabs_provider import AirLabsProvider, _TTLDiskCache
from travelguardian.models import Flight
from travelguardian.store import Store
from travelguardian.tools import ToolBox, deny_all


@pytest.fixture
def airlabs_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRLABS_API_KEY", raising=False)
    store = Store(tmp_path / "store")
    store.reset()
    provider = AirLabsProvider(store.scenario_path, cache_path=tmp_path / "cache.json")
    return store, provider, tmp_path


def test_airlabs_fallback_to_simulated(airlabs_env):
    store, provider, _ = airlabs_env
    trip = store.load()
    f = trip.flights[0]

    # Baseline status
    st = provider.status(f)
    assert st.status == "on_time"
    assert st.flight_number == f.flight_number

    # Set disruption in simulated provider - AirLabsProvider must prioritize it
    provider.base.set_disruption(f.flight_number, f.departs.date().isoformat(), delay_minutes=95, reason="Mechanical check")
    st_delayed = provider.status(f)
    assert st_delayed.status == "delayed"
    assert st_delayed.delay_minutes == 95
    assert st_delayed.reason == "Mechanical check"


def test_airlabs_search_and_rebook(airlabs_env):
    store, provider, _ = airlabs_env
    trip = store.load()
    # Flight UA958: ORD -> LHR
    f = trip.flights[1]

    alts = provider.search(f.origin, f.destination, f.departs)
    assert len(alts) > 0
    first_alt = alts[0]
    assert first_alt.departs.tzinfo is not None
    assert hasattr(first_alt, "fare_difference")
    assert hasattr(first_alt, "seats_available")

    # Offer and rebooking delegation
    offered = provider.offer(first_alt.id)
    assert offered.id == first_alt.id
    
    rebooked = provider.rebook(f, first_alt.id)
    assert rebooked is not None
    assert rebooked.flight_number == first_alt.flight_number


def test_airlabs_ttl_disk_cache(tmp_path):
    cache_path = tmp_path / "test_cache.json"
    cache = _TTLDiskCache(cache_path, ttl_seconds=10)

    cache.set("flight_UA1542", {"status": "delayed", "delay": 45})
    assert cache.get("flight_UA1542") == {"status": "delayed", "delay": 45}

    # Verify persisted to disk
    assert cache_path.exists()
    disk_data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "flight_UA1542" in disk_data

    # Test expired TTL
    cache.set("flight_expired", {"test": 123}, ttl_seconds=-1)
    assert cache.get("flight_expired") is None


def test_toolbox_integration_with_airlabs(airlabs_env):
    store, provider, _ = airlabs_env
    tb = ToolBox(store, provider, approver=deny_all, now=provider.now)
    text, err = tb.call("check_flight_statuses", {})
    assert not err
    res = json.loads(text)
    assert "statuses" in res


def test_airlabs_mocked_live_request(tmp_path, monkeypatch):
    import io
    store = Store(tmp_path / "store")
    store.reset()
    provider = AirLabsProvider(
        store.scenario_path,
        api_key="mock_test_key",
        cache_path=tmp_path / "cache.json",
        allow_live_date_mismatch=True,
    )
    trip = store.load()
    f = trip.flights[0]

    mock_resp = {
        "response": {
            "flight_iata": f.flight_number,
            "status": "en-route",
            "arr_delayed": 65,
            "dep_gate": "F14",
        }
    }

    class DummyResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(mock_resp).encode("utf-8")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: DummyResponse())

    st = provider.status(f)
    assert st.status == "delayed"
    assert st.delay_minutes == 65
    assert "AirLabs live update" in (st.reason or "")



def test_airlabs_search_ignores_live_flights_outside_the_window(tmp_path, monkeypatch):
    """AirLabs /schedules returns the next ~10 hours; a trip on another day must not get those flights."""
    from datetime import datetime

    store = Store(tmp_path / "store")
    store.reset()
    provider = AirLabsProvider(store.scenario_path, api_key="k", cache_path=tmp_path / "c.json",
                               allow_live_date_mismatch=True)
    earliest = datetime.fromisoformat("2026-12-03T16:35:00-06:00")
    rows = {"today": [{"flight_iata": "UA929", "dep_iata": "ORD", "arr_iata": "LHR",
                       "dep_time": "2026-11-24 15:45", "arr_time": "2026-11-25 05:55", "status": "scheduled"}],
            "in_window": [{"flight_iata": "UA929", "dep_iata": "ORD", "arr_iata": "LHR",
                           "dep_time": "2026-12-03 19:00", "arr_time": "2026-12-04 08:30", "status": "scheduled"}]}

    monkeypatch.setattr(provider, "_request", lambda endpoint, params: rows["today"])
    stale = provider.search("ORD", "LHR", earliest)
    assert stale and all(a.id.startswith("alt-") for a in stale)  # simulated fallback, not today's UA929

    monkeypatch.setattr(provider, "_request", lambda endpoint, params: rows["in_window"])
    live = provider.search("ORD", "LHR", earliest)
    assert [a.flight_number for a in live] == ["UA929"] and live[0].departs >= earliest
