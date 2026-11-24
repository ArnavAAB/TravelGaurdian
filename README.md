# TravelGuardian

A luxury concierge and autonomous travel assistant that monitors your flights, evaluates connection risks against airport minimum connection times (MCT) and calendar commitments, proposes optimized rebooking alternatives, and drafts communications. It **always asks for human approval before changing any booking**.

Powered by **NVIDIA Nemotron 3.5 Super VL** (`nvidia/nemotron-3.5-super-vl-120b-a12b`) with local tool execution.

---

## Quickstart: Running TravelGuardian

### 1. Interactive 3D Web Interface (Recommended)

TravelGuardian features a boarding-pass aesthetic frontend built with **Tailwind CSS**, **Three.js 3D transatlantic flight arcs**, and a **FastAPI** backend:

```powershell
# Start the web server and open the browser
python server.py
# or via CLI
python -m travelguardian.cli web
```
Visit **[http://localhost:8000](http://localhost:8000)** to:
- 🌍 Interact with the **3D Great-Circle Globe** (Three.js) connecting SFO → ORD → LHR.
- 🚨 Click **`Simulate +110m Delay`** to trigger an international connection crisis at ORD.
- 🤖 Watch the autonomous agent evaluate connection clearance, query flight alternatives, check calendar reachability, and present Proposal #TG-402.
- 🖋️ Hover and preview alternatives (`UA938`, `UA926`, `BA296`, `UA901`), then click **`Approve Rebooking`** to trigger the vintage ink stamping animation.
- ✉️ Open the **Drafts** and **Ask / Inbox** slide-over drawers to chat with Nemotron or view prepared hotel/airline notices.

> **Offline Demo**: You can also double-click `web/index.html` (or `Start-Process "web\index.html"`) to run the 3D frontend entirely offline in your browser.

---

### 2. Command Line Interface (CLI)

You can also run all scenarios directly in your terminal:

```powershell
# Verify connection to NVIDIA API and save agent config
python -m travelguardian.cli setup

# Run the delayed-flight interactive demo
python -m travelguardian.cli demo

# Check current itinerary, timeline, and conflict warnings
python -m travelguardian.cli itinerary

# Ask TravelGuardian free-form questions
python -m travelguardian.cli ask "What flights do I have booked?"

# Ingest and parse confirmation emails from samples/inbox
python -m travelguardian.cli reset --empty
python -m travelguardian.cli ingest

# Inspect prepared draft messages
python -m travelguardian.cli drafts

# Offline rule-based playbook (no API key needed)
python -m travelguardian.cli respond --offline
```

*Tip: Run `pip install -e .` to enable the shortcut `tg <command>` (e.g. `tg demo`, `tg web`).*

---

### 3. Voice Approval Loop (Gmail → Calendar → risk engine → phone call → approval)

```
Gmail sync ──► Nemotron extraction ──► trip store ──► Google Calendar flight events
                                           │
flight status (live or injected delay) ──► risk engine (connection time, MCT, calendar reachability)
                                           │ connection will be missed
                                           ▼
                              proposal TG-402 (rules pick the flight; Nemotron writes the explanation)
                                           │
              ElevenLabs outbound call (Twilio) ── agent explains ── traveler says yes / no
                                           │ MCP tools over https://<tunnel>/mcp (Bearer TG_API_TOKEN)
                                           ▼
       approval gate: pending-only, idempotent ──► email reply in the original Gmail thread
                                                ──► Google Calendar event updated
                                                ──► transcript stored as the approval record (dashboard)
```

Setup:

```bash
pip install -e ".[google]"          # Gmail/Calendar libraries (optional: local fallbacks otherwise)
cp .env.example .env                # fill in TG_API_TOKEN, REBOOK_EMAIL_TO, ElevenLabs ids, ...
tg google-auth                      # one-time OAuth consent (gmail.readonly, gmail.send, calendar.events)
python server.py                    # dashboard + API + /mcp on :8000
ngrok http 8000                     # public HTTPS for the voice agent and its webhook
```

Configure the ElevenLabs agent (MCP server, post-call webhook and prompt) as described in
[docs/elevenlabs-agent.md](docs/elevenlabs-agent.md). Then:

```bash
tg sync                 # read confirmations, extract with Nemotron, create calendar events
tg simulate-delay UA1542 110
tg evaluate             # risk engine -> proposal TG-402
tg call TG-402          # ElevenLabs calls TRAVELER_PHONE; the traveler approves or declines by voice
```

Or do the same from the dashboard: **Simulate +110m Delay**, then **Resolve with Agent** (runs the risk engine), then
**Call traveler for approval**. The approval record (transcript, who approved, email thread, calendar
event) shows up in the panel as the call completes. With `TG_WATCH_INTERVAL=60` and `TG_AUTO_CALL=true`
the server watches flights and calls on its own.

Safety rules enforced in code (`travelguardian/approval.py`):
- **Rules decide, the LLM explains.** The alternative is picked by deterministic ranking. Nemotron only writes the rationale and the email, and its email is discarded if it names a different flight.
- **Explicit approval.** `approve_proposal` requires the traveler's quoted words. The REST and MCP endpoints require the bearer token. Dashboard buttons work only from the local machine, not through the tunnel.
- **Pending only, idempotent.** A declined or failed proposal can't be approved. Approving twice returns the recorded result without sending a second email.
- **No real airline is contacted.** Requests go to `REBOOK_EMAIL_TO` (with Gmail, your own address by default), and fares/seats are marked unverified.
- **Fallbacks.** Without Google credentials, the sample inbox and a local calendar file are used. Without ElevenLabs, call endpoints return 503 and everything else works.

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/sync` | none | Gmail (or samples) → extraction → trip → calendar |
| `POST /api/evaluate` | none (`call: true` needs local/token) | Risk engine; creates proposals |
| `GET /api/proposals[/{id}]` | none | Proposals with call, transcript, actions |
| `POST /api/proposals/{id}/call` | local or token | Start the ElevenLabs outbound call |
| `POST /api/proposals/{id}/approve` \| `/decline` | Bearer token | Approval gate for integrations |
| `POST /api/proposals/{id}/transcript` | none | Pull the transcript from ElevenLabs |
| `POST /api/webhooks/elevenlabs` | HMAC signature | Post-call transcript webhook |
| `/mcp` | Bearer token | MCP server: `get_proposal`, `approve_proposal`, `decline_proposal` |
| `GET /api/config` | none | Which integrations are live vs simulated |

---

## The Core Scenario: Disruption at ORD

Sample itinerary: **SFO → ORD (UA1542)** connecting to **ORD → LHR (UA958)**, hotel in London, and a critical **Northwind Client Kickoff meeting** at 10:00 GMT the morning after landing.

```
                  UA1542 (+110m delay)                UA958 (Leaves 15:50 CST)
   SFO [07:00 PST] ──────────────────► ORD [15:05 CST] ──────────────────────► LHR [06:05 GMT]
                                                │
                                       Connection buffer: 45m
                                       ORD Intl MCT:      90m
                                       ──────────────────────
                                       DEFICIT:          -45m (HIGH RISK / MISSED)
```

1. **Delay Detection**: UA1542 is delayed 110 minutes (departing 08:50 PST, arriving 15:05 CST).
2. **Deterministic Risk Assessment**: Layover is only 45 minutes against ORD's 90-minute Minimum Connection Time. Risk is classified as **HIGH**.
3. **Alternative Search & Calendar Preview**:
   - `UA938` (ORD→LHR, departs 18:00 CST, arrives 07:25 GMT): **Protected (free)**. With 105m ground transfer to 30 Fenchurch St, traveler reaches meeting at 09:10 GMT (before 10:00 kickoff). **RECOMMENDED**.
   - `UA926` (ORD→LHR, arrives 11:10 GMT): Free, but misses client kickoff by 70 minutes.
   - `BA296` (ORD→LHR, arrives 07:40 GMT): Reaches meeting, but incurs +$1,240 fare difference.
   - `UA901` (SFO→LHR nonstop, arrives 10:45 GMT): Misses kickoff by 45 minutes.
4. **Human Approval Gate**: Change is proposed as `#TG-402`. Rebooking only executes when the traveler confirms approval.
5. **Draft Generation**: Prepares notices to Kingsway Grand Hotel regarding the new arrival time and requesting an extension for the missing night of Tue 08 Dec, plus airline customer care fallback drafts.

---

## Architecture

```
                    ┌────────────────────────────────────────────────────────┐
                    │  3D Web UI (web/index.html + Three.js + Tailwind CSS)  │
                    └───────────────────────────┬────────────────────────────┘
                                                │ HTTP / REST
                    ┌───────────────────────────▼────────────────────────────┐
                    │               FastAPI Server (server.py)               │
                    └───────────────────────────┬────────────────────────────┘
                                                │
 ┌──────────────────────┐                       │                       ┌──────────────────────┐
 │    CLI (tg / cli.py) ├───────────────────────┤◄──────────────────────┤  MCP Server (tg mcp) │
 └──────────────────────┘                       │                       └──────────────────────┘
                                                │
                   ┌────────────────────────────▼────────────────────────────┐
                   │             ToolBox & Approval Gate (tools.py)          │
                   │           decide(), defer_all(), itinerary rules        │
                   └──────┬─────────────────────┬─────────────────────┬──────┘
                          │                     │                     │
          ┌───────────────▼──────┐    ┌─────────▼────────────┐  ┌─────▼────────────────┐
          │ NVIDIA Nemotron      │    │ Flight Providers:    │  │ Trip Store           │
          │ 3.5 Super VL         │    │ • AirLabsProvider    │  │ (data/trip.json)     │
          │ (agent.py, ingest.py)│    │ • SimulatedProvider  │  │ Calendar, drafts     │
          └──────────────────────┘    └──────────────────────┘  └──────────────────────┘
```

- **Deterministic Rules in Code**: Connection risk, minimum connection times (MCT), hotel gap detection, and calendar reachability are evaluated with tested algorithms (`itinerary.py`), not prompt guesswork.
- **LLM Reasoning & Synthesis**: NVIDIA Nemotron 3.5 Super VL selects optimal flight alternatives, reasons through ground transport trade-offs, and drafts human-ready notices.
- **Strict Approval Gate**: `request_booking_change` cannot mutate trip records without an explicit `approve` call from the user.
- **Live Flight Feeds & Fallbacks**: `AirLabsProvider` pulls live radar, delay, and gate telemetry via AirLabs v9 with disk-backed TTL caching (`airlabs_cache.json`). It seamlessly prioritizes simulated demo disruptions and falls back to `SimulatedProvider` when offline or out of horizon.
- **Local Tool Execution**: All booking credentials, calendar data, and trip state remain strictly local.

---

## CLI Command Reference

| Command | Description |
|---|---|
| `tg web` / `python server.py` | Launches the interactive 3D Web UI at `http://localhost:8000` |
| `tg setup` | Tests connection to NVIDIA API and writes `.travelguardian.json` |
| `tg demo [--offline]` | Runs the delayed-flight simulation walkthrough |
| `tg itinerary` | Displays formatted timeline, conflict alerts, and pending approvals |
| `tg bookings` | Groups confirmed flights and hotels by confirmation number |
| `tg status` | Polls flight statuses and flags schedule changes |
| `tg simulate-delay <flight> <min>` | Injects a simulated delay or cancellation into the scenario |
| `tg respond [--offline]` | Executes the agent disruption recovery playbook |
| `tg watch [--interval N]` | Polls flight statuses in a background loop and triggers recovery |
| `tg ask "<question>"` | Free-form natural language query to the Nemotron assistant |
| `tg approve <id>` / `decline <id>` | Decides a pending rebooking proposal |
| `tg drafts` | Displays prepared email and message drafts |
| `tg reset [--empty]` | Resets trip data to baseline or empty state |
| `tg ingest [--inbox DIR]` | Parses `.eml` confirmations using Nemotron structured outputs |
| `tg mcp` | Runs standard MCP server on stdio for MCP hosts |
| `tg google-auth` | One-time Google OAuth consent for Gmail + Calendar |
| `tg sync` | Reads confirmations (Gmail or samples), extracts bookings, syncs flight events to the calendar |
| `tg evaluate` | Runs the risk engine and creates a rebooking proposal (TG-402) for a missed connection |
| `tg call <id> [--to +1...]` | ElevenLabs voice agent calls the traveler about a proposal |

---

## Configuration & Credentials

### NVIDIA API (Required for Agent Reasoning)
Set your NVIDIA API key in your environment (defaults to pre-configured key):

- **PowerShell**:
  ```powershell
  $env:NVIDIA_API_KEY = "nvapi-..."
  ```
- **Bash / macOS**:
  ```bash
  export NVIDIA_API_KEY="nvapi-..."
  ```

API Base URL: `https://integrate.api.nvidia.com/v1`  
Model ID: `nvidia/nemotron-3.5-super-vl-120b-a12b`

### AirLabs API (Optional for Live Flight Feeds)
To pull real-time live flight tracking, delays, and gate assignments:

```powershell
$env:AIRLABS_API_KEY = "your-airlabs-api-key"
```

When set:
- Both `server.py` and `travelguardian.cli` automatically route status queries through `AirLabsProvider`.
- Live requests are cached to disk in `airlabs_cache.json` with a 5-minute TTL to stay well within free API quotas.
- Interactive simulation buttons in the Web UI still take immediate precedence for effortless disruption testing.
- When unset, TravelGuardian transparently runs with `SimulatedProvider`.

Data is persisted in `./data` (override with `TG_HOME`).

---

## Running the Test Suite

```powershell
python -m pytest
```

Runs 48 tests. They cover itinerary building, MCT calculation, simulated provider disruptions, AirLabs caching and fallback, Nemotron agent loops and structured output parsing. They also cover the voice approval loop: proposal creation, token-protected and idempotent approvals, replies in the original thread, calendar replacement, the MCP tools over HTTP, ElevenLabs call and transcript handling (mocked), webhook signatures, Gmail/Calendar API usage (faked services) and the background monitor.
