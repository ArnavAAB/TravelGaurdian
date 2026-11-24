/**
 * TravelGuardian Backend API Integration Adapter
 * Connects the web frontend to the FastAPI server with local simulation fallback.
 */

class TravelGuardianAPI {
    constructor() {
        this.baseUrl = window.location.origin;
        this.isConnected = false;
        this.ready = this.checkBackendConnection();
    }

    _updateStatusBadge(label) {
        const apply = () => {
            const statusEl = document.getElementById('agentStatusText');
            if (statusEl) statusEl.textContent = label;
        };
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', apply, { once: true });
        } else {
            apply();
        }
    }

    async checkBackendConnection() {
        try {
            const res = await fetch(`${this.baseUrl}/api/status`, {
                method: 'GET',
                signal: AbortSignal.timeout(8000)
            });
            this.isConnected = res.ok;
            console.log(`[TG_API] Backend check: ${res.status} (connected=${this.isConnected})`);
        } catch (e) {
            this.isConnected = false;
            console.warn('[TG_API] Backend unreachable, using local simulation:', e);
        }

        this._updateStatusBadge(
            this.isConnected ? 'Connected to FastAPI Backend' : 'Local ToolBox Simulation Active'
        );
        return this.isConnected;
    }

    async _request(path, options = {}) {
        await this.ready;
        if (!this.isConnected) return null;

        const res = await fetch(`${this.baseUrl}${path}`, options);
        if (!res.ok) {
            throw new Error(`HTTP ${res.status} on ${path}`);
        }
        return await res.json();
    }

    async getStatus() {
        try {
            return (await this._request('/api/status')) ?? { simulated: true };
        } catch (e) {
            console.warn('[TG_API] getStatus failed:', e);
            return { simulated: true };
        }
    }

    async getItinerary() {
        try {
            return await this._request('/api/itinerary');
        } catch (e) {
            console.warn('[TG_API] getItinerary failed:', e);
            return null;
        }
    }

    async triggerDelay(flightNum = 'UA1542', delayMins = 110, reason = 'Late-arriving aircraft') {
        console.log(`[TG_API] Triggering ${flightNum} +${delayMins}m delay`);
        try {
            return await this._request('/api/simulate-delay', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ flight: flightNum, delay: delayMins, reason })
            });
        } catch (e) {
            console.warn('[TG_API] Backend delay call failed, using local fallback:', e);
            return null;
        }
    }

    async clearDisruptions() {
        try {
            return await this._request('/api/clear-disruptions', { method: 'POST' });
        } catch (e) {
            console.warn('[TG_API] Clear disruptions failed:', e);
            return null;
        }
    }

    async resolveDisruption() {
        try {
            return await this._request('/api/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ offline: false })
            });
        } catch (e) {
            console.warn('[TG_API] Agent response failed, using fallback:', e);
            return null;
        }
    }

    // With no backend (index.html opened as a file) the decision is simulated locally.
    // With a backend, a refusal (e.g. 403 through a tunnel, 409 not pending) is returned as { error }.
    async approveProposal(proposalId, optionId) {
        console.log(`[TG_API] Approving proposal ${proposalId} with ${optionId}`);
        const r = await this._requestDetailed('/api/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: proposalId, option: optionId })
        });
        if (r.status === 0 && !this.isConnected) return { status: 'applied', message: 'Rebooking approved locally (offline demo)' };
        if (!r.ok) return { error: (r.data && r.data.detail) || `HTTP ${r.status}` };
        return r.data;
    }

    async declineProposal(proposalId) {
        console.log(`[TG_API] Declining proposal ${proposalId}`);
        const r = await this._requestDetailed('/api/decline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: proposalId })
        });
        if (r.status === 0 && !this.isConnected) return { status: 'declined', message: 'Rebooking declined locally (offline demo)' };
        if (!r.ok) return { error: (r.data && r.data.detail) || `HTTP ${r.status}` };
        return r.data;
    }

    async askAgent(question) {
        try {
            return await this._request('/api/ask', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ question })
            });
        } catch (e) {
            console.warn('[TG_API] Ask failed, falling back:', e);
            return null;
        }
    }

    async ingestEmails() {
        try {
            return await this._request('/api/sync', { method: 'POST' });
        } catch (e) {
            console.warn('[TG_API] Sync failed:', e);
            return null;
        }
    }

    // Like _request, but returns { ok, status, data } so callers can show the server's error detail.
    async _requestDetailed(path, options = {}) {
        await this.ready;
        if (!this.isConnected) return { ok: false, status: 0, data: { detail: 'Backend not connected' } };
        try {
            const res = await fetch(`${this.baseUrl}${path}`, options);
            let data = null;
            try { data = await res.json(); } catch (_) { data = null; }
            return { ok: res.ok, status: res.status, data };
        } catch (e) {
            return { ok: false, status: 0, data: { detail: String(e) } };
        }
    }

    async evaluate(call = false) {
        return this._requestDetailed('/api/evaluate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ call })
        });
    }

    async getProposal(id) {
        return this._requestDetailed(`/api/proposals/${encodeURIComponent(id)}`);
    }

    async callTraveler(id) {
        return this._requestDetailed(`/api/proposals/${encodeURIComponent(id)}/call`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
    }

    async refreshTranscript(id) {
        return this._requestDetailed(`/api/proposals/${encodeURIComponent(id)}/transcript`, { method: 'POST' });
    }

    async getConfig() {
        return this._requestDetailed('/api/config');
    }

    async getDrafts() {
        try {
            return await this._request('/api/drafts');
        } catch (e) {
            console.warn('[TG_API] Get drafts failed:', e);
            return null;
        }
    }
}

window.TG_API = new TravelGuardianAPI();