import pytest
from fastapi.testclient import TestClient

from server import app
from travelguardian.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    store_dir = tmp_path / "tg_data"
    monkeypatch.setenv("TG_HOME", str(store_dir))
    monkeypatch.delenv("AIRLABS_API_KEY", raising=False)
    monkeypatch.delenv("USE_LIVE_AIRLABS", raising=False)
    monkeypatch.setenv("TG_LLM", "off")  # no model calls in tests: template rationale/email
    monkeypatch.setenv("TG_GOOGLE", "off")  # local inbox/calendar
    monkeypatch.setenv("TG_DASHBOARD_APPROVALS", "open")  # TestClient is not a loopback client

    store = Store(store_dir)
    store.reset()

    with TestClient(app) as c:
        yield c, store


def test_server_static_index(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "TravelGuardian" in resp.text


def test_server_api_status(client):
    c, _ = client
    resp = c.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "statuses" in data
    assert data["provider"] == "simulated"


def test_server_api_itinerary(client):
    c, _ = client
    resp = c.get("/api/itinerary")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "conflicts" in data


def test_server_api_simulate_and_clear_delay(client):
    c, store = client
    # Simulate delay
    resp = c.post(
        "/api/simulate-delay",
        json={"flight": "UA1542", "delay": 110, "reason": "Late-arriving aircraft"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["delay"] == 110

    # Verify status reflects delay
    st_resp = c.get("/api/status")
    st_data = st_resp.json()
    assert st_data["statuses"]["seg-UA1542-20261203"]["delay_minutes"] == 110

    # Clear disruptions
    clear_resp = c.post("/api/clear-disruptions")
    assert clear_resp.status_code == 200
    assert clear_resp.json()["ok"] is True

    # Verify cleared
    st_resp2 = c.get("/api/status")
    assert st_resp2.json()["statuses"]["seg-UA1542-20261203"]["delay_minutes"] == 0


def test_server_api_respond_offline(client):
    c, _ = client
    resp = c.post("/api/respond", json={"offline": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "offline"
    assert "itinerary" in data
    assert "statuses" in data


def test_server_api_drafts(client):
    c, _ = client
    resp = c.get("/api/drafts")
    assert resp.status_code == 200
    data = resp.json()
    assert "drafts" in data
    assert "count" in data


def test_server_api_approve_with_option(client):
    c, store = client
    # First simulate disruption
    c.post("/api/simulate-delay", json={"flight": "UA1542", "delay": 110, "reason": "Weather"})

    # Approve option UA938 directly
    resp = c.post("/api/approve", json={"id": "TG-402", "option": "UA938"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "applied"
    assert data["result"]["id"] == "TG-402"
    assert data["result"]["new_segment_id"] == "seg-UA938-20261203"
    assert {"email", "calendar"} <= set(data["result"]["actions"])


def test_server_api_decline(client):
    c, store = client
    # Trigger a pending proposal via toolbox
    from server import get_toolbox
    tb = get_toolbox(store)
    trip = store.load()
    f = trip.flights[1]
    tb.t_request_booking_change([f.id], "alt-UA938", "Test proposal")

    itin = c.get("/api/itinerary").json()
    assert len(itin["pending_changes"]) > 0
    prop_id = itin["pending_changes"][0]["id"]

    # Decline
    resp = c.post("/api/decline", json={"id": prop_id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "declined"
