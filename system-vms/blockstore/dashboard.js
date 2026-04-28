/**
 * DeCloud Block Store Dashboard - JavaScript
 * Version: 2.0.0
 *
 * Fetches /health, /stats, /manifests, /diagnostics
 * Renders: storage, identity, resources, peers, diagnostics, event log, export
 */

// ==================== Configuration ====================
const CONFIG = {
    vmId:        '__VM_ID__',
    vmName:      '__VM_NAME__',
    region:      '__NODE_REGION__',
    nodeId:      '__NODE_ID__',
    advertiseIp: '__BLOCKSTORE_ADVERTISE_IP__',

    refreshInterval: 15000,  // 15 seconds

    api: {
        health:      '/health',
        stats:       '/stats',
        manifests:   '/manifests',
        diagnostics: '/diagnostics'
    },

    gcTrigger:   85,
    gcHardLimit: 95
};

// ==================== State ====================
const state = {
    initialized:    false,
    lastUpdate:     null,
    errors:         [],
    healthData:     null,
    statsData:      null,
    manifestsData:  null,
    diagData:       null
};

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', () => {
    console.log('DeCloud Block Store Dashboard v2.0 initializing...');
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
        const [health, stats, manifests, diag] = await Promise.allSettled([
            fetchAPI(CONFIG.api.health),
            fetchAPI(CONFIG.api.stats),
            fetchAPI(CONFIG.api.manifests),
            fetchAPI(CONFIG.api.diagnostics)
        ]);

        if (health.status === 'fulfilled')    state.healthData    = health.value;
        if (stats.status === 'fulfilled')     state.statsData     = stats.value;
        if (manifests.status === 'fulfilled') state.manifestsData = manifests.value;
        if (diag.status === 'fulfilled')      state.diagData      = diag.value;

        state.lastUpdate = new Date();

        renderStatus();
        renderStorage();
        renderIdentity();
        renderResourcesTable();
        renderPeersList();
        renderDiagnostics();
        renderEventLog();
        renderFooter();
        renderLastUpdate();

        if (state.errors.length > 0) { state.errors = []; closeAlert(); }
    } catch (err) {
        console.error('Dashboard update failed:', err);
        state.errors.push(err);
        if (state.errors.length === 1) showAlert('Failed to fetch block store data. Retrying...');
    }
}

// ==================== Render: Status ====================
function renderStatus() {
    const d = state.healthData;
    if (!d) return;

    const dot  = document.getElementById('overall-status');
    const text = document.getElementById('status-text');

    const ok = d.status === 'healthy' || d.status === 'ok';
    if (dot)  dot.className  = 'status-dot ' + (ok ? 'online' : 'offline');
    if (text) text.textContent = ok ? 'Operational' : (d.status || 'Unknown');
}

// ==================== Render: Storage ====================
function renderStorage() {
    const d = state.healthData;
    if (!d) return;

    const used     = d.usedBytes     || 0;
    const capacity = d.capacityBytes || 1;
    const pct      = Math.min(100, (used / capacity) * 100);

    const bar = document.getElementById('storage-bar');
    if (bar) {
        bar.style.width = pct.toFixed(1) + '%';
        bar.className = 'storage-bar-fill' +
            (pct >= CONFIG.gcHardLimit ? ' danger' : pct >= CONFIG.gcTrigger ? ' warning' : '');
    }

    setText('used-label',     formatBytes(used));
    setText('capacity-label', formatBytes(capacity));
    setText('usage-badge',    pct.toFixed(1) + '%');
    setText('block-count',    formatNum(d.blockCount || 0));
    setText('manifest-count', formatNum((state.manifestsData?.manifests || []).length));

    // Bitswap from diagnostics
    const diag = state.diagData;
    if (diag?.bitswap) {
        setText('bitswap-sent',     formatBytes(diag.bitswap.sentBytes || 0));
        setText('bitswap-received', formatBytes((d.bitswapReceived || 0) * 4096)); // approx if no byte count
    } else {
        setText('bitswap-sent',     d.bitswapSent != null ? formatNum(d.bitswapSent) : '—');
        setText('bitswap-received', d.bitswapReceived != null ? formatNum(d.bitswapReceived) : '—');
    }
}

// ==================== Render: Identity ====================
function renderIdentity() {
    const d = state.healthData;
    if (!d) return;

    const peerId = d.peerId || '';
    const el = document.getElementById('peer-id');
    if (el) {
        el.textContent = peerId.length > 20
            ? peerId.substring(0, 12) + '...' + peerId.substring(peerId.length - 6)
            : peerId;
        el.title = peerId;
    }
    setText('connected-peers', d.connectedPeers || 0);
    setText('uptime-value',    formatUptime(d.uptimeSeconds || 0));
    setText('advertise-ip',    CONFIG.advertiseIp);
    setText('node-region',     CONFIG.region);
}

// ==================== Render: Resources Table ====================
function renderResourcesTable() {
    const wrap = document.getElementById('resources-table-wrap');
    const badge = document.getElementById('resource-count-badge');
    if (!state.manifestsData) return;

    const manifests = state.manifestsData.manifests || [];

    // Group by resourceId — keep only the latest version per resource.
    // The blockstore stores one manifest entry per rootCid (one per lazysync
    // cycle), so a single VM may have multiple entries. Only the highest
    // version is meaningful for display.
    const grouped = {};
    for (const m of manifests) {
        const key = m.resourceId || m.rootCid || '—';
        if (!grouped[key] || (m.version || 0) > (grouped[key].version || 0)) {
            grouped[key] = m;
        }
    }
    const latest = Object.values(grouped);
    if (badge) badge.textContent = latest.length;

    if (latest.length === 0) {
        wrap.innerHTML =
            '<div class="empty-state">' +
            '<span class="empty-icon">&#x1f4e6;</span>' +
            '<p>No resources stored yet</p>' +
            '<p style="font-size:0.75rem;margin-top:0.5rem;color:var(--text-muted)">Blocks appear here as VM overlays and model shards replicate in</p>' +
            '</div>';
        return;
    }

    let html = '<table class="resources-table"><thead><tr>' +
        '<th>Type</th><th>Resource</th><th>Owner</th>' +
        '<th>Root CID</th><th>Version</th><th>Size</th><th>Chunks</th><th>Updated</th>' +
        '</tr></thead><tbody>';

    for (const m of latest) html += renderResourceRow(m);
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

function renderResourceRow(m) {
    const typeBadge = resourceTypeBadge(m.resourceType);
    const resourceId = truncate(m.resourceId || '—', 24);
    const owner = truncateWallet(m.resourceOwner || '—');
    const rootCid = truncate(m.rootCid || '—', 20);
    const version = m.version != null ? `v${m.version}` : '—';
    const size = formatBytes(m.totalBytes || 0);
    const chunks = formatNum(m.chunkCids ? m.chunkCids.length : 0);
    const updated = m.updatedAt ? timeAgo(m.updatedAt) : '—';

    return `<tr>
        <td>${typeBadge}</td>
        <td><span class="res-id" title="${m.resourceId || ''}">${resourceId}</span></td>
        <td><span class="mono-sm" title="${m.resourceOwner || ''}">${owner}</span></td>
        <td><span class="mono-sm" title="${m.rootCid || ''}">${rootCid}</span></td>
        <td><span class="mono-sm">${version}</span></td>
        <td>${size}</td>
        <td>${chunks}</td>
        <td>${updated}</td>
    </tr>`;
}

// ==================== Render: Peers ====================
function renderPeersList() {
    const container = document.getElementById('peers-list');
    const badge     = document.getElementById('peer-count-badge');
    if (!container) return;

    // Prefer detailed peer info from diagnostics
    const peers = state.diagData?.peers || state.manifestsData?.peers || [];
    if (badge) badge.textContent = peers.length;

    if (peers.length === 0) {
        container.innerHTML = '<p class="empty-state-inline">No connected peers</p>';
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
    if (!d) {
        setText('diag-status', 'Waiting for diagnostics...');
        return;
    }

    // GossipSub
    setText('diag-gs-received',  formatNum(d.gossipSub?.messagesReceived  || 0));
    setText('diag-gs-published', formatNum(d.gossipSub?.messagesPublished || 0));
    setText('diag-gs-topics',    (d.gossipSub?.topics || []).join(', ') || '—');
    setText('diag-gs-last-rx',   d.gossipSub?.lastReceivedAt  ? timeAgo(d.gossipSub.lastReceivedAt)  : '—');
    setText('diag-gs-last-tx',   d.gossipSub?.lastPublishedAt ? timeAgo(d.gossipSub.lastPublishedAt) : '—');

    // DHT Announce
    setText('diag-dht-ok',   formatNum(d.dhtAnnounce?.success || 0));
    setText('diag-dht-fail', formatNum(d.dhtAnnounce?.fail    || 0));
    setText('diag-dht-last', d.dhtAnnounce?.lastAt ? timeAgo(d.dhtAnnounce.lastAt) : '—');
    setText('diag-dht-reannounce', formatNum(d.dhtAnnounce?.reannounceCount || 0));

    // XOR
    setText('diag-xor-accepted', formatNum(d.xor?.accepted || 0));
    setText('diag-xor-rejected', formatNum(d.xor?.rejected || 0));
    const total = (d.xor?.accepted || 0) + (d.xor?.rejected || 0);
    const rate  = total > 0 ? ((d.xor.accepted / total) * 100).toFixed(0) + '%' : '—';
    setText('diag-xor-rate', rate);

    // Bitswap
    setText('diag-bs-received', formatNum(d.bitswap?.received || 0));
    setText('diag-bs-sent',     formatNum(d.bitswap?.sent     || 0));

    // GC
    setText('diag-gc-runs',    formatNum(d.gc?.runCount     || 0));
    setText('diag-gc-evicted', formatNum(d.gc?.blocksEvicted || 0));
    setText('diag-gc-freed',   formatBytes(d.gc?.bytesFreed  || 0));

    // Replication chain health indicator
    renderReplicationChain(d);
}

function renderReplicationChain(d) {
    const chain = document.getElementById('replication-chain');
    if (!chain) return;

    const gs  = d.gossipSub   || {};
    const dht = d.dhtAnnounce || {};
    const bs  = d.bitswap     || {};

    const steps = [
        {
            label: 'Block Write',
            ok: (state.healthData?.blockCount || 0) > 0,
            detail: `${formatNum(state.healthData?.blockCount || 0)} blocks stored`
        },
        {
            label: 'DHT Announce',
            ok: (dht.success || 0) > 0,
            detail: dht.fail > 0
                ? `${dht.success} ok / ${dht.fail} fail`
                : `${formatNum(dht.success || 0)} announced`
        },
        {
            label: 'GossipSub Publish',
            ok: (gs.messagesPublished || 0) > 0,
            detail: `${formatNum(gs.messagesPublished || 0)} published`
        },
        {
            label: 'GossipSub Receive',
            ok: (gs.messagesReceived || 0) > 0,
            detail: `${formatNum(gs.messagesReceived || 0)} received`
        },
        {
            label: 'Bitswap Exchange',
            ok: (bs.received || 0) > 0 || (bs.sent || 0) > 0,
            detail: `↑${formatNum(bs.sent || 0)} ↓${formatNum(bs.received || 0)}`
        }
    ];

    chain.innerHTML = steps.map((s, i) => `
        <div class="chain-step ${s.ok ? 'ok' : 'idle'}">
            <div class="chain-num">${i + 1}</div>
            <div class="chain-body">
                <div class="chain-label">${s.label}</div>
                <div class="chain-detail">${s.detail}</div>
            </div>
            ${i < steps.length - 1 ? '<div class="chain-arrow">→</div>' : ''}
        </div>`).join('');
}

// ==================== Render: Event Log ====================
function renderEventLog() {
    const container = document.getElementById('event-log');
    const badge     = document.getElementById('event-count-badge');
    if (!container || !state.diagData) return;

    const events = [...(state.diagData.eventLog || [])].reverse(); // newest first
    if (badge) badge.textContent = events.length;

    if (events.length === 0) {
        container.innerHTML = '<p class="empty-state-inline">No events yet — waiting for block activity...</p>';
        return;
    }

    container.innerHTML = events.slice(0, 100).map(e => {
        const cls  = eventClass(e.event);
        const icon = eventIcon(e.event);
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

function eventClass(event) {
    if (event.includes('fail') || event.includes('reject'))  return 'ev-error';
    if (event.includes('gc') || event.includes('evict'))     return 'ev-warn';
    if (event.includes('bitswap') || event.includes('dht'))  return 'ev-info';
    if (event.includes('gossip'))                            return 'ev-gossip';
    return 'ev-default';
}

function eventIcon(event) {
    if (event.includes('fail'))     return '✗';
    if (event.includes('reject'))   return '⊘';
    if (event.includes('gc'))       return '♻';
    if (event.includes('announce')) return '📡';
    if (event.includes('gossip'))   return '📨';
    if (event.includes('bitswap'))  return '⇄';
    if (event.includes('peer'))     return '🔗';
    if (event.includes('reannounce')) return '🔁';
    if (event.includes('manifest')) return '📋';
    return '●';
}

function formatEventDetails(d) {
    return Object.entries(d)
        .map(([k, v]) => `<span class="ev-key">${k}</span>=<span class="ev-val">${v}</span>`)
        .join(' ');
}

function formatEventTime(ts) {
    if (!ts) return '—';
    try {
        const d = new Date(ts);
        return d.toTimeString().slice(0, 8);
    } catch { return ts; }
}

// ==================== Render: Footer & Update time ====================
function renderFooter() {
    const peers = state.diagData?.connectedPeers ?? state.healthData?.connectedPeers ?? 0;
    const blocks = state.healthData?.blockCount ?? 0;
    setText('footer-peers',   peers + ' peer' + (peers !== 1 ? 's' : ''));
    setText('footer-storage', formatBytes(state.healthData?.usedBytes || 0) + ' used');
    setText('footer-blocks',  formatNum(blocks) + ' blocks');
}

function renderLastUpdate() {
    const el = document.getElementById('last-update');
    if (el && state.lastUpdate) el.textContent = 'Updated ' + state.lastUpdate.toLocaleTimeString();
}

// ==================== Export Functionality ====================
const EXPORT_ENDPOINTS = [
    { key: 'health',      label: 'Health',         url: '/health' },
    { key: 'stats',       label: 'Storage Stats',  url: '/stats' },
    { key: 'manifests',   label: 'Manifests',      url: '/manifests' },
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
            const r = await fetch(ep.url, { headers: { Accept: 'application/json' } });
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
            collectedAt:       new Date().toISOString(),
            vmId:              CONFIG.vmId,
            vmName:            CONFIG.vmName,
            region:            CONFIG.region,
            nodeId:            CONFIG.nodeId,
            dashboardVersion:  '2.0',
            sections:          [...exportEnabled]
        }
    };
    for (const ep of EXPORT_ENDPOINTS) {
        if (exportEnabled.has(ep.key) && exportData[ep.key]) {
            snapshot[ep.key] = exportData[ep.key];
        }
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
        ? json.slice(0, MAX) + '\n\n… (truncated in preview — full data available via Copy/Download)'
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
    a.download = `blockstore-${CONFIG.vmId.slice(-6)}-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.json`;
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

function formatBytes(bytes) {
    if (bytes == null) return '—';
    if (bytes === 0)   return '0 B';
    const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function fmtBytes(bytes) { return formatBytes(bytes); }

function formatNum(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString();
}

function formatUptime(secs) {
    if (!secs) return '—';
    const d = Math.floor(secs / 86400);
    const h = Math.floor((secs % 86400) / 3600);
    const m = Math.floor((secs % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function timeAgo(ts) {
    if (!ts) return '—';
    try {
        const diff = (Date.now() - new Date(ts).getTime()) / 1000;
        if (diff < 10)   return 'just now';
        if (diff < 60)   return Math.round(diff) + 's ago';
        if (diff < 3600) return Math.round(diff / 60) + 'm ago';
        if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
        return Math.round(diff / 86400) + 'd ago';
    } catch { return ts; }
}

function truncate(s, n) {
    if (!s) return '—';
    return s.length > n ? s.slice(0, n) + '…' : s;
}

function truncateWallet(w) {
    if (!w || w === '—') return '—';
    if (w.length <= 12)   return w;
    return w.slice(0, 6) + '…' + w.slice(-4);
}

function resourceTypeBadge(t) {
    const map = {
        VMOverlay:     { label: 'VM Overlay',    cls: 'badge-vm'    },
        ModelShard:    { label: 'Model Shard',   cls: 'badge-model' },
        LoRAAdapter:   { label: 'LoRA',          cls: 'badge-lora'  },
        ImageTemplate: { label: 'Base Image',    cls: 'badge-image' },
        Unknown:       { label: 'Unknown',       cls: 'badge-unk'   }
    };
    const b = map[t] || map.Unknown;
    return `<span class="type-badge ${b.cls}">${b.label}</span>`;
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
    console.log('=== BlockStore Dashboard State ===');
    console.log('Health:', state.healthData);
    console.log('Stats:', state.statsData);
    console.log('Diagnostics:', state.diagData);
    console.log('Manifests:', state.manifestsData);
};