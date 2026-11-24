/**
 * Live Trip Console: drives the real backend (sync, delay injection, risk check, calls, proposals)
 * and shows what is stored, independent of the scripted demo visuals below it.
 */

const consoleState = { flights: [] };

function consoleLog(lines) {
    const el = document.getElementById('consoleLog');
    if (!el) return;
    el.classList.remove('hidden');
    el.textContent = (Array.isArray(lines) ? lines : [lines]).join('\n');
}

function consoleBusy(busy) {
    document.querySelectorAll('.console-btn').forEach(b => {
        b.disabled = busy;
        b.classList.toggle('opacity-50', busy);
    });
}

function detailOf(r) {
    return (r && r.data && (r.data.detail || r.data.error)) || `HTTP ${r ? r.status : '?'}`;
}

async function consolePost(path, body) {
    return window.TG_API._requestDetailed(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
}

function chip(label, value, live) {
    const cls = live ? 'bg-emeraldBg text-emerald border-emerald/30' : 'bg-parchment text-mutedText border-slateBorder';
    return `<span class="px-2 py-0.5 rounded-full border ${cls}">${escapeHtml(label)}: <strong>${escapeHtml(value)}</strong></span>`;
}

function renderIntegrations(cfg) {
    const el = document.getElementById('integrationChips');
    if (!el) return;
    if (!cfg) {
        el.innerHTML = chip('Backend', 'not connected (run python server.py)', false);
        return;
    }
    el.innerHTML = [
        chip('Gmail', cfg.mail === 'gmail' ? 'live' : 'sample inbox', cfg.mail === 'gmail'),
        chip('Calendar', cfg.calendar === 'google' ? 'Google' : 'local file', cfg.calendar === 'google'),
        chip('Flight status', cfg.flight_status, cfg.flight_status !== 'simulated'),
        chip('Voice', cfg.voice === 'elevenlabs' ? (cfg.traveler_phone_set ? 'ElevenLabs' : 'ElevenLabs (no TRAVELER_PHONE)') : 'not configured', cfg.voice === 'elevenlabs' && cfg.traveler_phone_set),
        chip('Nemotron', cfg.llm, cfg.llm === 'nemotron'),
        chip('Approval token', cfg.approval_token_set ? 'set' : 'missing', cfg.approval_token_set),
        chip('Webhook secret', cfg.webhook_secret_set ? 'set' : 'missing', cfg.webhook_secret_set),
    ].join('');
    if (cfg.notes && cfg.notes.length) consoleLog(cfg.notes);
}

function renderItinerary(itin) {
    const listEl = document.getElementById('consoleItinerary');
    const confEl = document.getElementById('consoleConflicts');
    const nameEl = document.getElementById('consoleTraveler');
    const select = document.getElementById('delayFlight');
    if (!listEl) return;

    const flights = (itin && itin.flights) || [];
    consoleState.flights = flights;
    nameEl.textContent = itin && itin.traveler ? `${itin.traveler}'s trip` : 'Your trip';

    const keep = select.value;
    select.innerHTML = flights.length
        ? flights.map(f => `<option value="${escapeHtml(f.flight_number)}">${escapeHtml(f.flight_number)} ${escapeHtml(f.origin)}→${escapeHtml(f.destination)} · ${escapeHtml(f.departs.slice(0, 10))}</option>`).join('')
        : '<option value="">No flights yet: sync your inbox</option>';
    if (keep && flights.some(f => f.flight_number === keep)) select.value = keep;

    const items = (itin && itin.items) || [];
    const icon = { flight: '✈', hotel: '🏨', event: '📅' };
    listEl.innerHTML = items.length
        ? items.map(i => `
            <div class="bg-alabaster rounded-lg border ${i.status ? 'border-crimson/40' : 'border-slateBorder'} px-3 py-2">
                <div class="flex items-center justify-between gap-2">
                    <span class="font-semibold text-ink">${icon[i.type] || '•'} ${escapeHtml(i.title)}</span>
                    ${i.status ? `<span class="text-[10px] font-mono font-bold text-crimson">${escapeHtml(i.status.split(':')[0])}</span>` : ''}
                </div>
                <div class="text-[11px] text-mutedText">${escapeHtml(i.status || i.detail)}</div>
            </div>`).join('')
        : '<p class="text-mutedText">No bookings yet. Click "Sync Gmail → trip → Calendar", or "Load sample trip".</p>';

    const conflicts = (itin && itin.conflicts) || [];
    const tone = { critical: 'text-crimson', warning: 'text-amberAlert', info: 'text-mutedText' };
    confEl.innerHTML = conflicts.length
        ? `<div class="text-xs font-mono text-mutedText uppercase tracking-wider mb-1">CONFLICTS (risk engine)</div>` +
          conflicts.map(c => `<div class="${tone[c.severity] || ''}"><strong>${escapeHtml(c.severity.toUpperCase())}</strong> · ${escapeHtml(c.message)}</div>`).join('')
        : (items.length ? '<div class="text-emerald">No conflicts detected.</div>' : '');
}

function renderProposals(list) {
    const el = document.getElementById('consoleProposals');
    if (!el) return;
    if (!list || !list.length) {
        el.innerHTML = '<p class="text-mutedText">None yet. Inject a delay, then run the risk check.</p>';
        return;
    }
    const tone = {
        pending: 'bg-brass/10 text-brass border-brass/30',
        applied: 'bg-emeraldBg text-emerald border-emerald/30',
        approved: 'bg-emeraldBg text-emerald border-emerald/30',
        declined: 'bg-crimsonBg text-crimson border-crimson/30',
        failed: 'bg-crimsonBg text-crimson border-crimson/30'
    };
    el.innerHTML = list.map(p => {
        const label = p.status === 'applied' ? 'APPROVED' : p.status.toUpperCase();
        const call = p.call ? `Call: ${escapeHtml(p.call.status)}` : 'No call yet';
        const heard = (p.transcript || []).length ? ` · transcript ${p.transcript.length} turns` : '';
        return `
            <div class="bg-alabaster rounded-lg border border-slateBorder px-3 py-2 space-y-1">
                <div class="flex items-center justify-between gap-2">
                    <span class="font-mono font-bold text-ink">${escapeHtml(p.id)}</span>
                    <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border ${tone[p.status] || tone.pending}">${label}</span>
                </div>
                <div class="text-[11px] text-ink">${escapeHtml(p.summary || p.reason)}</div>
                <div class="flex items-center justify-between gap-2">
                    <span class="text-[10px] font-mono text-mutedText">${call}${heard}</span>
                    <span class="flex gap-1.5">
                        ${p.status === 'pending' ? `<button onclick="consoleCall('${escapeHtml(p.id)}')" class="px-2 py-1 rounded text-[10px] font-bold text-white bg-emerald hover:bg-emerald/90">Call</button>` : ''}
                        <button onclick="openProposal('${escapeHtml(p.id)}')" class="px-2 py-1 rounded text-[10px] font-bold text-ink border border-slateBorder hover:border-brass">Open</button>
                    </span>
                </div>
            </div>`;
    }).join('');
}

async function refreshConsole() {
    const cfg = await window.TG_API.getConfig();
    if (!cfg.ok) {
        renderIntegrations(null);
        return;
    }
    renderIntegrations(cfg.data);
    const [itin, props] = await Promise.all([
        window.TG_API._requestDetailed('/api/itinerary'),
        window.TG_API._requestDetailed('/api/proposals')
    ]);
    if (itin.ok) renderItinerary(itin.data);
    if (props.ok) renderProposals(props.data.proposals);
}

async function consoleSync() {
    consoleBusy(true);
    consoleLog('Reading confirmation emails and extracting bookings with Nemotron… (can take a minute)');
    const r = await consolePost('/api/sync');
    consoleBusy(false);
    if (!r.ok) return consoleLog(`Sync failed: ${detailOf(r)}`);
    const d = r.data;
    consoleLog([
        `Inbox: ${d.mail_provider} · Calendar: ${d.calendar_provider}`,
        ...d.processed.map(p => `  ${p.status.padEnd(24)} ${p.subject}`),
        `${d.flight_events} flight event(s) in the calendar · ${d.meetings_imported} meeting(s) imported`,
        ...(d.notes || [])
    ]);
    refreshConsole();
}

async function consoleReset(empty) {
    const what = empty ? 'an EMPTY trip (then sync your Gmail)' : 'the SAMPLE trip';
    if (!confirm(`Replace the current trip with ${what}? Proposals and history are cleared.`)) return;
    const r = await consolePost('/api/reset', { empty });
    if (!r.ok) return consoleLog(`Reset failed: ${detailOf(r)}`);
    await resetAllState();
    consoleLog(empty ? 'Empty trip. Next: Sync Gmail → trip → Calendar.' : 'Sample trip loaded (Maya Rao, SFO → ORD → LHR).');
    refreshConsole();
}

async function consoleInjectDelay() {
    const flight = document.getElementById('delayFlight').value;
    const delay = parseInt(document.getElementById('delayMinutes').value || '0', 10);
    if (!flight) return consoleLog('No flight to delay yet: sync your inbox or load the sample trip.');
    const r = await consolePost('/api/simulate-delay', { flight, delay, reason: 'Delay injected for demo' });
    if (!r.ok) return consoleLog(`Could not inject delay: ${detailOf(r)}`);
    consoleLog(`${flight} now delayed ${delay} min (simulated). Next: Run risk check.`);
    refreshConsole();
}

async function consoleClearDelays() {
    const r = await consolePost('/api/clear-disruptions');
    consoleLog(r.ok ? 'All injected delays cleared.' : `Failed: ${detailOf(r)}`);
    refreshConsole();
}

async function consoleEvaluate(call) {
    consoleBusy(true);
    consoleLog(call ? 'Running risk check, then calling the traveler…' : 'Running risk check…');
    const r = await window.TG_API.evaluate(call);
    consoleBusy(false);
    if (!r.ok) return consoleLog(`Risk check failed: ${detailOf(r)}`);
    const d = r.data;
    const lines = d.connections.map(c => `  ${c.airport}: ${c.layover_minutes} min to connect vs ${c.minimum_connection_minutes} min MCT → ${c.risk.toUpperCase()}`);
    if (!lines.length) lines.push('  No connecting flights in this trip.');
    if (d.created.length) lines.push(`New proposal: ${d.created.join(', ')}`);
    if (d.existing.length) lines.push(`Already open: ${d.existing.join(', ')}`);
    (d.calls || []).forEach(c => lines.push(c.error ? `Call not placed: ${c.error}` : `Calling ${c.to_number} (${c.conversation_id})`));
    if (!d.created.length && !d.existing.length) lines.push('Nothing at risk, so no proposal.');
    consoleLog(lines);
    await refreshConsole();
    const pid = d.created[0] || d.existing[0];
    if (pid) {
        await openProposal(pid);
        if (call) startPolling(pid);
    }
}

async function openProposal(id) {
    stopPolling();
    // Clear any stamp/buttons left from a previous proposal before showing this one.
    const stampOverlay = document.getElementById('approvalStampContainer');
    const buttons = document.getElementById('decisionButtonsContainer');
    if (stampOverlay) stampOverlay.classList.add('hidden');
    if (buttons) buttons.classList.remove('hidden');
    ['btnApprove', 'btnDecline'].forEach(b => { const el = document.getElementById(b); if (el) el.disabled = false; });
    state.isDeciding = false;
    state.activeProposalId = id;
    state.status = 'proposed';
    renderState();
    const p = await loadProposal(id);
    if (p) selectProposalOption(p);
    const panel = document.getElementById('resolutionPanel');
    if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (p && p.call && !['done', 'failed'].includes(p.call.status)) startPolling(id);
}

async function consoleCall(id) {
    await openProposal(id);
    await startVoiceCall();
    refreshConsole();
}

window.addEventListener('DOMContentLoaded', () => {
    window.TG_API.ready.then(refreshConsole);
});
