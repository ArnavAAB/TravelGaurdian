# ElevenLabs voice agent setup

TravelGuardian places an outbound call through ElevenLabs Conversational AI over a Twilio number. The
agent explains the disruption and acts **only** through three MCP tools served by this backend:

| Tool | When the agent calls it |
|---|---|
| `get_proposal(proposal_id)` | At the start, to read the delay, the missed connection and the recommended flight |
| `approve_proposal(proposal_id, traveler_said)` | Only after the traveler clearly says yes. `traveler_said` quotes their words |
| `decline_proposal(proposal_id, reason)` | When the traveler says no |

Approval goes through the backend gate. Only a *pending* proposal can be approved, repeating an approval
does nothing new, and a declined proposal can't be approved later. After approval the backend emails the
rescheduling request in the original confirmation thread, updates the calendar and keeps the transcript.

## 1. Expose the backend

```bash
python server.py                                    # http://127.0.0.1:8000
ngrok http 8000                                     # or: cloudflared tunnel --url http://localhost:8000
```

## 2. Phone number

In ElevenLabs, go to **Phone Numbers**, import your Twilio number and assign the agent to it. Copy the
phone number id into `ELEVENLABS_PHONE_NUMBER_ID`. A Twilio trial account can only call verified numbers.

## 3. MCP server

MCP is off by default per workspace. Enable it in the Agents Platform settings, then **Add Custom MCP Server**:

- **Server URL:** `https://<your-tunnel>/mcp`
- **Transport:** Streamable HTTP
- **Secret token:** the value of `TG_API_TOKEN`. The server accepts `Authorization: Bearer <token>` or the bare token.
- **Tool approval:** auto-approve these three tools (Fine-Grained, or No Approval). The traveler's spoken
  yes is the approval, and the backend still refuses anything that isn't pending.

Add the server to the agent.

## 4. Post-call webhook (stores the transcript)

In the Agents Platform settings, set the post-call webhook to `https://<your-tunnel>/api/webhooks/elevenlabs`.
Enable the transcription event and put the signing secret in `ELEVENLABS_WEBHOOK_SECRET`. Requests
with a bad or stale `ElevenLabs-Signature` are rejected. The dashboard can also pull the transcript on
demand with `POST /api/proposals/{id}/transcript`.

## 5. Agent prompt

Each call passes these dynamic variables: `proposal_id`, `traveler_name`, `delayed_flight`,
`delay_minutes`, `new_arrival`, `connection_airport`, `missed_flight`, `missed_departure`,
`layover_minutes`, `mct_minutes`, `alternative_flight`, `alternative_route`, `alternative_departs`,
`alternative_arrives`, `rationale`, `fare_note`.

**First message**

```
Hi {{traveler_name}}, this is TravelGuardian calling about your trip. Your flight {{delayed_flight}} is running {{delay_minutes}} minutes late. Do you have a minute?
```

**System prompt**

```
You are TravelGuardian, a calm, concise travel assistant on a phone call with {{traveler_name}}.

Facts from TravelGuardian's rules engine (proposal {{proposal_id}}):
- {{delayed_flight}} is delayed {{delay_minutes}} minutes and now arrives {{new_arrival}}.
- That leaves {{layover_minutes}} minutes to connect at {{connection_airport}}. The minimum connection
  time there is {{mct_minutes}} minutes, so {{missed_flight}} ({{missed_departure}}) will be missed.
- Recommended replacement: {{alternative_flight}}, {{alternative_route}}, departing {{alternative_departs}},
  arriving {{alternative_arrives}}.
- Why: {{rationale}}
- {{fare_note}}

How to run the call:
1. Call get_proposal with proposal_id "{{proposal_id}}" before explaining. If its facts differ from the
   ones above, use get_proposal's.
2. Explain the delay, the missed connection and the recommended flight in plain words, in under 40 seconds.
3. Ask one clear question: "Shall I send the rebooking request for {{alternative_flight}}?"
4. Only if the traveler clearly says yes to this change, call approve_proposal with proposal_id
   "{{proposal_id}}" and traveler_said set to their exact words. Then tell them the request was sent in
   their original booking email thread and their calendar was updated. If the tool result lists errors,
   say the change is approved and the follow-up will be retried.
5. If they say no, call decline_proposal and confirm that nothing was changed.
6. If the answer is unclear or conditional ("maybe", "what about a later flight?"), do not call
   approve_proposal or decline_proposal. Answer, then ask again. You cannot book other flights on this call.

Never say the airline has confirmed the booking. Never quote prices or seat availability.
```

## 6. Environment

```
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
ELEVENLABS_PHONE_NUMBER_ID=...
ELEVENLABS_WEBHOOK_SECRET=...
TRAVELER_PHONE=+1...
TG_API_TOKEN=...
```

To place a call, click **Call traveler for approval** on the dashboard or run `tg call TG-402`. To have
the monitor call automatically, set `TG_WATCH_INTERVAL=60` and `TG_AUTO_CALL=true`.
