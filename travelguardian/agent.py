"""TravelGuardian powered by NVIDIA Nemotron VL.

Tools execute locally on your machine against your trip data. That keeps
booking credentials and the approval prompt local.

    tg setup          # verify connection and save config
    tg respond        # run the disruption playbook
    tg ask "..."      # anything else ("organise my bookings", "draft a cancellation for ...")
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Callable, Optional

import openai
from openai import OpenAI

from .store import PACKAGE_ROOT
from .tools import TOOL_SPECS, ToolBox

MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
CONFIG = Path(os.environ.get("TG_AGENT_CONFIG", PACKAGE_ROOT / ".travelguardian.json"))

SYSTEM = """You are TravelGuardian, a travel assistant that looks after one traveler's trip.

You can read their itinerary (flights, hotels, calendar), check live flight status, assess connections, search alternative flights, estimate ground transfer times, preview a revised itinerary, request a booking change, and save draft messages.

When a flight is delayed or cancelled:
1. Confirm the disruption with check_flight_statuses.
2. Check every onward connection with assess_connection.
3. If a connection is at high risk or missed, search alternatives from the hint it returns. If the traveler hasn't left yet, also consider an alternative from their current origin.
4. Preview the best one or two options with preview_change. Weigh arrival time against calendar commitments (use estimate_ground_transfer for meetings), cost, and seat availability. Prefer protected rebookings, which cost nothing.
5. Call request_booking_change for your recommended option. This asks the traveler. Never describe a change as made unless the tool result says "applied".
6. Save drafts for what the traveler should send: a note to the hotel if arrival changes, and a meeting reschedule if a calendar event is now at risk. If they declined the change, draft a rebooking request to the airline for the option they might prefer instead.

Booking changes happen only through request_booking_change, and only with the traveler's approval. Drafts are never sent by you.

Finish with a short summary for the traveler: what happened, what changed (or is pending), the revised itinerary in a few lines, and which drafts are waiting for them. Write plainly; times in local time with the airport code."""


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        }
        for spec in TOOL_SPECS
    ]


def load_config() -> dict:
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def setup(client: OpenAI) -> dict:
    """Verify connection to NVIDIA API and save config."""
    cfg = load_config()
    try:
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to connect to NVIDIA API: {e}")
    cfg["model"] = MODEL
    cfg["base_url"] = str(getattr(client, "base_url", "https://integrate.api.nvidia.com/v1"))
    cfg["status"] = "ready"
    CONFIG.write_text(json.dumps(cfg, indent=2))
    return cfg


def run(
    client: OpenAI,
    toolbox: ToolBox,
    prompt: str,
    on_text: Callable[[str], None] = lambda t: print(t, end="", flush=True),
    on_tool: Callable[[str, dict], None] = lambda n, a: None,
    on_reasoning: Optional[Callable[[str], None]] = None,
    follow_up: Optional[Callable[[], Optional[str]]] = None,
    max_turns: int = 15,
) -> str:
    """Start an agent session with local tool execution."""
    session_id = f"sesn_{secrets.token_hex(6)}"
    base_url = str(getattr(client, "base_url", "https://integrate.api.nvidia.com/v1"))
    print(f"\n[model] {MODEL} via {base_url} (session {session_id})\n", file=sys.stderr)

    tools = _tools()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]

    message: Optional[str] = prompt
    while message is not None:
        if message != prompt:
            messages.append({"role": "user", "content": message})

        turn = 0
        while turn < max_turns:
            turn += 1
            content_text = ""
            tool_calls_map: dict[int, dict] = {}

            stream = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=1,
                top_p=0.95,
                max_tokens=16384,
                parallel_tool_calls=True,
                seed=42,
                extra_body={"chat_template_kwargs": {"enable_thinking": True}},
                stream=True,
                tools=tools,
                tool_choice="auto",
            )

            for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    if on_reasoning is not None:
                        on_reasoning(reasoning)
                    else:
                        print(reasoning, end="", flush=True)

                if delta.content is not None:
                    content_text += delta.content
                    on_text(delta.content)

                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name if (tc.function and tc.function.name) else "",
                                "arguments": tc.function.arguments if (tc.function and tc.function.arguments) else "",
                            }
                        else:
                            if tc.id:
                                tool_calls_map[idx]["id"] += tc.id
                            if tc.function and tc.function.name:
                                tool_calls_map[idx]["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += tc.function.arguments

            if tool_calls_map:
                tool_calls = []
                for idx in sorted(tool_calls_map.keys()):
                    tc_info = tool_calls_map[idx]
                    tool_calls.append(
                        {
                            "id": tc_info["id"] or f"call_{idx}_{secrets.token_hex(3)}",
                            "type": "function",
                            "function": {
                                "name": tc_info["name"],
                                "arguments": tc_info["arguments"],
                            },
                        }
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": content_text if content_text else None,
                        "tool_calls": tool_calls,
                    }
                )

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except Exception:
                        args = {}
                    on_tool(fn_name, args)
                    result_text, is_error = toolbox.call(fn_name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_text,
                        }
                    )
            else:
                on_text("\n")
                break

        message = follow_up() if follow_up else None

    return session_id
