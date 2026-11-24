"""TravelGuardian Streamlit Web UI.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st
from openai import OpenAI

from travelguardian.agent import MODEL, SYSTEM, _tools
from travelguardian.airlabs_provider import AirLabsProvider
from travelguardian.cli import _client
from travelguardian.ingest import extract, merge, read_inbox, to_bookings
from travelguardian.providers import SimulatedProvider
from travelguardian.store import SAMPLES, Store
from travelguardian.tools import ToolBox, decide, defer_all

st.set_page_config(
    page_title="TravelGuardian · AI Travel Assistant",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for modern styling
st.markdown(
    """
    <style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-title {
        color: #6c757d;
        font-size: 1rem;
        margin-bottom: 1.5rem;
    }
    .card {
        background-color: var(--secondary-background-color);
        padding: 1rem 1.2rem;
        border-radius: 0.6rem;
        margin-bottom: 0.8rem;
        border-left: 4px solid #4a90e2;
    }
    .card-flight { border-left-color: #3b82f6; }
    .card-hotel { border-left-color: #10b981; }
    .card-event { border-left-color: #8b5cf6; }
    .badge-delayed {
        background-color: #ef4444;
        color: white;
        padding: 0.2rem 0.5rem;
        border-radius: 0.3rem;
        font-weight: bold;
        font-size: 0.8rem;
    }
    .badge-ontime {
        background-color: #10b981;
        color: white;
        padding: 0.2rem 0.5rem;
        border-radius: 0.3rem;
        font-weight: bold;
        font-size: 0.8rem;
    }
    .badge-pending {
        background-color: #f59e0b;
        color: white;
        padding: 0.2rem 0.5rem;
        border-radius: 0.3rem;
        font-weight: bold;
        font-size: 0.8rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_store() -> Store:
    return Store()


def get_provider(store: Store):
    use_live = st.session_state.get("use_live_airlabs", False) and bool(os.getenv("AIRLABS_API_KEY"))
    if use_live:
        return AirLabsProvider(
            store.scenario_path,
            ttl_seconds=300,
            allow_live_date_mismatch=st.session_state.get("allow_live_date_mismatch", True),
        )
    return SimulatedProvider(store.scenario_path)


def get_toolbox(store: Store, interactive: bool = False) -> ToolBox:
    prov = get_provider(store)
    return ToolBox(store, prov, approver=defer_all, now=prov.now)


# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []


# --- Sidebar ---
with st.sidebar:
    st.title("🛡️ TravelGuardian")
    st.caption(f"Powered by **NVIDIA Nemotron 3.5 Super VL**")

    # AirLabs Live Toggle
    has_airlabs = bool(os.getenv("AIRLABS_API_KEY"))
    st.toggle(
        "🌐 Live AirLabs Feed",
        key="use_live_airlabs",
        disabled=not has_airlabs,
        help="Fetch live flight telemetry and schedules from AirLabs v9 (requires AIRLABS_API_KEY).",
    )
    if not has_airlabs:
        st.caption("ℹ️ Set `AIRLABS_API_KEY` for live feed")

    st.markdown("---")
    st.subheader("⚡ Quick Demo")
    if st.button("🚨 Simulate UA1542 Delay (110m)", use_container_width=True, type="primary"):
        store = get_store()
        if not store.scenario_path.exists():
            store.reset()
        prov = get_provider(store)
        prov.set_disruption("UA1542", "2026-12-03", delay_minutes=110, reason="Late-arriving aircraft")
        st.session_state.trigger_agent_prompt = (
            "Check my flights. If anything is disrupted, handle it: check my connection, find options, "
            "show me the revised itinerary and ask me before changing anything."
        )
        st.success("UA1542 delayed by 110 min! Switched to Assistant tab to resolve.")
        st.rerun()

    st.markdown("---")
    st.subheader("🎮 Simulator Controls")
    store = get_store()
    if store.scenario_path.exists():
        trip = store.load()
        flight_nums = [f.flight_number for f in trip.flights] if trip.flights else ["UA1542"]
        selected_flight = st.selectbox("Select Flight", flight_nums)
        delay_mins = st.slider("Delay Minutes", min_value=0, max_value=240, value=60, step=10)
        delay_reason = st.text_input("Reason", "Late-arriving aircraft")

        col_sim1, col_sim2 = st.columns(2)
        with col_sim1:
            if st.button("Apply Delay", use_container_width=True):
                prov = get_provider(store)
                f = next((x for x in trip.flights if x.flight_number == selected_flight), None)
                if f:
                    prov.set_disruption(f.flight_number, f.departs.date().isoformat(), delay_minutes=delay_mins, reason=delay_reason)
                    st.success(f"Delayed {selected_flight} by {delay_mins}m")
                    st.rerun()
        with col_sim2:
            if st.button("Clear All", use_container_width=True):
                prov = get_provider(store)
                prov.clear_disruptions()
                st.info("Cleared all disruptions.")
                st.rerun()

    st.markdown("---")
    st.subheader("⚙️ Data Management")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        if st.button("Reset Sample", use_container_width=True):
            store = get_store()
            store.reset()
            st.success("Sample trip loaded.")
            st.rerun()
    with col_d2:
        if st.button("Clear Data", use_container_width=True):
            store = get_store()
            store.reset(with_bookings=False)
            st.warning("Data cleared.")
            st.rerun()

# --- Main Page Header ---
st.markdown('<div class="main-title">TravelGuardian Dashboard</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Watches your flights, detects connection risks, proposes alternatives, and executes rebookings only with your approval.</div>', unsafe_allow_html=True)

store = get_store()
if not store.scenario_path.exists():
    store.reset()

tb = get_toolbox(store)
itinerary_data = json.loads(tb.call("get_itinerary", {})[0])
flight_status_data = json.loads(tb.call("check_flight_statuses", {})[0])

# Tabs
tab_itin, tab_agent, tab_drafts, tab_ingest = st.tabs([
    "📅 Itinerary & Conflicts",
    "🤖 AI Travel Assistant",
    "📝 Drafts",
    "📥 Ingest Emails",
])

# ==========================================
# TAB 1: ITINERARY & CONFLICTS
# ==========================================
with tab_itin:
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("Trip Itinerary")

        current_day = None
        for item in itinerary_data.get("items", []):
            item_date = item["at"][:10]
            if item_date != current_day:
                current_day = item_date
                st.markdown(f"#### 🗓️ {current_day}")

            itype = item.get("type", "flight")
            card_class = f"card card-{itype}"
            icon = {"flight": "✈️", "hotel": "🏨", "event": "💼"}.get(itype, "📍")

            status_badge = ""
            if item.get("status"):
                status_badge = f'<span class="badge-delayed">{item["status"]}</span>'

            st.markdown(
                f"""
                <div class="{card_class}">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong>{icon} {item['title']}</strong>
                        {status_badge}
                    </div>
                    <div style="font-size: 0.9rem; color: #4b5563; margin-top: 0.3rem;">
                        {item.get('detail', '')}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with col_right:
        # Pending Approvals Card
        pending = itinerary_data.get("pending_changes", [])
        if pending:
            st.subheader("🔔 Approvals Needed")
            for p in pending:
                with st.container(border=True):
                    st.markdown(f"**Proposal ID:** `{p['id']}`")
                    st.markdown(f"**Action:** Replace segments `{', '.join(p['segment_ids'])}` with **{p['alternative_id']}**")
                    st.markdown(f"**Reason:** {p['reason']}")
                    
                    c_app, c_dec = st.columns(2)
                    with c_app:
                        if st.button("✅ Approve", key=f"app_{p['id']}", type="primary", use_container_width=True):
                            prov = get_provider(store)
                            res = decide(store, prov, p["id"], approved=True, decided_by="traveler (web)", now=prov.now())
                            st.success(f"Change applied: {res.get('message', 'Approved')}")
                            st.rerun()
                    with c_dec:
                        if st.button("❌ Decline", key=f"dec_{p['id']}", use_container_width=True):
                            prov = get_provider(store)
                            res = decide(store, prov, p["id"], approved=False, decided_by="traveler (web)", now=prov.now())
                            st.info(f"Change declined.")
                            st.rerun()

        # Conflicts Card
        st.subheader("⚠️ Detected Conflicts")
        conflicts = itinerary_data.get("conflicts", [])
        if not conflicts:
            st.success("✅ No conflicts detected!")
        else:
            for conf in conflicts:
                sev = conf.get("severity", "info")
                msg = conf.get("message", "")
                if sev == "critical":
                    st.error(f"**CRITICAL:** {msg}")
                elif sev == "warning":
                    st.warning(f"**WARNING:** {msg}")
                else:
                    st.info(f"**INFO:** {msg}")

        # Live Flight Statuses
        st.subheader("📡 Live Flight Status")
        for sid, s in flight_status_data.get("statuses", {}).items():
            f_num = s["flight_number"]
            f_stat = s["status"]
            delay = s["delay_minutes"]
            reason = s.get("reason")
            
            with st.container(border=True):
                c1, c2 = st.columns([2, 1])
                c1.markdown(f"**{f_num}**")
                if f_stat == "on_time":
                    c2.markdown('<span class="badge-ontime">ON TIME</span>', unsafe_allow_html=True)
                else:
                    c2.markdown(f'<span class="badge-delayed">+{delay}m {f_stat.upper()}</span>', unsafe_allow_html=True)
                if reason:
                    st.caption(f"Reason: {reason}")


# ==========================================
# TAB 2: AI TRAVEL ASSISTANT
# ==========================================
with tab_agent:
    st.subheader("🤖 AI Travel Assistant (NVIDIA Nemotron 3.5 Super VL)")
    st.caption("Ask questions or let TravelGuardian analyze flight delays, evaluate MCT & calendar impacts, and prepare rebookings.")

    # Show previous messages
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            if "reasoning" in msg and msg["reasoning"]:
                with st.expander("💭 Thought Process / Reasoning", expanded=False):
                    st.markdown(f"```\n{msg['reasoning']}\n```")
            st.markdown(msg["content"])
            if "tool_calls" in msg and msg["tool_calls"]:
                with st.expander("🛠️ Tool Executions", expanded=False):
                    for tc in msg["tool_calls"]:
                        st.markdown(f"- **{tc['name']}**: `{tc.get('args', {})}`")

    # Check if a prompt was triggered programmatically (e.g. from demo button)
    user_prompt = None
    if "trigger_agent_prompt" in st.session_state and st.session_state.trigger_agent_prompt:
        user_prompt = st.session_state.trigger_agent_prompt
        st.session_state.trigger_agent_prompt = None
    else:
        user_prompt = st.chat_input("Ask TravelGuardian anything about your trip...")

    if user_prompt:
        # Display user message
        st.session_state.chat_history.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        # Assistant turn
        with st.chat_message("assistant"):
            status_box = st.status("TravelGuardian is thinking and coordinating tools...", expanded=True)
            reasoning_area = st.empty()
            content_area = st.empty()

            client = _client()
            toolbox = get_toolbox(store)
            tools = _tools()

            messages = [
                {"role": "system", "content": SYSTEM},
            ]
            # Include recent chat history
            for h in st.session_state.chat_history:
                messages.append({"role": h["role"], "content": h["content"]})

            executed_tools = []
            full_reasoning = ""
            full_content = ""

            turn = 0
            max_turns = 10
            while turn < max_turns:
                turn += 1
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

                turn_content = ""
                tool_calls_map = {}

                for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta

                    r = getattr(delta, "reasoning_content", None)
                    if r:
                        full_reasoning += r
                        status_box.update(label="Analyzing trip data & reasoning...")

                    if delta.content is not None:
                        turn_content += delta.content
                        full_content += delta.content
                        content_area.markdown(full_content)

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
                                "id": tc_info["id"] or f"call_{idx}",
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
                            "content": turn_content if turn_content else None,
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
                        status_box.write(f"⚙️ Running tool: `{fn_name}`({args})")
                        executed_tools.append({"name": fn_name, "args": args})
                        result_text, is_err = toolbox.call(fn_name, args)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result_text,
                            }
                        )
                else:
                    break

            status_box.update(label="Complete", state="complete", expanded=False)
            if full_reasoning:
                with st.expander("💭 Thought Process / Reasoning", expanded=False):
                    st.markdown(f"```\n{full_reasoning}\n```")

            st.session_state.chat_history.append({
                "role": "assistant",
                "content": full_content,
                "reasoning": full_reasoning,
                "tool_calls": executed_tools,
            })
            st.rerun()


# ==========================================
# TAB 3: DRAFTS
# ==========================================
with tab_drafts:
    st.subheader("📝 Draft Messages (Ready to Review & Send)")
    st.caption("TravelGuardian automatically creates draft emails for hotels, airlines, or client meeting organizers, but NEVER sends them without your review.")

    trip = store.load()
    drafts_list = trip.drafts
    if not drafts_list:
        st.info("No drafts currently waiting. When a disruption occurs, the agent will prepare notices here.")
    else:
        for idx, d in enumerate(drafts_list):
            with st.container(border=True):
                st.markdown(f"### ✉️ {d.subject}")
                st.markdown(f"**To:** `{d.to}` | **Type:** `{d.kind}`")
                st.text_area("Body", value=d.body, height=140, key=f"draft_{idx}")


# ==========================================
# TAB 4: INGEST EMAILS
# ==========================================
with tab_ingest:
    st.subheader("📥 Ingest Confirmation Emails")
    st.caption("Extract flight and hotel bookings directly from confirmation emails using Nemotron Structured Outputs.")

    inbox_files = sorted(Path(SAMPLES / "inbox").glob("*.eml"))
    st.markdown(f"Found **{len(inbox_files)}** email confirmations in `samples/inbox`:")

    col_i1, col_i2 = st.columns([1, 1])
    with col_i1:
        if st.button("🚀 Ingest All Sample Emails", type="primary", use_container_width=True):
            with st.spinner("Extracting bookings with NVIDIA Nemotron..."):
                client = _client()
                trip = store.load()
                for msg in read_inbox(SAMPLES / "inbox"):
                    x = extract(client, msg)
                    if not x.is_travel_booking:
                        st.write(f"⏩ Skipped promotional email: *{msg.subject}*")
                        continue
                    flights, hotels = to_bookings(x, msg.id)
                    added, updated = merge(trip, flights, hotels)
                    st.write(f"✅ Read *{msg.subject}*: {len(flights)} flight(s), {len(hotels)} hotel(s)")
                store.save(trip)
                st.success("Ingestion complete! Check the Itinerary tab.")
                st.rerun()

    st.markdown("---")
    st.markdown("#### Sample Email Previews")
    for eml in inbox_files:
        with st.expander(f"📄 {eml.name}"):
            st.code(eml.read_text(encoding="utf-8", errors="ignore")[:600] + "...", language="text")
