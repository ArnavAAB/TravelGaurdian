"""`tg` command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import render
from .providers import SimulatedProvider
from .store import SAMPLES, Store
from .tools import ToolBox, defer_all


def _toolbox(store: Store, interactive: bool = True) -> ToolBox:
    import os
    if os.getenv("AIRLABS_API_KEY"):
        from .airlabs_provider import AirLabsProvider
        provider = AirLabsProvider(store.scenario_path)
    else:
        provider = SimulatedProvider(store.scenario_path)
    return ToolBox(store, provider, approver=render.approval_prompt if interactive else defer_all, now=provider.now)


def _require_data(store: Store) -> None:
    if not store.scenario_path.exists():
        raise SystemExit("No trip data yet. Run `tg reset` (sample trip) or `tg reset --empty && tg ingest`.")


def _client():
    import os
    from openai import OpenAI

    api_key = os.environ.get("NVIDIA_API_KEY", "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")
    base_url = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    return OpenAI(base_url=base_url, api_key=api_key)


def cmd_reset(a, store: Store) -> None:
    store.reset(with_bookings=not a.empty)
    print(f"Reset trip data in {store.root}" + (" (no bookings; run `tg ingest`)" if a.empty else " (sample trip loaded)"))


def cmd_ingest(a, store: Store) -> None:
    from .ingest import extract, merge, read_inbox, to_bookings

    _require_data(store)
    client = _client()
    trip = store.load()
    for msg in read_inbox(Path(a.inbox)):
        x = extract(client, msg)
        if not x.is_travel_booking:
            print(f"  skip  {msg.subject}")
            continue
        flights, hotels = to_bookings(x, msg.id)
        added, updated = merge(trip, flights, hotels)
        print(f"  read  {msg.subject}: {len(flights)} flight(s), {len(hotels)} hotel(s) [{added} new, {updated} updated]")
    store.save(trip)


def cmd_itinerary(a, store: Store) -> None:
    _require_data(store)
    tb = _toolbox(store)
    data = json.loads(tb.call("get_itinerary", {})[0])
    render.itinerary(data)
    render.conflicts(data["conflicts"])
    if data["pending_changes"]:
        render.heading("Pending approval")
        for p in data["pending_changes"]:
            print(f"  {p['id']}: replace {', '.join(p['segment_ids'])} with {p['alternative_id']} · {p['reason']}")


def cmd_bookings(a, store: Store) -> None:
    _require_data(store)
    data = json.loads(_toolbox(store).call("list_bookings", {})[0])
    render.heading(f"Bookings · {data['traveler']}")
    for b in data["bookings"]:
        print(f"  {render.bold(b['confirmation'])}  {b['provider']} ({b['type']})")
        for i in b["items"]:
            print(f"      {i}")
    print(f"\n  {len(data['drafts'])} draft(s). See `tg drafts`.")


def cmd_status(a, store: Store) -> None:
    _require_data(store)
    data = json.loads(_toolbox(store).call("check_flight_statuses", {})[0])
    render.heading("Flight status")
    for sid, s in data["statuses"].items():
        tag = {"on_time": render.c("on time", "32"), "delayed": render.c(f"delayed {s['delay_minutes']} min", "1;31"), "cancelled": render.c("CANCELLED", "1;31")}[s["status"]]
        print(f"  {s['flight_number']:8} {tag}" + (f"  · {s['reason']}" if s.get("reason") else ""))
    if data["changed_since_last_check"]:
        print(render.bold(f"\n  {len(data['changed_since_last_check'])} change(s) since last check."))


def cmd_simulate(a, store: Store) -> None:
    _require_data(store)
    prov = SimulatedProvider(store.scenario_path)
    trip = store.load()
    f = next((x for x in trip.flights if x.flight_number == a.flight.upper()), None)
    if f is None:
        raise SystemExit(f"No booked flight {a.flight}")
    if a.clear:
        prov.clear_disruptions()
        print("Cleared all simulated disruptions.")
        return
    prov.set_disruption(f.flight_number, f.departs.date().isoformat(), delay_minutes=a.minutes, cancelled=a.cancel, reason=a.reason)
    print(f"Simulated: {f.flight_number} " + ("cancelled" if a.cancel else f"delayed {a.minutes} min") + f" ({a.reason})")


def cmd_respond(a, store: Store) -> None:
    _require_data(store)
    tb = _toolbox(store)
    if a.offline:
        from .offline import respond

        respond(tb)
    else:
        from .agent import run

        run(
            _client(),
            tb,
            "Check my flights. If anything is disrupted, handle it: check my connection, find options, show me the revised itinerary and ask me before changing anything.",
            on_tool=render.tool_call,
            on_reasoning=render.reasoning,
            follow_up=_follow_up,
        )
    _show_after(store)


def cmd_ask(a, store: Store) -> None:
    _require_data(store)
    from .agent import run

    run(_client(), _toolbox(store), " ".join(a.question), on_tool=render.tool_call, on_reasoning=render.reasoning, follow_up=_follow_up)


def _follow_up():
    if not sys.stdin.isatty():
        return None
    try:
        text = input(render.bold("\nYou (enter to finish) > ")).strip()
    except EOFError:
        return None
    return text or None


def _show_after(store: Store) -> None:
    tb = _toolbox(store)
    data = json.loads(tb.call("get_itinerary", {})[0])
    render.itinerary(data)
    render.conflicts(data["conflicts"], "Remaining conflicts")
    render.drafts([d.model_dump(mode="json") for d in store.load().drafts])


def cmd_watch(a, store: Store) -> None:
    """Poll flight status; run the disruption playbook when something changes."""
    _require_data(store)
    print(f"Watching flights every {a.interval}s. Ctrl-C to stop.")
    while True:
        tb = _toolbox(store)
        changes = json.loads(tb.call("check_flight_statuses", {})[0])["changed_since_last_check"]
        bad = [c for c in changes if c["status"] != "on_time"]
        stamp = datetime.now().strftime("%H:%M:%S")
        if bad:
            print(render.c(f"[{stamp}] Schedule change: " + ", ".join(f"{c['flight_number']} {c['status']} {c['delay_minutes']}m" for c in bad), "1;31"))
            cmd_respond(a, store)
            if a.once:
                return
        else:
            print(render.dim(f"[{stamp}] all flights unchanged"))
        time.sleep(a.interval)


def cmd_approve(a, store: Store, approved: bool = True) -> None:
    from . import approval
    from .services import build_services

    _require_data(store)
    svc = build_services(store)
    try:
        if approved:
            r = approval.approve(svc, a.proposal_id, channel="traveler (cli)")
        else:
            r = approval.decline(svc, a.proposal_id, channel="traveler (cli)")
    except approval.GateError as e:
        raise SystemExit(f"{e.status_code}: {e.message}")
    print(json.dumps(r, indent=2, default=str))


def cmd_google_auth(a, store: Store) -> None:
    from .google_auth import credentials
    from .settings import load_settings

    settings = load_settings()
    credentials(settings, interactive=True)
    print(f"Google authorised (Gmail read/send, Calendar events). Token saved to {settings.google_token_path}")


def cmd_sync(a, store: Store) -> None:
    from .services import build_services
    from .sync import sync_inbox

    _require_data(store)
    svc = build_services(store)
    for note in svc.notes:
        print(render.dim(f"  note: {note}"))
    r = sync_inbox(svc)
    for p in r["processed"]:
        print(f"  {p['status']:24} {p['subject']}")
    print(f"  {r['flight_events']} flight event(s) in {r['calendar_provider']} calendar; "
          f"{r['meetings_imported']} meeting(s) imported")


def cmd_evaluate(a, store: Store) -> None:
    from .proposals import evaluate, view
    from .services import build_services

    _require_data(store)
    svc = build_services(store)
    r = evaluate(svc)
    for c in r["connections"]:
        print(f"  {c['airport']}: {c['layover_minutes']} min vs MCT {c['minimum_connection_minutes']} -> {c['risk']}")
    trip = store.load()
    for pid in r["created"] + r["existing"]:
        v = view(trip.proposal(pid))
        print(render.bold(f"\n  {pid} [{v['status']}]") + f"  {v['summary']}")
    if not (r["created"] or r["existing"]):
        print("  No connection at risk. Nothing to propose.")


def cmd_call(a, store: Store) -> None:
    from .services import build_services
    from .voice import ElevenLabsVoice, VoiceError

    _require_data(store)
    svc = build_services(store)
    trip = store.load()
    prop = trip.proposal(a.proposal_id)
    if prop is None or prop.status != "pending":
        raise SystemExit(f"{a.proposal_id} is not a pending proposal.")
    try:
        prop.call = ElevenLabsVoice(svc.settings).start_call(prop, a.to or svc.settings.traveler_phone, svc.now())
    except VoiceError as e:
        raise SystemExit(str(e))
    store.save(trip)
    print(f"Calling {prop.call.to_number} about {prop.id} (conversation {prop.call.conversation_id}).")


def cmd_drafts(a, store: Store) -> None:
    _require_data(store)
    render.drafts([d.model_dump(mode="json") for d in store.load().drafts])


def cmd_setup(a, store: Store) -> None:
    from .agent import CONFIG, setup

    cfg = setup(_client())
    print(f"Agent ready using model {cfg.get('model')} at {cfg.get('base_url')}. Saved to {CONFIG}")


def cmd_demo(a, store: Store) -> None:
    """The killer demo, end to end."""
    store.reset()
    render.heading("TravelGuardian demo")
    print("  Sample trip loaded (as read from samples/inbox). Morning of departure, 05:30 at SFO.")
    cmd_itinerary(a, store)
    input(render.dim("\n  [enter] to simulate the delay ")) if sys.stdin.isatty() else None
    prov = SimulatedProvider(store.scenario_path)
    prov.set_disruption("UA1542", "2026-12-03", delay_minutes=110, reason="Late-arriving aircraft")
    print(render.c("\n  ⚠  United pushes a status update: UA1542 SFO->ORD delayed 110 min.", "1;31"))
    cmd_respond(a, store)


def cmd_mcp(a, store: Store) -> None:
    from .mcp_server import main as mcp_main

    mcp_main()


def cmd_web(a, store: Store) -> None:
    import subprocess
    import sys
    import webbrowser

    server_script = Path(__file__).resolve().parent.parent / "server.py"
    print("Launching TravelGuardian Web Interface at http://127.0.0.1:8000 ...")
    try:
        webbrowser.open("http://127.0.0.1:8000")
    except Exception:
        pass
    subprocess.run([sys.executable, str(server_script)])


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="tg", description="TravelGuardian: watches your trip and fixes disruptions, with your approval.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("reset", help="load the sample trip (or start empty)")
    s.add_argument("--empty", action="store_true", help="no bookings; fill them with `tg ingest`")
    s = sub.add_parser("ingest", help="read confirmation emails with Nemotron")
    s.add_argument("--inbox", default=str(SAMPLES / "inbox"))
    sub.add_parser("itinerary", help="show itinerary and conflicts")
    sub.add_parser("bookings", help="bookings grouped by confirmation")
    sub.add_parser("status", help="check live flight status")
    s = sub.add_parser("simulate-delay", help="make the simulated airline delay or cancel a flight")
    s.add_argument("flight")
    s.add_argument("minutes", type=int, nargs="?", default=0)
    s.add_argument("--cancel", action="store_true")
    s.add_argument("--clear", action="store_true")
    s.add_argument("--reason", default="Late-arriving aircraft")
    s = sub.add_parser("respond", help="handle disruptions now")
    s.add_argument("--offline", action="store_true", help="rule-based playbook, no LLM")
    s = sub.add_parser("watch", help="poll and respond to schedule changes")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--offline", action="store_true")
    s.add_argument("--once", action="store_true", help="exit after handling one disruption")
    s = sub.add_parser("ask", help="ask TravelGuardian anything about the trip")
    s.add_argument("question", nargs="+")
    s = sub.add_parser("approve", help="approve a pending booking change")
    s.add_argument("proposal_id")
    s = sub.add_parser("decline", help="decline a pending booking change")
    s.add_argument("proposal_id")
    sub.add_parser("drafts", help="show draft messages")
    sub.add_parser("setup", help="verify connection and save agent config")
    s = sub.add_parser("demo", help="run the delayed-flight demo")
    s.add_argument("--offline", action="store_true")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    sub.add_parser("web", help="launch the web dashboard and API server")
    sub.add_parser("google-auth", help="one-time Google OAuth consent (Gmail + Calendar)")
    sub.add_parser("sync", help="read confirmations from Gmail (or samples) and sync flight events to the calendar")
    sub.add_parser("evaluate", help="run the risk engine; create a rebooking proposal for a missed connection")
    s = sub.add_parser("call", help="have the ElevenLabs voice agent call the traveler about a proposal")
    s.add_argument("proposal_id")
    s.add_argument("--to", help="E.164 number; defaults to TRAVELER_PHONE")

    a = p.parse_args(argv)
    from .settings import load_dotenv

    load_dotenv()
    store = Store()
    handlers = {
        "reset": cmd_reset, "ingest": cmd_ingest, "itinerary": cmd_itinerary, "bookings": cmd_bookings,
        "status": cmd_status, "simulate-delay": cmd_simulate, "respond": cmd_respond, "watch": cmd_watch,
        "ask": cmd_ask, "approve": cmd_approve, "decline": lambda a, s: cmd_approve(a, s, approved=False),
        "drafts": cmd_drafts, "setup": cmd_setup, "demo": cmd_demo, "mcp": cmd_mcp, "web": cmd_web,
        "google-auth": cmd_google_auth, "sync": cmd_sync, "evaluate": cmd_evaluate, "call": cmd_call,
    }
    try:
        handlers[a.cmd](a, store)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
