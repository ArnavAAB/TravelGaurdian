/**
 * TravelGuardian Application Controller
 * Orchestrates UI state, Three.js 3D animations, tool trace executions,
 * approval stamping, and slide-over drawers.
 */

const state = {
    status: 'ontime', // 'ontime' | 'delayed' | 'resolving' | 'proposed' | 'approved' | 'declined'
    selectedOption: 'UA938',
    activeProposalId: null,
    isDeciding: false,
    drafts: [],
    activeDrawer: null,
    viewMode: '3d', // '3d' | '2d'
    proposal: null, // latest proposal from the backend (TG-402 ...)
    pollTimer: null
};

// Initializer
window.addEventListener('DOMContentLoaded', () => {
    // Initialize 3D Visualizer
    if (window.FlightVisualizer3D) {
        window.visualizer3D = new FlightVisualizer3D('threeCanvasContainer');
    }

    renderState();
    loadBackendData();
});

// Load live data from backend if available
async function loadBackendData() {
    const itinerary = await window.TG_API.getItinerary();
    if (itinerary && itinerary.pending_changes && itinerary.pending_changes.length > 0) {
        state.activeProposalId = itinerary.pending_changes[0].id;
        const badge = document.getElementById('proposalBadge');
        if (badge) badge.textContent = `PROPOSAL #${state.activeProposalId}`;
        loadProposal(state.activeProposalId);
    }
    const drafts = await window.TG_API.getDrafts();
    if (drafts && drafts.length > 0) {
        state.drafts = drafts;
        renderDrafts();
    }
}

// Main Action Button Handler
function handleMainAction() {
    if (state.status === 'ontime') {
        triggerDelayScenario();
    } else if (state.status === 'delayed') {
        runAgentResolution();
    } else if (state.status === 'approved' || state.status === 'declined') {
        resetAllState();
    }
}

// 1. Trigger Delay Simulation (+110m)
async function triggerDelayScenario() {
    state.status = 'delayed';
    await window.TG_API.triggerDelay('UA1542', 110, 'Late-arriving aircraft');
    
    if (window.visualizer3D) {
        window.visualizer3D.setState('delayed', state.selectedOption);
    }
    renderState();
}

// 2. Run Autonomous Agent Tool Trace & Alternatives Evaluation
async function runAgentResolution() {
    state.status = 'resolving';
    if (window.visualizer3D) {
        window.visualizer3D.setState('resolving', state.selectedOption);
    }
    renderState();

    const traceContainer = document.getElementById('toolTraceList');
    traceContainer.innerHTML = '';

    // Run the deterministic risk engine on the backend: it creates the rebooking proposal (e.g. TG-402)
    const apiPromise = window.TG_API.evaluate(false);

    const steps = [
        { fn: 'check_flight_statuses', detail: 'UA1542 +110m delay' },
        { fn: 'assess_connection', detail: 'ORD: 45m buffer < 90m MCT' },
        { fn: 'search_alternatives', detail: '4 options evaluated' },
        { fn: 'estimate_ground_transfer', detail: '105m to Fenchurch St' },
        { fn: 'preview_change', detail: 'Arrives 07:25 GMT (Dec 4)' },
        { fn: 'request_booking_change', detail: 'Proposal pending approval' }
    ];

    // Animate tool steps sequentially with a tactile cadence
    steps.forEach((step, idx) => {
        setTimeout(async () => {
            const item = document.createElement('div');
            item.className = 'bg-parchment p-2.5 rounded-lg border border-slateBorder flex items-center justify-between shadow-xs transition-all duration-200';
            item.innerHTML = `
                <div class="truncate flex items-center space-x-1.5">
                    <span class="text-brass font-bold text-xs">✓</span>
                    <span class="text-ink font-semibold font-mono text-xs">${step.fn}</span>
                </div>
                <span class="text-[10px] text-mutedText bg-alabaster px-1.5 py-0.5 rounded border border-slateBorder font-mono">${step.detail}</span>
            `;
            traceContainer.appendChild(item);

            if (idx === steps.length - 1) {
                const apiRes = await apiPromise;
                const result = apiRes && apiRes.ok ? apiRes.data : null;
                const pid = result && ((result.created || [])[0] || (result.existing || [])[0]);
                if (pid) {
                    state.activeProposalId = pid;
                    const badge = document.getElementById('proposalBadge');
                    if (badge) badge.textContent = `PROPOSAL #${pid}`;
                    loadProposal(pid);
                }

                setTimeout(() => {
                    state.status = 'proposed';
                    if (window.visualizer3D) {
                        window.visualizer3D.setState('proposed', state.selectedOption);
                    }
                    renderState();
                    if (state.proposal) selectProposalOption(state.proposal); else selectOption('UA938');
                }, 400);
            }
        }, idx * 380);
    });
}

// Helper: Northwind Client Kickoff badge and buffer synchronization
function updateKickoffBadge(status, selectedOption) {
    const kickoffBadge = document.getElementById('badgeKickoff');
    const kickoffBuffer = document.getElementById('txtKickoffBuffer');
    if (!kickoffBadge || !kickoffBuffer) return;

    if (status === 'ontime') {
        // Baseline on-time: lands at 06:05 GMT, 3h 55m buffer to 10:00 GMT kickoff
        kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
        kickoffBadge.textContent = 'REACHABLE';
        kickoffBuffer.className = 'font-mono text-[11px] text-emerald font-semibold';
        kickoffBuffer.textContent = '3h 55m buffer';
    } else if (status === 'delayed' || status === 'resolving') {
        // Pre-approval disrupted state: connection missed at ORD, kickoff is at severe risk!
        kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
        kickoffBadge.textContent = 'CONFLICT';
        kickoffBuffer.className = 'font-mono text-[11px] text-crimson font-semibold';
        kickoffBuffer.textContent = 'Misses Kickoff (ORD Connection Missed)';
    } else if (status === 'proposed') {
        // Option evaluation & preview
        if (selectedOption === 'UA938') {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
            kickoffBadge.textContent = 'REACHABLE';
            kickoffBuffer.className = 'font-mono text-[11px] text-emerald font-semibold';
            kickoffBuffer.textContent = '2h 35m buffer';
        } else if (selectedOption === 'BA296') {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
            kickoffBadge.textContent = 'REACHABLE';
            kickoffBuffer.className = 'font-mono text-[11px] text-emerald font-semibold';
            kickoffBuffer.textContent = '2h 20m buffer';
        } else {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
            kickoffBadge.textContent = 'CONFLICT';
            kickoffBuffer.className = 'font-mono text-[11px] text-crimson font-semibold';
            kickoffBuffer.textContent = 'Misses Kickoff';
        }
    } else if (status === 'approved') {
        // Post-approval rebooked state: UA938 arrives at 07:25 GMT, 2h 35m buffer to 10:00 kickoff!
        if (selectedOption === 'UA938') {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
            kickoffBadge.textContent = 'REACHABLE';
            kickoffBuffer.className = 'font-mono text-[11px] text-emerald font-semibold';
            kickoffBuffer.textContent = '2h 35m buffer (Lands 07:25 GMT)';
        } else if (selectedOption === 'BA296') {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
            kickoffBadge.textContent = 'REACHABLE';
            kickoffBuffer.className = 'font-mono text-[11px] text-emerald font-semibold';
            kickoffBuffer.textContent = '2h 20m buffer (Lands 07:40 GMT)';
        } else {
            kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
            kickoffBadge.textContent = 'CONFLICT';
            kickoffBuffer.className = 'font-mono text-[11px] text-crimson font-semibold';
            kickoffBuffer.textContent = 'Misses Kickoff';
        }
    } else if (status === 'declined') {
        // Rebooking declined: flight remains disrupted and connection missed
        kickoffBadge.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
        kickoffBadge.textContent = 'CONFLICT';
        kickoffBuffer.className = 'font-mono text-[11px] text-crimson font-semibold';
        kickoffBuffer.textContent = 'Connection Missed';
    }
}

// 3. Candidate Option Selector
function selectOption(optId) {
    state.selectedOption = optId;
    updateKickoffBadge(state.status, optId);

    ['UA938', 'UA926', 'BA296', 'UA901'].forEach(id => {
        const el = document.getElementById(`opt-${id}`);
        if (el) {
            if (id === optId) {
                el.className = 'cursor-pointer boarding-pass rounded-xl p-4 border-2 border-emerald shadow-md transition-all relative transform -translate-y-0.5';
            } else {
                el.className = 'cursor-pointer boarding-pass rounded-xl p-4 border border-slateBorder hover:border-brass transition-all opacity-85';
            }
        }
    });

    // Update Gate Title Text
    const titleEl = document.getElementById('approvalGateTitle');
    if (optId === 'UA938') {
        titleEl.innerHTML = 'Replace disrupted UA958 with <span class="text-emerald font-mono font-bold">UA938</span> (Complimentary · Arrives 07:25 GMT)';
    } else if (optId === 'UA926') {
        titleEl.innerHTML = 'Replace disrupted UA958 with <span class="text-amberAlert font-mono font-bold">UA926</span> (Complimentary · Arrives 11:10 GMT)';
    } else if (optId === 'BA296') {
        titleEl.innerHTML = 'Replace disrupted UA958 with <span class="text-amberAlert font-mono font-bold">BA296</span> (+$1,240.00 Fare Diff · Arrives 07:40 GMT)';
    } else if (optId === 'UA901') {
        titleEl.innerHTML = 'Replace entire itinerary with Nonstop <span class="text-ink font-mono font-bold">UA901</span> (SFO→LHR · Arrives 10:45 GMT)';
    }

    if (window.visualizer3D) {
        window.visualizer3D.updateOptionHighlight(optId);
    }
}

// Hover Option Preview Effect for Client Kickoff Meeting
function previewOption(optId) {
    // If already finalized (approved or declined), preserve final state
    if (state.status === 'approved' || state.status === 'declined') return;

    updateKickoffBadge('proposed', optId);

    if (window.visualizer3D) {
        window.visualizer3D.updateOptionHighlight(optId);
    }
}

function resetOptionPreview() {
    updateKickoffBadge(state.status, state.selectedOption);
    if (window.visualizer3D) {
        window.visualizer3D.updateOptionHighlight(state.selectedOption);
    }
}

// 4. Decision Approver Handler (Human-in-the-Loop Gate)
async function handleDecision(approved) {
    // Prevent duplicate approval/decline triggers
    if (state.isDeciding) return;
    state.isDeciding = true;

    // Immediately disable and hide decision buttons
    const btnApprove = document.getElementById('btnApprove');
    const btnDecline = document.getElementById('btnDecline');
    const decisionButtonsContainer = document.getElementById('decisionButtonsContainer');

    if (btnApprove) btnApprove.disabled = true;
    if (btnDecline) btnDecline.disabled = true;
    if (decisionButtonsContainer) decisionButtonsContainer.classList.add('hidden');

    const stampOverlay = document.getElementById('approvalStampContainer');
    const stampBadge = document.getElementById('stampBadge');

    if (stampOverlay) stampOverlay.classList.remove('hidden');

    const proposalId = state.activeProposalId || 'TG-402';

    if (approved) {
        if (stampBadge) {
            stampBadge.className = 'px-8 py-3 rounded-md text-2xl font-bold tracking-widest uppercase shadow-2xl stamp-approved';
            stampBadge.textContent = 'APPROVED';
        }
        state.status = 'approved';

        const apiRes = await window.TG_API.approveProposal(proposalId, state.selectedOption);
        if (apiRes && apiRes.error) {
            revertDecision(apiRes.error);
            return;
        }
        if (apiRes && apiRes.result) renderApprovalRecord(apiRes.result);
        if (apiRes && apiRes.drafts && apiRes.drafts.length > 0) {
            state.drafts = apiRes.drafts;
        } else {
            // Hotel Notice Draft (fallback)
            state.drafts = [
                {
                    to: 'reservations@kingswaygrand.example',
                    subject: 'Revised arrival notice - Maya Rao (KGH-558201)',
                    body: `Dear Kingsway Grand Hotel,\n\nDue to flight rebooking (UA938), I will now arrive at London Heathrow (LHR) on Friday, 04 December at 07:25 GMT instead of 06:05 GMT.\n\nPlease hold my room under confirmation KGH-558201. I would like to request early check-in if possible, or arrange to store my luggage until the 15:00 check-in time.\n\nThank you,\nMaya Rao`
                },
                {
                    to: 'reservations@kingswaygrand.example',
                    subject: 'Hotel extension request for Tue 08 Dec (KGH-558201)',
                    body: `Dear Kingsway Grand Hotel,\n\nI noticed my booking currently ends on Tue 08 Dec, but my return flight BA287 departs on Wed 09 Dec. Please check if I can extend my reservation for the night of 08 Dec.\n\nConfirmation: KGH-558201\nGuest: Maya Rao`
                }
            ];
        }
    } else {
        if (stampBadge) {
            stampBadge.className = 'px-8 py-3 rounded-md text-2xl font-bold tracking-widest uppercase shadow-2xl stamp-declined';
            stampBadge.textContent = 'DECLINED';
        }
        state.status = 'declined';

        const apiRes = await window.TG_API.declineProposal(proposalId);
        if (apiRes && apiRes.error) {
            revertDecision(apiRes.error);
            return;
        }
        if (apiRes && apiRes.result) renderApprovalRecord(apiRes.result);
        if (apiRes && apiRes.drafts && apiRes.drafts.length > 0) {
            state.drafts = apiRes.drafts;
        } else {
            // Airline Rebooking Request Draft (fallback)
            state.drafts = [
                {
                    to: 'united.disruptions@united.example',
                    subject: 'Assisted Rebooking Request - PNR K7Q2LM (Delayed UA1542)',
                    body: `To United Customer Service,\n\nMy flight UA1542 (SFO->ORD) has been delayed 110 minutes, reducing my international connection at ORD for UA958 to 45 minutes (below the 90-minute MCT).\n\nI declined the automatic rebooking proposal and request rebooking on your earliest available transatlantic flight to London.\n\nConfirmation: K7Q2LM\nPassenger: Maya Rao`
                }
            ];
        }
    }

    if (window.visualizer3D) {
        window.visualizer3D.setState(state.status, state.selectedOption);
    }

    renderDrafts();
    renderState();
    if (window.refreshConsole) refreshConsole();
}

// 5. State Renderer & DOM Synchronization
function renderState() {
    const mainBtnLbl = document.getElementById('lblMainTrigger');
    const mainBtn = document.getElementById('btnMainTrigger');
    const statusPill = document.getElementById('statusPill');
    const statusPillText = document.getElementById('statusPillText');
    
    const mctGauge = document.getElementById('mctGaugeContainer');
    const resolutionPanel = document.getElementById('resolutionPanel');
    
    const timeOrdArr = document.getElementById('timeOrdArr');
    const badgeLeg1 = document.getElementById('badgeLeg1');
    const badgeLeg2 = document.getElementById('badgeLeg2');
    const txtLeg2FlightNum = document.getElementById('txtLeg2FlightNum');
    const timeOrdDep = document.getElementById('timeOrdDep');
    const timeLhrArr = document.getElementById('timeLhrArr');

    const draftBadge = document.getElementById('draftBadge');
    draftBadge.textContent = state.drafts.length;

    // Synchronize 2D SVG Arc (if visible)
    const arcSfoOrd = document.getElementById('arcSFO_ORD');
    const arcOrdLhr = document.getElementById('arcORD_LHR');
    const planeGroup = document.getElementById('planeGroup');
    const ordPulseRing = document.getElementById('ordPulseRing');
    const ordNodeBg = document.getElementById('ordNodeBg');
    const ordNodeDot = document.getElementById('ordNodeDot');
    const ordMctText = document.getElementById('ordMctText');

    if (state.status === 'ontime') {
        mainBtnLbl.textContent = 'Simulate +110m Delay';
        mainBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg text-xs sm:text-sm font-semibold transition-all duration-200 shadow-sm bg-crimson text-white hover:bg-red-700 active:scale-[0.98]';

        statusPill.className = 'self-start md:self-auto flex items-center space-x-2 px-3.5 py-1.5 rounded-full text-xs font-medium bg-emeraldBg border border-emerald/30 text-emerald';
        statusPillText.textContent = 'ITINERARY ON TIME · ALL CONNECTIONS SECURE';

        if (arcSfoOrd) arcSfoOrd.setAttribute('class', 'path-on-time');
        if (arcOrdLhr) arcOrdLhr.setAttribute('class', 'path-on-time');
        if (planeGroup) planeGroup.setAttribute('transform', 'translate(100, 120) rotate(-20)');

        if (ordPulseRing) ordPulseRing.classList.add('opacity-0');
        if (ordNodeBg) ordNodeBg.setAttribute('stroke', '#B88A3B');
        if (ordNodeDot) ordNodeDot.setAttribute('fill', '#B88A3B');
        if (ordMctText) {
            ordMctText.textContent = 'Connection: 155m';
            ordMctText.setAttribute('class', 'font-mono text-[11px] font-semibold fill-brass');
        }

        mctGauge.classList.add('hidden');
        resolutionPanel.classList.add('hidden');

        timeOrdArr.innerHTML = '13:15 <span class="text-xs font-normal text-mutedText">CST</span>';
        badgeLeg1.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
        badgeLeg1.textContent = 'ON TIME';

        badgeLeg2.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
        badgeLeg2.textContent = 'ON TIME';
        txtLeg2FlightNum.textContent = 'UA958';
        timeOrdDep.innerHTML = '15:50 <span class="text-xs font-normal text-mutedText">CST</span>';
        timeLhrArr.innerHTML = '06:05 <span class="text-xs font-normal text-mutedText">GMT</span>';

    } else if (state.status === 'delayed') {
        mainBtnLbl.textContent = 'Resolve with Agent';
        mainBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg text-xs sm:text-sm font-semibold transition-all duration-200 shadow-sm bg-brass text-white hover:bg-brassHover active:scale-[0.98] animate-bounce';

        statusPill.className = 'self-start md:self-auto flex items-center space-x-2 px-3.5 py-1.5 rounded-full text-xs font-medium bg-crimsonBg border border-crimson/30 text-crimson';
        statusPillText.textContent = 'UA1542 DELAYED +110m · CONNECTION AT HIGH RISK AT ORD';

        if (arcSfoOrd) arcSfoOrd.setAttribute('class', 'path-delayed');
        if (arcOrdLhr) arcOrdLhr.setAttribute('class', 'path-delayed');
        if (planeGroup) planeGroup.setAttribute('transform', 'translate(250, 75) rotate(0)');

        if (ordPulseRing) {
            ordPulseRing.classList.remove('opacity-0');
            ordPulseRing.classList.add('pulse-danger');
        }
        if (ordNodeBg) ordNodeBg.setAttribute('stroke', '#B6382E');
        if (ordNodeDot) ordNodeDot.setAttribute('fill', '#B6382E');
        if (ordMctText) {
            ordMctText.textContent = 'MCT Deficit: 45m < 90m';
            ordMctText.setAttribute('class', 'font-mono text-[11px] font-bold fill-crimson');
        }

        mctGauge.classList.remove('hidden');
        resolutionPanel.classList.add('hidden');

        timeOrdArr.innerHTML = '15:05 <span class="text-xs font-mono font-bold text-crimson">+110m</span>';
        badgeLeg1.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
        badgeLeg1.textContent = 'DELAYED +110m';

        badgeLeg2.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-crimsonBg text-crimson border border-crimson/20';
        badgeLeg2.textContent = 'CONNECTION MISSED';

    } else if (state.status === 'resolving' || state.status === 'proposed') {
        mainBtnLbl.textContent = state.status === 'proposed' ? 'Awaiting Approval' : 'Evaluating Options...';
        resolutionPanel.classList.remove('hidden');
    } else if (state.status === 'approved') {
        mainBtnLbl.textContent = 'Reset Scenario';
        mainBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg text-xs sm:text-sm font-semibold transition-all duration-200 shadow-sm bg-ink text-white hover:bg-slate-800';

        statusPill.className = 'self-start md:self-auto flex items-center space-x-2 px-3.5 py-1.5 rounded-full text-xs font-medium bg-emeraldBg border border-emerald/30 text-emerald';
        statusPillText.textContent = `REBOOKED ON ${state.selectedOption} · ITINERARY SECURED`;

        if (arcSfoOrd) arcSfoOrd.setAttribute('class', 'path-on-time');
        if (arcOrdLhr) arcOrdLhr.setAttribute('class', 'path-rebooked');
        if (planeGroup) planeGroup.setAttribute('transform', 'translate(550, 75) rotate(15)');

        if (ordPulseRing) ordPulseRing.classList.add('opacity-0');
        if (ordNodeBg) ordNodeBg.setAttribute('stroke', '#1E6B4B');
        if (ordNodeDot) ordNodeDot.setAttribute('fill', '#1E6B4B');
        if (ordMctText) {
            ordMctText.textContent = 'Rebooked on UA938';
            ordMctText.setAttribute('class', 'font-mono text-[11px] font-bold fill-emerald');
        }

        badgeLeg2.className = 'text-[10px] font-mono px-2 py-0.5 rounded font-semibold bg-emeraldBg text-emerald border border-emerald/20';
        badgeLeg2.textContent = 'REBOOKED (APPLIED)';
        txtLeg2FlightNum.textContent = state.selectedOption;
        timeOrdDep.innerHTML = '18:00 <span class="text-xs font-normal text-mutedText">CST</span>';
        timeLhrArr.innerHTML = '07:25 <span class="text-xs font-normal text-mutedText">GMT</span>';
    } else if (state.status === 'declined') {
        mainBtnLbl.textContent = 'Reset Scenario';
        mainBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg text-xs sm:text-sm font-semibold transition-all duration-200 shadow-sm bg-ink text-white hover:bg-slate-800';

        statusPill.className = 'self-start md:self-auto flex items-center space-x-2 px-3.5 py-1.5 rounded-full text-xs font-medium bg-crimsonBg border border-crimson/30 text-crimson';
        statusPillText.textContent = 'REBOOKING DECLINED · MANUAL ASSISTANCE DRAFT READY';
    }

    // Synchronize Northwind Client Kickoff badge and buffer
    updateKickoffBadge(state.status, state.selectedOption);
}

// 6. View Mode Toggle (3D Globe vs 2D Schematic)
function toggleViewMode(mode) {
    state.viewMode = mode;
    const btn3d = document.getElementById('btnMode3D');
    const btn2d = document.getElementById('btnMode2D');
    const container3d = document.getElementById('threeCanvasContainer');
    const container2d = document.getElementById('svg2dContainer');

    if (mode === '3d') {
        btn3d.className = 'px-3 py-1 text-xs font-mono font-bold rounded bg-brass text-white';
        btn2d.className = 'px-3 py-1 text-xs font-mono text-mutedText hover:text-ink';
        container3d.classList.remove('hidden');
        container2d.classList.add('hidden');
    } else {
        btn2d.className = 'px-3 py-1 text-xs font-mono font-bold rounded bg-brass text-white';
        btn3d.className = 'px-3 py-1 text-xs font-mono text-mutedText hover:text-ink';
        container3d.classList.add('hidden');
        container2d.classList.remove('hidden');
    }
}

// 7. Drawer Toggle Handler
function toggleDrawer(type) {
    const drawerDrafts = document.getElementById('drawerDrafts');
    const drawerInbox = document.getElementById('drawerInbox');
    
    const bDrafts = document.getElementById('backdropDrafts');
    const pDrafts = document.getElementById('panelDrafts');
    const bInbox = document.getElementById('backdropInbox');
    const pInbox = document.getElementById('panelInbox');

    if (type === 'drafts') {
        if (state.activeDrawer === 'drafts') {
            bDrafts.classList.add('opacity-0');
            pDrafts.classList.add('translate-x-full');
            setTimeout(() => drawerDrafts.classList.add('pointer-events-none'), 350);
            state.activeDrawer = null;
        } else {
            drawerDrafts.classList.remove('pointer-events-none');
            bDrafts.classList.remove('opacity-0');
            pDrafts.classList.remove('translate-x-full');
            state.activeDrawer = 'drafts';
        }
    } else if (type === 'inbox') {
        if (state.activeDrawer === 'inbox') {
            bInbox.classList.add('opacity-0');
            pInbox.classList.add('translate-x-full');
            setTimeout(() => drawerInbox.classList.add('pointer-events-none'), 350);
            state.activeDrawer = null;
        } else {
            drawerInbox.classList.remove('pointer-events-none');
            bInbox.classList.remove('opacity-0');
            pInbox.classList.remove('translate-x-full');
            state.activeDrawer = 'inbox';
        }
    }
}

// 8. Render Draft Cards in Slide-over
function renderDrafts() {
    const list = document.getElementById('draftsList');
    if (state.drafts.length === 0) {
        list.innerHTML = `
            <div class="text-center py-12 text-mutedText text-xs font-mono">
                No drafts prepared yet. Trigger a disruption resolution to generate automated notices.
            </div>
        `;
        return;
    }

    list.innerHTML = state.drafts.map((d, i) => `
        <div class="boarding-pass rounded-xl p-4 space-y-2 border border-slateBorder">
            <div class="flex items-center justify-between text-xs font-mono">
                <span class="text-brass font-bold">DRAFT #${i + 1}</span>
                <button onclick="copyDraftText(${i})" class="text-[11px] text-mutedText hover:text-ink border border-slateBorder px-2 py-0.5 rounded bg-parchment">
                    Copy Text
                </button>
            </div>
            <div class="text-xs font-bold text-ink">To: <span class="font-mono text-mutedText font-normal">${d.to}</span></div>
            <div class="text-xs font-bold text-ink">Subj: <span class="font-sans text-mutedText font-normal">${d.subject}</span></div>
            <textarea id="txtDraft_${i}" rows="5" class="w-full bg-parchment p-3 rounded-lg border border-slateBorder font-mono text-[11px] text-ink focus:outline-none focus:border-brass">${d.body}</textarea>
        </div>
    `).join('');
}

function copyDraftText(idx) {
    const txtArea = document.getElementById(`txtDraft_${idx}`);
    if (txtArea) {
        txtArea.select();
        document.execCommand('copy');
        alert('Draft text copied to clipboard!');
    }
}

// 9. Ingest Animation Trigger
async function triggerIngest() {
    const btn = document.getElementById('btnIngestSample');
    const prog = document.getElementById('ingestProgress');
    btn.disabled = true;
    btn.classList.add('opacity-50');
    prog.classList.remove('hidden');

    const result = await window.TG_API.ingestEmails();
    const processed = (result && result.processed) || null;
    const summary = processed
        ? `Read ${processed.length} email(s) from the ${result.mail_provider} inbox: ` +
          processed.map(p => `${escapeHtml(p.subject)} (${escapeHtml(p.status)})`).join('; ') +
          `. ${result.flight_events} flight event(s) kept in the ${result.calendar_provider} calendar.`
        : 'Successfully parsed confirmation emails: extracted SFO→ORD (UA1542), ORD→LHR (UA958), Kingsway Grand Hotel reservation, and client meeting kickoff.';

    setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove('opacity-50');
        prog.classList.add('hidden');
        
        const chatContainer = document.getElementById('chatMessages');
        const msg = document.createElement('div');
        msg.className = 'bg-emeraldBg p-3 rounded-lg border border-emerald/30 text-ink animate-fade-in';
        msg.innerHTML = `
            <span class="font-mono text-emerald font-bold block mb-1">INGEST COMPLETE (NVIDIA NEMOTRON)</span>
            ${summary}
        `;
        chatContainer.appendChild(msg);
        chatContainer.scrollTop = chatContainer.scrollHeight;
    }, 1200);
}

// 10. Chat Input Handler (Calls Live NVIDIA Nemotron Model via Backend API)
async function handleChatSubmit(e) {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    const txt = input.value.trim();
    if (!txt) return;

    const chatContainer = document.getElementById('chatMessages');
    
    // User Message
    const uMsg = document.createElement('div');
    uMsg.className = 'bg-alabaster p-3 rounded-lg border border-slateBorder text-ink text-right';
    uMsg.innerHTML = `<span class="font-mono text-mutedText font-bold block mb-1">YOU</span>${txt}`;
    chatContainer.appendChild(uMsg);

    input.value = '';
    chatContainer.scrollTop = chatContainer.scrollHeight;

    // Loading indicator
    const loadingMsg = document.createElement('div');
    loadingMsg.className = 'bg-parchment p-3 rounded-lg border border-slateBorder text-ink animate-pulse';
    loadingMsg.innerHTML = `<span class="font-mono text-brass font-bold block mb-1">TRAVELGUARDIAN AI</span>Thinking...`;
    chatContainer.appendChild(loadingMsg);
    chatContainer.scrollTop = chatContainer.scrollHeight;

    // Call live agent API or smart fallback
    const apiResult = await window.TG_API.askAgent(txt);
    loadingMsg.remove();

    let resp = "";
    let reasoning = "";

    if (apiResult && apiResult.reply) {
        resp = apiResult.reply;
        reasoning = apiResult.reasoning || "";
    } else {
        const lower = txt.toLowerCase();
        if (lower.includes('why ua938') || lower.includes('ua938')) {
            resp = "UA938 is recommended because it is a complimentary United rebooking, arrives at LHR at 07:25 GMT, and preserves a 2h 35m buffer before your 10:00 Client Kickoff at 30 Fenchurch St.";
        } else if (lower.includes('hotel') || lower.includes('kingsway') || lower.includes('bloomsbury')) {
            resp = "You are booked at Kingsway Grand Hotel (Dec 4–8). Note: TravelGuardian detected a missing night gap on Dec 8 which will be included in the draft notice.";
        } else if (lower.includes('status')) {
            resp = `Current status: ${state.status.toUpperCase()}. UA1542 is SFO→ORD and UA958/UA938 connects to LHR.`;
        } else {
            resp = "I am actively watching your itinerary against deterministic minimum connection time (MCT) rules and your calendar commitments.";
        }
    }

    const aMsg = document.createElement('div');
    aMsg.className = 'bg-parchment p-3 rounded-lg border border-slateBorder text-ink';
    let reasoningHtml = "";
    if (reasoning) {
        reasoningHtml = `<details class="mb-2"><summary class="text-[10px] text-mutedText cursor-pointer font-mono uppercase">Thinking / Reasoning</summary><p class="text-[11px] text-mutedText mt-1 font-mono">${reasoning.slice(0, 300)}...</p></details>`;
    }
    aMsg.innerHTML = `<span class="font-mono text-brass font-bold block mb-1">TRAVELGUARDIAN AI</span>${reasoningHtml}<div>${resp}</div>`;
    chatContainer.appendChild(aMsg);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// 11. Reset All State
async function resetAllState() {
    state.status = 'ontime';
    state.selectedOption = 'UA938';
    state.activeProposalId = null;
    state.isDeciding = false;
    state.drafts = [];
    stopPolling();
    state.proposal = null;
    renderApprovalRecord(null);
    await window.TG_API.clearDisruptions();

    const stampOverlay = document.getElementById('approvalStampContainer');
    if (stampOverlay) stampOverlay.classList.add('hidden');

    const decisionButtonsContainer = document.getElementById('decisionButtonsContainer');
    if (decisionButtonsContainer) decisionButtonsContainer.classList.remove('hidden');

    const btnApprove = document.getElementById('btnApprove');
    if (btnApprove) btnApprove.disabled = false;
    const btnDecline = document.getElementById('btnDecline');
    if (btnDecline) btnDecline.disabled = false;

    if (window.visualizer3D) {
        window.visualizer3D.setState('ontime', 'UA938');
    }

    renderDrafts();
    renderState();
}


// 12. Voice call & approval record (backend proposal TG-402 ...)
function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadProposal(id) {
    const r = await window.TG_API.getProposal(id);
    if (r.ok && r.data) renderApprovalRecord(r.data);
    return r.ok ? r.data : null;
}

function renderApprovalRecord(p) {
    state.proposal = p;
    const summaryEl = document.getElementById('voiceSummary');
    const rationaleEl = document.getElementById('voiceRationale');
    const callStatusEl = document.getElementById('callStatus');
    const callBtn = document.getElementById('btnCallTraveler');
    const statusEl = document.getElementById('recordStatus');
    const listEl = document.getElementById('transcriptList');
    const actionsEl = document.getElementById('recordActions');
    if (!summaryEl) return;

    if (!p) {
        summaryEl.textContent = 'Waiting for a rebooking proposal.';
        rationaleEl.textContent = '';
        callStatusEl.textContent = 'No call placed yet.';
        callBtn.disabled = false;
        statusEl.textContent = 'PENDING';
        listEl.innerHTML = '<p class="text-mutedText">The call transcript is stored here as the approval record once the call ends.</p>';
        actionsEl.innerHTML = '';
        return;
    }

    summaryEl.textContent = p.summary || p.reason || '';
    rationaleEl.textContent = p.rationale || '';
    const badge = document.getElementById('proposalBadge');
    if (badge) badge.textContent = `PROPOSAL #${p.id}`;

    const titleEl = document.getElementById('approvalGateTitle');
    const alt = (p.context || {}).alternative;
    if (titleEl && alt) {
        titleEl.innerHTML = `Replace ${escapeHtml((p.context.replaces || []).join(' + '))} with <span class="text-emerald font-mono font-bold">${escapeHtml(alt.flight_number)}</span> (Arrives ${escapeHtml(alt.arrives_short || alt.arrives)})`;
    }

    const call = p.call;
    if (call) {
        callStatusEl.textContent = `Call ${call.status} · ${call.to_number || ''}` + (call.conversation_id ? ` · ${call.conversation_id}` : '');
    } else {
        callStatusEl.textContent = p.status === 'pending' ? 'No call placed yet.' : 'No call was placed for this proposal.';
    }
    callBtn.disabled = p.status !== 'pending' || (call && ['initiated', 'in-progress', 'processing'].includes(call.status));

    const styles = {
        pending: 'border-brass/30 bg-brass/10 text-brass',
        applied: 'border-emerald/30 bg-emeraldBg text-emerald',
        approved: 'border-emerald/30 bg-emeraldBg text-emerald',
        declined: 'border-crimson/30 bg-crimsonBg text-crimson',
        failed: 'border-crimson/30 bg-crimsonBg text-crimson'
    };
    statusEl.className = `text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border ${styles[p.status] || styles.pending}`;
    statusEl.textContent = p.status === 'applied' ? 'APPROVED' : p.status.toUpperCase();

    const turns = p.transcript || [];
    listEl.innerHTML = turns.length
        ? turns.map(t => {
            const agent = t.role === 'agent';
            return `<div class="flex ${agent ? 'justify-start' : 'justify-end'}">
                <div class="max-w-[85%] px-3 py-2 rounded-lg border ${agent ? 'bg-parchment border-slateBorder' : 'bg-emeraldBg border-emerald/20'}">
                    <span class="font-mono text-[10px] font-bold ${agent ? 'text-brass' : 'text-emerald'}">${agent ? 'AGENT' : 'TRAVELER'}</span>
                    <div class="text-ink">${escapeHtml(t.message)}</div>
                </div></div>`;
        }).join('')
        : '<p class="text-mutedText">The call transcript is stored here as the approval record once the call ends.</p>';

    const lines = [];
    if (p.decided_by) lines.push(`Decision: ${escapeHtml(p.status === 'applied' ? 'approved' : p.status)} by ${escapeHtml(p.decided_by)}${p.decided_at ? ' at ' + escapeHtml(p.decided_at) : ''}`);
    if (p.spoken_confirmation) lines.push(`Traveler said: “${escapeHtml(p.spoken_confirmation)}”`);
    const a = p.actions || {};
    if (a.email) lines.push(`✉ Rescheduling email sent to ${escapeHtml(a.email.to)} in thread ${escapeHtml(a.email.thread_id)}`);
    if (a.calendar) lines.push(`📅 Calendar (${escapeHtml(a.calendar.provider)}) event ${escapeHtml(a.calendar.updated)} updated`);
    if (p.status === 'applied' && !p.complete) lines.push('<span class="text-crimson">Some follow-up steps did not finish; approving again retries them.</span>');
    actionsEl.innerHTML = lines.map(l => `<div>${l}</div>`).join('');

    // A decision made on the phone shows up here without anyone touching the buttons.
    if (p.status !== 'pending' && (state.status === 'proposed' || state.status === 'resolving')) {
        applyDecisionVisuals(p.status === 'applied' || p.status === 'approved');
    }
}

// Select the flight the proposal is for (live trips may not be one of the four demo cards).
function selectProposalOption(p) {
    const alt = p && p.context && p.context.alternative ? p.context.alternative.flight_number : null;
    if (!alt) return;
    if (document.getElementById(`opt-${alt}`)) selectOption(alt); else state.selectedOption = alt;
    renderApprovalRecord(p); // restore the proposal's own gate title after selectOption
}

function applyDecisionVisuals(approved) {
    const stampOverlay = document.getElementById('approvalStampContainer');
    const stampBadge = document.getElementById('stampBadge');
    const buttons = document.getElementById('decisionButtonsContainer');
    if (stampOverlay) stampOverlay.classList.remove('hidden');
    if (buttons) buttons.classList.add('hidden');
    if (stampBadge) {
        stampBadge.className = `px-8 py-3 rounded-md text-2xl font-bold tracking-widest uppercase shadow-2xl ${approved ? 'stamp-approved' : 'stamp-declined'}`;
        stampBadge.textContent = approved ? 'APPROVED' : 'DECLINED';
    }
    state.status = approved ? 'approved' : 'declined';
    if (window.visualizer3D) window.visualizer3D.setState(state.status, state.selectedOption);
    renderState();
}

function revertDecision(message) {
    const stampOverlay = document.getElementById('approvalStampContainer');
    const buttons = document.getElementById('decisionButtonsContainer');
    if (stampOverlay) stampOverlay.classList.add('hidden');
    if (buttons) buttons.classList.remove('hidden');
    ['btnApprove', 'btnDecline'].forEach(id => { const b = document.getElementById(id); if (b) b.disabled = false; });
    state.status = 'proposed';
    state.isDeciding = false;
    if (window.visualizer3D) window.visualizer3D.setState('proposed', state.selectedOption);
    renderState();
    alert(`Not applied: ${message}`);
}

async function startVoiceCall() {
    const id = state.activeProposalId || (state.proposal && state.proposal.id);
    const statusEl = document.getElementById('callStatus');
    if (!id) {
        statusEl.textContent = 'No proposal yet. Run the disruption response first.';
        return;
    }
    document.getElementById('btnCallTraveler').disabled = true;
    statusEl.textContent = 'Placing call via ElevenLabs…';
    const r = await window.TG_API.callTraveler(id);
    if (!r.ok) {
        statusEl.textContent = `Call not placed: ${(r.data && r.data.detail) || 'HTTP ' + r.status}`;
        document.getElementById('btnCallTraveler').disabled = false;
        return;
    }
    renderApprovalRecord(r.data);
    startPolling(id);
}

function startPolling(id) {
    stopPolling();
    const startedAt = Date.now();
    state.pollTimer = setInterval(async () => {
        if (Date.now() - startedAt > 15 * 60 * 1000) return stopPolling();
        let p = await loadProposal(id);
        if (!p) return;
        const call = p.call;
        if (call && call.conversation_id && !['done', 'failed'].includes(call.status)) {
            const t = await window.TG_API.refreshTranscript(id);
            if (t.ok) { renderApprovalRecord(t.data); p = t.data; }
        }
        if (window.refreshConsole) refreshConsole();
        const finished = p.call && ['done', 'failed'].includes(p.call.status);
        if (finished && p.status !== 'pending') stopPolling();
    }, 4000);
}

function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
}
