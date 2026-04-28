/**
 * DeCloud DHT Node Dashboard - JavaScript
 * Version: 2.0.0
 *
 * Fetches /health, /peers, /diagnostics
 * Renders: status, identity, network stats, peers, diagnostics, event log, export
 */

// ==================== Configuration ====================
const CONFIG = {
    vmId:   '__VM_ID__',
    vmName: '__VM_NAME__',
    region: '__DHT_REGION__',
    nodeId: '__NODE_ID__',

    refreshInterval: 10000,  // 10 seconds

    api: {
        health:      '/health',
        peers:       '/peers',
        diagnostics: '/diagnostics'
    }
};

// ==================== State ====================
const state = {
    initialized:  false,
    lastUpdate:   null,
    errors:       [],
    healthData:   null,
    peersData:    null,
    diagData:     null
};

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', () => {
    console.log('DeCloud DHT Dashboard v2.0 initializing...');
    initializeDashboard();
});

async function initializeDashboard() {
    try {
        await updateDashboard();
        setInterval(updateDashboard, CONFIG.refreshInterval);
        state.initialized = true;
        hideLoadingOverlay();
    } catch (err) {
        console.error('Dashboard init failed:', err);
        showAlert('Failed to load dashboard. Please refresh the page.');
        hideLoadingOverlay();
    }
}

// ==================== Main Update Loop ====================
async function updateDashboard() {
    try {
        const [health, peers, diag] = await Promise.allSettled([
            fetchAPI(CONFIG.api.health),
            fetchAPI(CONFIG.api.peers),
            fetchAPI(CONFIG.api.diagnostics)
        ]);

        if (health.status === 'fulfilled') state.healthData = health.value;
        if (peers.status  === 'fulfilled') state.peersData  = peers.value;
        if (diag.status   === 'fulfilled') state.diagData   = diag.value;

        state.lastUpdate = new Date();

        renderOverallStatus();
        renderNodeIdentity();
        renderNetworkStats();
        renderPeersList();
        renderDiagnostics();
        renderEventLog();
        renderAddresses();
        renderLastUpdate();

        if (state.errors.length > 0) { state.errors = []; closeAlert(); }
    } catch (err) {
        console.error('Dashboard update failed:', err);
        state.errors.push(err);
        if (state.errors.length === 1) showAlert('Failed to fetch DHT node data. Retrying...');
    }
}

// ==================== Render: Overall Status ====================
function renderOverallStatus() {
    const dot  = document.getElementById('overall-status');
    const text = document.getElementById('status-text');
    const d = state.healthData;
    if (!d) { setStatus(dot, text, 'checking', 'Checking...'); return; }

    const peers = d.connectedPeers || 0;
    const rtSize = d.routingTable || 0;

    if (peers === 0)  { setStatus(dot, text, 'offline', 'Isolated'); return; }
    if (rtSize < 3)   { setStatus(dot, text, 'warning', 'Low peers'); return; }
    setStatus(dot, text, 'online', 'Operational');
}

function setStatus(dot, text, status, label) {
    if (dot)  dot.className  = 'status-dot ' + status;
    if (text) text.textContent = label;
}

// ==================== Render: Node Identity ====================
function renderNodeIdentity() {
    const d = state.healthData;
    if (!d) return;

    const peerId = d.peerId || '';
    const el = document.getElementById('peer-id-value');
    if (el) {
        el.textContent = peerId.length > 20
            ? peerId.slice(0, 12) + '…' + peerId.slice(-6) : peerId;
        el.title = peerId;
    }
    setText('uptime-value',   formatUptime(d.uptimeSeconds || 0));
    setText('routing-table',  formatNum(d.routingTable || 0) + ' peers');
    setText('status-value',   d.status || '—');
}

// ==================== Render: Network Stats ====================
function renderNetworkStats() {
    const d    = state.healthData;
    const diag = state.diagData;
    if (!d) return;

    const peers = d.connectedPeers || 0;
    setText('connected-peers-count', peers);

    // Routing table bar
    const rtSize   = d.routingTable || 0;
    const rtTarget = 20; // expect ~20 peers in k-buckets
    const rtPct    = Math.min(100, (rtSize / rtTarget) * 100);
    const rtBar = document.getElementById('routing-bar');
    if (rtBar) {
        rtBar.style.width = rtPct.toFixed(0) + '%';
        rtBar.className = 'metric-bar-fill' + (rtPct < 25 ? ' danger' : rtPct < 50 ? ' warning' : '');
    }
    setText('routing-table-count', rtSize);

    // Bootstrap status from diagnostics
    if (diag?.bootstrap) {
        const ok = diag.bootstrap.successes > 0;
        setText('bootstrap-status', ok
            ? `✓ ${diag.bootstrap.successes} successful`
            : `${diag.bootstrap.attempts} attempts, 0 successes`);
        setText('bootstrap-last', diag.bootstrap.lastAt ? timeAgo(diag.bootstrap.lastAt) : '—');
    }
}

// ==================== Render: Peers List ====================
function renderPeersList() {
    const container = document.getElementById('peers-list');
    const badge     = document.getElementById('peer-count-badge');
    if (!container) return;

    // Prefer detailed peer info from diagnostics
    const peers = state.diagData?.peers ||
                  (state.peersData?.peers || []).map(id => ({ id }));
    if (badge) badge.textContent = peers.length;

    if (peers.length === 0) {
        container.innerHTML = '<p class="empty-state-inline">No connected peers — node is isolated</p>';
        return;
    }

    container.innerHTML = peers.map(p => {
        const id    = typeof p === 'string' ? p : (p.id || p);
        const addrs = typeof p === 'object' && p.addrs ? p.addrs : [];
        const short = id.length > 20 ? id.slice(0, 12) + '…' + id.slice(-6) : id;
        return `<div class="peer-row">
            <span class="peer-dot"></span>
            <span class="peer-id mono-sm" title="${id}">${short}</span>
            ${addrs.length ? `<span class="peer-addr">${addrs[0]}</span>` : ''}
        </div>`;
    }).join('');
}

// ==================== Render: Diagnostics ====================
function renderDiagnostics() {
    const d = state.diagData;
    if (!d) return;

    // GossipSub
    setText('diag-gs-topics',   (d.gossipSub?.topics || []).join(', ') || '—');
    setText('diag-gs-relayed',  formatNum(d.gossipSub?.messagesRelayed || 0));
    setText('diag-gs-last',     d.gossipSub?.lastMessageAt ? timeAgo(d.gossipSub.lastMessageAt) : '—');

    // Bootstrap
    setText('diag-boot-attempts', formatNum(d.bootstrap?.attempts  || 0));
    setText('diag-boot-success',  formatNum(d.bootstrap?.successes || 0));
    setText('diag-boot-last',     d.bootstrap?.lastAt ? timeAgo(d.bootstrap.lastAt) : '—');

    // Peer Events
    setText('diag-peer-connects',    formatNum(d.peerEvents?.connects    || 0));
    setText('diag-peer-disconnects', formatNum(d.peerEvents?.disconnects || 0));

    // Provider Lookups
    setText('diag-prov-ok',   formatNum(d.providerLookups?.success || 0));
    setText('diag-prov-fail', formatNum(d.providerLookups?.fail    || 0));

    // Routing table size
    setText('diag-rt-size', formatNum(d.routingTable?.size || state.healthData?.routingTable || 0));
}

// ==================== Render: Event Log ====================
function renderEventLog() {
    const container = document.getElementById('event-log');
    const badge     = document.getElementById('event-count-badge');
    if (!container || !state.diagData) return;

    const events = [...(state.diagData.eventLog || [])].reverse(); // newest first
    if (badge) badge.textContent = events.length;

    if (events.length === 0) {
        container.innerHTML = '<p class="empty-state-inline">No events yet — waiting for peer activity...</p>';
        return;
    }

    container.innerHTML = events.slice(0, 80).map(e => {
        const cls  = dhtEventClass(e.event);
        const icon = dhtEventIcon(e.event);
        const ts   = formatEventTime(e.ts);
        const det  = formatEventDetails(e.details || {});
        return `<div class="event-row ${cls}">
            <span class="ev-icon">${icon}</span>
            <span class="ev-ts">${ts}</span>
            <span class="ev-name">${e.event}</span>
            <span class="ev-det">${det}</span>
        </div>`;
    }).join('');
}

function dhtEventClass(event) {
    if (event.includes('fail') || event.includes('disconnect')) return 'ev-error';
    if (event.includes('retry') || event.includes('isolated'))  return 'ev-warn';
    if (event.includes('provider'))                             return 'ev-info';
    if (event.includes('connect') || event.includes('bootstrap')) return 'ev-ok';
    return 'ev-default';
}

function dhtEventIcon(event) {
    if (event.includes('fail'))        return '✗';
    if (event.includes('disconnect'))  return '↙';
    if (event.includes('connect'))     return '↗';
    if (event.includes('bootstrap'))   return '⚡';
    if (event.includes('provider'))    return '🔍';
    if (event.includes('gossip'))      return '📨';
    return '●';
}

function formatEventDetails(d) {
    return Object.entries(d)
        .map(([k, v]) => `<span class="ev-key">${k}</span>=<span class="ev-val">${v}</span>`)
        .join(' ');
}

function formatEventTime(ts) {
    if (!ts) return '—';
    try { return new Date(ts).toTimeString().slice(0, 8); } catch { return ts; }
}

// ==================== Render: Addresses ====================
function renderAddresses() {
    const d = state.healthData;
    if (!d?.addresses) return;

    const container = document.getElementById('addresses-list');
    if (!container) return;

    container.innerHTML = (d.addresses || []).map(a =>
        `<div class="address-row"><span class="mono-sm">${a}</span></div>`
    ).join('') || '<p class="empty-state-inline">No addresses</p>';
}

// ==================== Render: Last Update ====================
function renderLastUpdate() {
    const el = document.getElementById('last-update');
    if (el && state.lastUpdate) el.textContent = 'Updated ' + state.lastUpdate.toLocaleTimeString();
}

// ==================== Export Functionality ====================
const EXPORT_ENDPOINTS = [
    { key: 'health',      label: 'Health',         url: '/health' },
    { key: 'peers',       label: 'Peers',          url: '/peers' },
    { key: 'diagnostics', label: 'Diagnostics',    url: '/diagnostics' },
];

const exportEnabled = new Set(EXPORT_ENDPOINTS.map(ep => ep.key));
const exportData    = {};
let   exportPayload = null;

function openExport() {
    document.getElementById('export-overlay').classList.add('open');
    if (!document.getElementById('exp-item-health')) buildSectionList();
    if (exportPayload) renderExportPreview(exportPayload);
}

function closeExport() {
    document.getElementById('export-overlay').classList.remove('open');
}

function closeExportOnBackdrop(e) {
    if (e.target === document.getElementById('export-overlay')) closeExport();
}

function buildSectionList() {
    const container = document.getElementById('export-sections');
    container.innerHTML = EXPORT_ENDPOINTS.map(ep => `
        <div class="export-section-item" id="exp-item-${ep.key}">
            <label class="tog" title="Include in export">
                <input type="checkbox" id="exp-tog-${ep.key}" checked
                       onchange="onToggleSection('${ep.key}', this.checked)">
                <span class="tog-slider"></span>
            </label>
            <span class="export-sec-dot pending" id="exp-dot-${ep.key}"></span>
            <span class="export-sec-name">${ep.label}</span>
            <span class="export-sec-size" id="exp-size-${ep.key}"></span>
        </div>`).join('');
}

function onToggleSection(key, enabled) {
    if (enabled) exportEnabled.add(key); else exportEnabled.delete(key);
    const item = document.getElementById(`exp-item-${key}`);
    const dot  = document.getElementById(`exp-dot-${key}`);
    const size = document.getElementById(`exp-size-${key}`);
    if (item) item.style.opacity = enabled ? '1' : '0.4';
    if (!enabled) {
        if (dot)  dot.className = 'export-sec-dot pending';
        if (size) { size.textContent = 'skipped'; size.style.color = 'var(--text-muted)'; }
    } else if (exportData[key]) {
        if (dot)  dot.className = 'export-sec-dot done';
        if (size) { size.textContent = fmtBytes(JSON.stringify(exportData[key]).length); size.style.color = ''; }
    }
    rebuildPayload();
}

async function collectExport() {
    const btn        = document.getElementById('export-collect-btn');
    const statusText = document.getElementById('export-status-text');
    const copyBtn    = document.getElementById('export-copy-btn');
    const dlBtn      = document.getElementById('export-dl-btn');

    if (btn) { btn.disabled = true; btn.textContent = 'Collecting...'; }
    if (copyBtn) copyBtn.disabled = true;
    if (dlBtn)   dlBtn.disabled   = true;

    if (!document.getElementById('exp-item-health')) buildSectionList();

    let completed = 0, errors = 0, skipped = 0;

    const tasks = EXPORT_ENDPOINTS.map(async ep => {
        const dot  = document.getElementById(`exp-dot-${ep.key}`);
        const size = document.getElementById(`exp-size-${ep.key}`);
        const item = document.getElementById(`exp-item-${ep.key}`);
        const tog  = document.getElementById(`exp-tog-${ep.key}`);
        if (!exportEnabled.has(ep.key)) { skipped++; return; }
        if (dot) dot.className = 'export-sec-dot loading';
        if (tog) tog.disabled = true;
        try {
            const r    = await fetch(ep.url, { headers: { Accept: 'application/json' } });
            const data = r.ok ? await r.json() : null;
            exportData[ep.key] = r.ok ? data : { _error: `HTTP ${r.status}` };
            const bytes = JSON.stringify(exportData[ep.key]).length;
            if (r.ok) {
                if (item) item.className = 'export-section-item done';
                if (dot)  dot.className  = 'export-sec-dot done';
                if (size) { size.textContent = fmtBytes(bytes); size.style.color = ''; }
                completed++;
            } else {
                if (item) item.className = 'export-section-item error';
                if (dot)  dot.className  = 'export-sec-dot error';
                if (size) { size.textContent = 'error'; size.style.color = 'var(--danger)'; }
                errors++;
            }
        } catch (e) {
            exportData[ep.key] = { _error: e.message };
            if (item) item.className = 'export-section-item error';
            if (dot)  dot.className  = 'export-sec-dot error';
            if (size) { size.textContent = 'failed'; size.style.color = 'var(--danger)'; }
            errors++;
        } finally {
            if (tog) tog.disabled = false;
        }
    });

    await Promise.all(tasks);
    rebuildPayload();

    if (btn)     { btn.disabled = false; btn.textContent = 'Re-collect'; }
    if (copyBtn) copyBtn.disabled = false;
    if (dlBtn)   dlBtn.disabled   = false;

    const enabled = EXPORT_ENDPOINTS.length - skipped;
    if (statusText) statusText.textContent = errors === 0
        ? `${completed}/${enabled} collected`
        : `${completed}/${enabled} ok, ${errors} failed`;
}

function rebuildPayload() {
    const snapshot = {
        _meta: {
            collectedAt: new Date().toISOString(),
            vmId:        CONFIG.vmId,
            vmName:      CONFIG.vmName,
            region:      CONFIG.region,
            nodeId:      CONFIG.nodeId,
            dashboardVersion: '2.0',
            sections: [...exportEnabled]
        }
    };
    for (const ep of EXPORT_ENDPOINTS) {
        if (exportEnabled.has(ep.key) && exportData[ep.key]) snapshot[ep.key] = exportData[ep.key];
    }
    exportPayload = snapshot;
    renderExportPreview(snapshot);
    return snapshot;
}

function renderExportPreview(payload) {
    const json    = JSON.stringify(payload, null, 2);
    const preview = document.getElementById('export-preview');
    const sizeEl  = document.getElementById('export-preview-size');
    const MAX     = 80_000;
    if (preview) preview.textContent = json.length > MAX
        ? json.slice(0, MAX) + '\n\n… (truncated)'
        : json;
    if (sizeEl) sizeEl.textContent = fmtBytes(json.length);
}

function exportCopy() {
    const payload = rebuildPayload();
    if (Object.keys(payload).length <= 1) { alert('Click Collect All Data first.'); return; }
    navigator.clipboard.writeText(JSON.stringify(payload, null, 2))
        .then(() => { const b = document.getElementById('export-copy-btn'); if (b) { b.textContent = 'Copied!'; setTimeout(() => b.textContent = 'Copy JSON', 2000); } })
        .catch(() => alert('Copy failed — use Download instead.'));
}

function exportDownload() {
    const payload = rebuildPayload();
    if (Object.keys(payload).length <= 1) { alert('Click Collect All Data first.'); return; }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `dht-${CONFIG.vmId.slice(-6)}-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
}

// ==================== Utilities ====================
async function fetchAPI(url) {
    const r = await fetch(url, { headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' } });
    if (!r.ok) throw new Error(`HTTP ${r.status} from ${url}`);
    return r.json();
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val ?? '—';
}

function fmtBytes(bytes) {
    if (bytes == null || bytes === 0) return bytes === 0 ? '0 B' : '—';
    const k = 1024, sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatNum(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString();
}

function formatUptime(secs) {
    if (!secs) return '—';
    const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function timeAgo(ts) {
    if (!ts) return '—';
    try {
        const diff = (Date.now() - new Date(ts).getTime()) / 1000;
        if (diff < 10)    return 'just now';
        if (diff < 60)    return Math.round(diff) + 's ago';
        if (diff < 3600)  return Math.round(diff / 60) + 'm ago';
        if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
        return Math.round(diff / 86400) + 'd ago';
    } catch { return ts; }
}

function hideLoadingOverlay() {
    const el = document.getElementById('loading-overlay');
    if (el) { el.classList.add('hidden'); setTimeout(() => el.style.display = 'none', 300); }
}

function showAlert(msg) {
    const banner = document.getElementById('alert-banner');
    const text   = document.getElementById('alert-message');
    if (banner) banner.style.display = 'flex';
    if (text)   text.textContent = msg;
}

function closeAlert() {
    const banner = document.getElementById('alert-banner');
    if (banner) banner.style.display = 'none';
}

window.debugState = function() {
    console.log('=== DHT Dashboard State ===');
    console.log('Health:', state.healthData);
    console.log('Peers:', state.peersData);
    console.log('Diagnostics:', state.diagData);
};