/**
 * DeCloud Relay VM Dashboard - JavaScript
 * Version: 2.0.0 - PEER CLASSIFICATION
 *
 * CHANGES v2.0.0:
 * - Peers grouped by type: CGNAT nodes, system VMs, relay peers
 * - Capacity counts only CGNAT node peers (system VMs excluded)
 * - System VMs grouped under parent node in dashboard
 * - Relay host infrastructure section for local system VMs
 */

// ==================== Configuration ====================
const CONFIG = {
    relayId: '__VM_ID__',
    relayName: '__VM_NAME__',
    relayRegion: '__RELAY_REGION__',
    relayCapacity: parseInt('__RELAY_CAPACITY__'),
    nodeId: '__NODE_ID__',  // Relay host's node ID

    // Update intervals
    refreshInterval: 10000,  // 10 seconds
    quickRefreshInterval: 5000,  // 5 seconds for critical checks

    // API endpoints
    api: {
        wireguard: '/api/relay/wireguard',
        status: '/api/relay/status',
        cleanupStats: '/api/relay/cleanup/stats'
    },

    // Thresholds
    thresholds: {
        handshakeStale: 180,  // 3 minutes
        handshakeWarning: 600,  // 10 minutes
        handshakeGracePeriod: 60,  // 1 minute - newly registered peers
        memoryWarning: 70,  // 70%
        memoryDanger: 85,  // 85%
        cpuWarning: 70,
        cpuDanger: 85
    }
};

// ==================== State Management ====================
const state = {
    initialized: false,
    lastUpdate: null,
    updateCount: 0,
    errors: [],

    // Data caches
    wireguardData: null,
    statusData: null,
    cleanupStats: null,

    // Connection states
    orchestratorStatus: 'checking',
    hostStatus: 'checking',
    cgnatNodes: [],
    systemVms: [],
    hostSystemVms: [],

    // Metrics
    totalRx: 0,
    totalTx: 0
};

// ==================== Initialization ====================
document.addEventListener('DOMContentLoaded', () => {
    console.log('DeCloud Relay Dashboard initializing...');
    console.log(`   Version: 2.0.0 (PEER CLASSIFICATION)`);
    console.log(`   Relay ID: ${CONFIG.relayId}`);
    console.log(`   Region: ${CONFIG.relayRegion}`);
    console.log(`   Capacity: ${CONFIG.relayCapacity} nodes`);

    initializeDashboard();
});

async function initializeDashboard() {
    try {
        // Initial data fetch
        await updateDashboard();

        // Set up auto-refresh
        setInterval(updateDashboard, CONFIG.refreshInterval);

        // Mark as initialized
        state.initialized = true;

        // Hide loading overlay
        hideLoadingOverlay();

        console.log('✓ Dashboard initialized successfully');
    } catch (error) {
        console.error('Failed to initialize dashboard:', error);
        showAlert('Failed to load dashboard. Please refresh the page.', 'error');
        hideLoadingOverlay();
    }
}

// ==================== Main Update Loop ====================
async function updateDashboard() {
    try {
        // Fetch all data in parallel - FIXED endpoint!
        const [wireguard, status, cleanupStats] = await Promise.all([
            fetchAPI(CONFIG.api.wireguard),
            fetchAPI(CONFIG.api.status),  // ✅ FIXED: was CONFIG.api.system
            fetchAPI(CONFIG.api.cleanupStats).catch(() => null)  // Optional
        ]);

        // Update state
        state.wireguardData = wireguard;
        state.statusData = status;  // ✅ FIXED
        state.cleanupStats = cleanupStats;
        state.lastUpdate = new Date();
        state.updateCount++;

        // Render all sections
        renderOverallStatus();
        renderUpstreamConnections();
        renderCGNATNodes();
        renderSystemMetrics();
        renderWireGuardStatus();
        renderCleanupInfo();  // ✅ NEW
        renderLastUpdate();

        // Clear error state
        if (state.errors.length > 0) {
            state.errors = [];
            closeAlert();
        }

    } catch (error) {
        console.error('Dashboard update failed:', error);
        state.errors.push(error);

        if (state.errors.length === 1) {
            showAlert(`Failed to fetch metrics. ${error.message} Retrying...`, 'warning');
        }
    }
}

// ==================== API Communication ====================
async function fetchAPI(endpoint) {
    const response = await fetch(endpoint, {
        method: 'GET',
        headers: {
            'Accept': 'application/json',
            'Cache-Control': 'no-cache'
        }
    });

    if (!response.ok) {
        throw new Error(`API request failed: ${response.status}`);
    }

    const contentType = response.headers.get('content-type');
    if (!contentType || !contentType.includes('application/json')) {
        throw new Error(`Expected JSON, got ${contentType}`);
    }

    return response.json();
}

// ==================== Rendering Functions ====================

/**
 * Render overall relay status indicator
 */
function renderOverallStatus() {
    const statusDot = document.getElementById('overall-status');
    const statusText = document.getElementById('status-text');

    if (!state.wireguardData || !state.statusData) {
        setStatus(statusDot, statusText, 'checking', 'Checking...');
        return;
    }

    // Check for critical issues
    const hasOrchestratorConnection = checkOrchestratorConnection();
    const memoryUsage = state.statusData.memory_percent || 0;

    if (!hasOrchestratorConnection) {
        setStatus(statusDot, statusText, 'offline', 'No Orchestrator');
        return;
    }

    if (memoryUsage > CONFIG.thresholds.memoryDanger) {
        setStatus(statusDot, statusText, 'warning', 'High Memory');
        return;
    }

    setStatus(statusDot, statusText, 'online', 'Operational');
}

/**
 * Check if orchestrator connection is present
 */
function checkOrchestratorConnection() {
    if (!state.wireguardData || !state.wireguardData.peers) return false;

    const peers = state.wireguardData.peers;
    const orchestratorPeer = peers.find(p =>
        p.allowed_ips && p.allowed_ips.includes('10.20.0.1')
    );

    if (!orchestratorPeer) return false;

    // Check if handshake is recent
    const now = Date.now() / 1000;
    const handshakeAge = orchestratorPeer.latest_handshake > 0
        ? now - orchestratorPeer.latest_handshake
        : Infinity;

    return handshakeAge < CONFIG.thresholds.handshakeWarning;
}

/**
 * Render upstream connections (Orchestrator + Host Node)
 */
function renderUpstreamConnections() {
    if (!state.wireguardData) return;

    const peers = state.wireguardData.peers || [];

    // Find orchestrator peer
    const orchestratorPeer = peers.find(p =>
        p.allowed_ips && p.allowed_ips.includes('10.20.0.1')
    );

    // Render orchestrator connection
    renderOrchestratorConnection(orchestratorPeer);

    // Render host node connection
    renderHostConnection();

    // Update count badge - orchestrator + host
    const orchestratorConnected = orchestratorPeer ? 1 : 0;
    const hostConnected = state.statusData ? 1 : 0; // Host is connected if we have status data
    const connectedCount = orchestratorConnected + hostConnected;

    document.getElementById('upstream-count').textContent = connectedCount;
}

/**
 * Render orchestrator connection status
 */
function renderOrchestratorConnection(peer) {
    const container = document.getElementById('orchestrator-connection');

    if (!peer) {
        container.innerHTML = `
            <div class="node-card offline">
                <div class="node-header">
                    <div class="node-info">
                        <div class="node-name">Orchestrator</div>
                        <div class="node-id">Connection Lost</div>
                    </div>
                    <span class="status-badge offline">Offline</span>
                </div>
            </div>
        `;
        return;
    }

    const now = Date.now() / 1000;
    const handshakeAge = peer.latest_handshake > 0
        ? now - peer.latest_handshake
        : Infinity;

    const isStale = handshakeAge > CONFIG.thresholds.handshakeStale;
    const statusClass = isStale ? 'warning' : 'online';
    const statusText = isStale ? 'Stale' : 'Connected';

    container.innerHTML = `
        <div class="node-card ${statusClass}">
            <div class="node-header">
                <div class="node-info">
                    <div class="node-name">Orchestrator</div>
                    <div class="node-id">${peer.allowed_ips}</div>
                </div>
                <span class="status-badge ${statusClass}">${statusText}</span>
            </div>
            <div class="node-details">
                <div class="detail-item">
                    <span class="detail-label">Endpoint:</span>
                    <span class="detail-value">${peer.endpoint || 'Unknown'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Last Handshake:</span>
                    <span class="detail-value">${formatHandshake(handshakeAge)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Transfer:</span>
                    <span class="detail-value">↓ ${formatBytes(peer.rx_bytes)} / ↑ ${formatBytes(peer.tx_bytes)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Public Key:</span>
                    <span class="detail-value mono">${truncateKey(peer.public_key)}</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render host node connection (via relay metadata)
 */
function renderHostConnection() {
    const container = document.getElementById('host-connection');

    if (!state.statusData) {
        container.innerHTML = `
            <div class="node-card checking">
                <div class="node-header">
                    <div class="node-info">
                        <div class="node-name">Host Node</div>
                        <div class="node-id">Checking...</div>
                    </div>
                    <span class="status-badge checking">Checking</span>
                </div>
            </div>
        `;
        return;
    }

    // Determine host status based on relay uptime and system health
    const uptime = state.statusData.uptime_seconds || 0;
    const memoryPercent = state.statusData.memory_percent || 0;

    let statusClass = 'online';
    let statusText = 'Connected';

    if (uptime < 300) { // Less than 5 minutes - recently started
        statusClass = 'checking';
        statusText = 'Starting';
    } else if (memoryPercent > 90) { // High memory - potential issue
        statusClass = 'warning';
        statusText = 'High Load';
    }

    // Extract gateway/host info
    const relayName = state.statusData.relay_name || 'Unknown';
    const hostNode = relayName.includes('relay-') ? 'Host Node (Active)' : 'Host Node';

    container.innerHTML = `
        <div class="node-card ${statusClass}">
            <div class="node-header">
                <div class="node-info">
                    <div class="node-name">${hostNode}</div>
                    <div class="node-id">Bridge Network: 192.168.122.1</div>
                </div>
                <span class="status-badge ${statusClass}">${statusText}</span>
            </div>
            <div class="node-details">
                <div class="detail-item">
                    <span class="detail-label">VM Uptime:</span>
                    <span class="detail-value">${formatUptime(uptime)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Memory Usage:</span>
                    <span class="detail-value">${memoryPercent.toFixed(1)}%</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">CGNAT Nodes:</span>
                    <span class="detail-value">${state.statusData.cgnat_node_count ?? state.statusData.current_load ?? 0} / ${state.statusData.max_capacity || 10}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">System VMs:</span>
                    <span class="detail-value">${state.statusData.system_vm_count ?? 0} (total peers: ${state.statusData.total_peers ?? '?'})</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Cleanup:</span>
                    <span class="detail-value">${state.statusData.cleanup_config?.enabled ? 'Enabled' : 'Disabled'}</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render peers grouped by classification:
 * - CGNAT nodes (with their child system VMs nested underneath)
 * - Relay host infrastructure (host's own system VMs)
 */
function renderCGNATNodes() {
    if (!state.wireguardData) return;

    const peers = state.wireguardData.peers || [];
    const now = Date.now() / 1000;

    // Classify peers using server-provided peer_type field
    const cgnatPeers = peers.filter(p => p.peer_type === 'cgnat-node');
    const systemVmPeers = peers.filter(p => p.peer_type === 'system-vm');
    const orchestratorPeer = peers.find(p => p.peer_type === 'orchestrator' ||
        (p.allowed_ips && p.allowed_ips.includes('10.20.0.1')));

    // Group system VMs by parent_node_id
    const systemVmsByParent = {};
    systemVmPeers.forEach(p => {
        const parent = p.parent_node_id || 'unknown';
        if (!systemVmsByParent[parent]) systemVmsByParent[parent] = [];
        systemVmsByParent[parent].push(p);
    });

    // Separate host system VMs from CGNAT node system VMs
    const hostVms = systemVmsByParent[CONFIG.nodeId] || [];
    delete systemVmsByParent[CONFIG.nodeId];

    state.cgnatNodes = cgnatPeers;
    state.systemVms = systemVmPeers;
    state.hostSystemVms = hostVms;

    // Use cgnat_node_count from API (accurate, classification-based)
    const cgnatCount = state.wireguardData.cgnat_node_count ?? cgnatPeers.length;
    const systemVmCount = state.wireguardData.system_vm_count ?? systemVmPeers.length;

    // Update count badges
    document.getElementById('cgnat-count').textContent = cgnatCount;
    document.getElementById('cgnat-capacity').textContent = `${cgnatCount} / ${CONFIG.relayCapacity}`;

    // Render CGNAT nodes list
    const container = document.getElementById('cgnat-list');
    let html = '';

    // -- Relay Host Infrastructure section --
    if (hostVms.length > 0) {
        html += `<div class="peer-group-header">Relay Host Infrastructure</div>`;
        html += hostVms.map(vm => renderSystemVmCard(vm, now)).join('');
    }

    // -- CGNAT Nodes section --
    if (cgnatPeers.length === 0 && hostVms.length === 0) {
        html = '<p style="text-align: center; color: var(--text-muted); padding: 2rem;">No CGNAT nodes connected</p>';
    } else {
        if (cgnatPeers.length > 0) {
            html += `<div class="peer-group-header">CGNAT Nodes (${cgnatCount}/${CONFIG.relayCapacity})</div>`;
        }

        cgnatPeers.forEach((peer, index) => {
            html += renderCgnatNodeCard(peer, index, now);

            // Find and nest child system VMs for this CGNAT node
            // Match by checking if any system VM's parent_node_id appears in
            // the remaining systemVmsByParent groups. We identify the parent
            // by checking the CGNAT node's tunnel IP subnet match.
            const childVms = findChildSystemVms(peer, systemVmsByParent);
            if (childVms.length > 0) {
                html += `<div class="nested-vms">`;
                html += childVms.map(vm => renderSystemVmCard(vm, now)).join('');
                html += `</div>`;
            }
        });

        // Any remaining ungrouped system VMs (parent not matched to a CGNAT peer)
        const remainingVms = Object.values(systemVmsByParent).flat();
        if (remainingVms.length > 0) {
            html += `<div class="peer-group-header">System VMs (ungrouped)</div>`;
            html += remainingVms.map(vm => renderSystemVmCard(vm, now)).join('');
        }
    }

    container.innerHTML = html;

    // Update totals
    state.totalRx = peers.reduce((sum, p) => sum + (p.rx_bytes || 0), 0);
    state.totalTx = peers.reduce((sum, p) => sum + (p.tx_bytes || 0), 0);
}

/**
 * Find system VMs that are children of a CGNAT node peer.
 * Matches by parent_node_id stored in system VM metadata.
 * Removes matched VMs from the systemVmsByParent map.
 */
function findChildSystemVms(cgnatPeer, systemVmsByParent) {
    // Try matching by description — orchestrator sets CGNAT node description like
    // "CGNAT node <name> (<id>)". The system VM's parent_node_id is the node ID.
    // We scan all remaining groups and check if any parent makes sense.
    const children = [];

    for (const [parentId, vms] of Object.entries(systemVmsByParent)) {
        if (!parentId || parentId === 'unknown') continue;

        // Heuristic: CGNAT peer and its system VMs share the same subnet
        // e.g., CGNAT node at 10.20.1.3 and its DHT VM at 10.20.1.203
        const cgnatSubnet = extractSubnet(cgnatPeer.allowed_ips);
        const vmSubnet = vms.length > 0 ? extractSubnet(vms[0].allowed_ips) : null;

        if (cgnatSubnet && vmSubnet && cgnatSubnet === vmSubnet) {
            children.push(...vms);
            delete systemVmsByParent[parentId];
            break;
        }
    }

    return children;
}

/**
 * Extract subnet (first 3 octets) from allowed_ips like "10.20.1.3/32"
 */
function extractSubnet(allowedIps) {
    if (!allowedIps) return null;
    const ip = allowedIps.split('/')[0].split(',')[0].trim();
    const parts = ip.split('.');
    if (parts.length === 4) return `${parts[0]}.${parts[1]}.${parts[2]}`;
    return null;
}

/**
 * Render a single CGNAT node card
 */
function renderCgnatNodeCard(peer, index, now) {
    const handshakeAge = peer.latest_handshake > 0
        ? now - peer.latest_handshake
        : Infinity;

    let statusClass = 'online';
    let statusText = 'Active';

    if (handshakeAge === Infinity) {
        // No handshake yet — check if peer is genuinely new or just broken
        const regAge = peer.registration_age_seconds;  // null = predates tracking
        if (regAge !== null && regAge !== undefined && regAge < CONFIG.thresholds.handshakeGracePeriod) {
            statusClass = 'checking';
            statusText = 'New (Grace Period)';
        } else {
            statusClass = 'warning';
            statusText = 'No Handshake';
        }
    } else if (handshakeAge > CONFIG.thresholds.handshakeStale) {
        statusClass = 'warning';
        statusText = 'Stale';
    }

    return `
        <div class="node-card ${statusClass}">
            <div class="node-header">
                <div class="node-info">
                    <div class="node-name">CGNAT Node ${index + 1}</div>
                    <div class="node-id">${peer.allowed_ips || 'Unknown'}</div>
                </div>
                <span class="status-badge ${statusClass}">${statusText}</span>
            </div>
            <div class="node-details">
                <div class="detail-item">
                    <span class="detail-label">Endpoint:</span>
                    <span class="detail-value">${peer.endpoint || 'Pending'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Last Handshake:</span>
                    <span class="detail-value">${formatHandshake(handshakeAge)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Transfer:</span>
                    <span class="detail-value">${'\\u2193'} ${formatBytes(peer.rx_bytes)} / ${'\\u2191'} ${formatBytes(peer.tx_bytes)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Public Key:</span>
                    <span class="detail-value mono" title="${peer.public_key}">${truncateKey(peer.public_key)}</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render a system VM card (compact, nested under parent node)
 */
function renderSystemVmCard(peer, now) {
    const handshakeAge = peer.latest_handshake > 0
        ? now - peer.latest_handshake
        : Infinity;

    const isActive = handshakeAge < CONFIG.thresholds.handshakeWarning;
    const statusClass = isActive ? 'online' : (handshakeAge === Infinity ? 'checking' : 'warning');
    const statusText = isActive ? 'Active' : (handshakeAge === Infinity ? 'Connecting' : 'Stale');
    const desc = peer.description || 'System VM';
    const vmType = desc.toLowerCase().includes('dht') ? 'DHT' :
        desc.toLowerCase().includes('block') ? 'BlockStore' : 'System';

    return `
        <div class="node-card ${statusClass} system-vm-card">
            <div class="node-header">
                <div class="node-info">
                    <div class="node-name">${vmType} VM</div>
                    <div class="node-id">${peer.allowed_ips || 'Unknown'}</div>
                </div>
                <span class="status-badge ${statusClass} small">${statusText}</span>
            </div>
            <div class="node-details compact">
                <div class="detail-item">
                    <span class="detail-label">Handshake:</span>
                    <span class="detail-value">${formatHandshake(handshakeAge)}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Transfer:</span>
                    <span class="detail-value">${'\\u2193'} ${formatBytes(peer.rx_bytes)} / ${'\\u2191'} ${formatBytes(peer.tx_bytes)}</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render system metrics
 */
function renderSystemMetrics() {
    if (!state.statusData) return;

    const data = state.statusData;

    // Memory
    const memoryPercent = data.memory_percent || 0;
    updateMetric('memory-value', `${memoryPercent.toFixed(1)}%`);
    updateMetricBar('memory-bar', memoryPercent);

    // CPU (if available)
    const cpuPercent = data.cpu_percent || 0;
    updateMetric('cpu-value', `${cpuPercent.toFixed(1)}%`);
    updateMetricBar('cpu-bar', cpuPercent);

    // Uptime
    if (data.uptime_seconds) {
        updateMetric('uptime-value', formatUptime(data.uptime_seconds));
    }

    // Bandwidth
    updateMetric('bandwidth-rx-value', formatBytes(state.totalRx));
    updateMetric('bandwidth-tx-value', formatBytes(state.totalTx));
}

/**
 * Render WireGuard status
 */
function renderWireGuardStatus() {
    if (!state.wireguardData) return;

    const data = state.wireguardData;
    const cgnatCount = data.cgnat_node_count ?? 0;
    const systemVmCount = data.system_vm_count ?? 0;

    document.getElementById('wg-interface').textContent = data.interface || 'wg-relay-server';
    document.getElementById('wg-peers').textContent = `${data.peer_count || 0} (${cgnatCount} nodes, ${systemVmCount} VMs)`;
    document.getElementById('wg-capacity').textContent = data.max_capacity || CONFIG.relayCapacity;
    document.getElementById('wg-available').textContent = data.available_slots ?? 0;
}

/**
 * ✅ NEW: Render cleanup information
 */
function renderCleanupInfo() {
    const container = document.getElementById('cleanup-info');
    if (!container) return;

    if (!state.cleanupStats) {
        container.innerHTML = '<p style="color: var(--text-muted);">Cleanup info unavailable</p>';
        return;
    }

    const stats = state.cleanupStats.stats || {};
    const lastRun = stats.last_run ? new Date(stats.last_run * 1000) : null;

    container.innerHTML = `
        <div class="info-row">
            <span class="info-label">Grace Period:</span>
            <span class="info-value">${state.cleanupStats.grace_period_seconds}s</span>
        </div>
        <div class="info-row">
            <span class="info-label">Stale Threshold:</span>
            <span class="info-value">${state.cleanupStats.stale_threshold_seconds}s</span>
        </div>
        <div class="info-row">
            <span class="info-label">Last Cleanup:</span>
            <span class="info-value">${lastRun ? lastRun.toLocaleTimeString() : 'Never'}</span>
        </div>
        <div class="info-row">
            <span class="info-label">Total Removed:</span>
            <span class="info-value">${stats.total_removed || 0} peers</span>
        </div>
    `;
}

/**
 * Render last update time
 */
function renderLastUpdate() {
    if (!state.lastUpdate) return;

    const element = document.getElementById('last-update');
    const now = new Date();
    const seconds = Math.floor((now - state.lastUpdate) / 1000);

    if (seconds < 60) {
        element.textContent = `${seconds}s ago`;
    } else {
        element.textContent = state.lastUpdate.toLocaleTimeString();
    }
}

// ==================== Helper Functions ====================

/**
 * Update a metric value
 */
function updateMetric(elementId, value) {
    const element = document.getElementById(elementId);
    if (element) {
        element.textContent = value;
    }
}

/**
 * Update a metric bar
 */
function updateMetricBar(elementId, percent) {
    const element = document.getElementById(elementId);
    if (!element) return;

    const fill = element.querySelector('.metric-bar-fill');
    if (!fill) return;

    fill.style.width = `${Math.min(100, Math.max(0, percent))}%`;

    // Apply danger/warning classes
    fill.classList.remove('warning', 'danger');
    if (percent >= CONFIG.thresholds.memoryDanger) {
        fill.classList.add('danger');
    } else if (percent >= CONFIG.thresholds.memoryWarning) {
        fill.classList.add('warning');
    }
}

/**
 * Set status indicator
 */
function setStatus(dotElement, textElement, status, text) {
    if (dotElement) {
        dotElement.className = `status-dot ${status}`;
    }
    if (textElement) {
        textElement.textContent = text;
    }
}

/**
 * Format bytes to human-readable
 */
function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`;
}

/**
 * Format handshake age
 */
function formatHandshake(seconds) {
    if (!isFinite(seconds)) return 'Never';
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * Format uptime
 */
function formatUptime(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

/**
 * Truncate public key for display
 */
function truncateKey(key) {
    if (!key) return 'Unknown';
    if (key.length <= 20) return key;
    return `${key.substring(0, 16)}...${key.substring(key.length - 4)}`;
}

/**
 * Show alert banner
 */
function showAlert(message, type = 'warning') {
    const banner = document.getElementById('alert-banner');
    const messageEl = document.getElementById('alert-message');

    banner.className = `alert-banner ${type}`;
    messageEl.textContent = message;
    banner.style.display = 'flex';
}

/**
 * Close alert banner
 */
function closeAlert() {
    const banner = document.getElementById('alert-banner');
    banner.style.display = 'none';
}

/**
 * Hide loading overlay
 */
function hideLoadingOverlay() {
    const overlay = document.getElementById('loading-overlay');
    if (!overlay) return;

    overlay.classList.add('hidden');

    // Remove from DOM after transition
    setTimeout(() => {
        overlay.style.display = 'none';
    }, 300);
}

// ==================== Debug Functions ====================

/**
 * Log current state to console (for debugging)
 */
window.debugState = function () {
    console.log('=== Dashboard State ===');
    console.log('Version: 2.0.0 (PEER CLASSIFICATION)');
    console.log('Initialized:', state.initialized);
    console.log('Last Update:', state.lastUpdate);
    console.log('Update Count:', state.updateCount);
    console.log('Errors:', state.errors);
    console.log('Orchestrator Status:', state.orchestratorStatus);
    console.log('CGNAT Nodes:', state.cgnatNodes.length);
    console.log('Total RX:', formatBytes(state.totalRx));
    console.log('Total TX:', formatBytes(state.totalTx));
    console.log('WireGuard Data:', state.wireguardData);
    console.log('Status Data:', state.statusData);
    console.log('Cleanup Stats:', state.cleanupStats);
};

/**
 * Force dashboard update (for debugging)
 */
window.forceUpdate = function () {
    console.log('Forcing dashboard update...');
    updateDashboard();
};

// Log initialization
console.log('Dashboard script loaded (v2.0.0 - PEER CLASSIFICATION)');