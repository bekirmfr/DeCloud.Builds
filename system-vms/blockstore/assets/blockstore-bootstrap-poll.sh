#!/bin/bash
#
# DeCloud Block Store Bootstrap Peer Polling
# Version: 2.0
#
# Two responsibilities, two authorities:
#   • Local sibling (co-located DHT)   ← NodeAgent /api/system-vms/dht/peer-info
#   • Remote blockstores (federation)  ← Orchestrator /api/blockstore/join
#
# Both flow through the binary's idempotent /connect endpoint, so repeating
# the calls every poll iteration is safe and self-healing.
#
# Authentication: HMAC-SHA256(authToken, nodeId:vmId)
# Endpoint:       POST /api/blockstore/join  (Orchestrator)

set -euo pipefail

LOG_FILE="/var/log/decloud-blockstore-bootstrap.log"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ═══════════════════════════════════════════════════════════════════
# Configuration (baked in by cloud-init)
# ═══════════════════════════════════════════════════════════════════
source /etc/decloud-blockstore/blockstore.env 2>/dev/null || true
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-}"
NODE_ID="${BLOCKSTORE_NODE_ID:-}"
VM_ID="${BLOCKSTORE_VM_ID:-}"
API_PORT="${BLOCKSTORE_API_PORT:-5090}"

GATEWAY=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
NODE_AGENT="http://${GATEWAY}:5100"

log "Fetching auth token from NodeAgent obligation state..."
AUTH_TOKEN=""
for i in $(seq 1 24); do
    AUTH_TOKEN=$(curl -sf --max-time 5 \
        "${NODE_AGENT}/api/obligations/blockstore/state" 2>/dev/null \
        | jq -r '.authToken // empty' 2>/dev/null || true)
    [ -n "$AUTH_TOKEN" ] && break
    sleep 5
done
if [ -z "$AUTH_TOKEN" ]; then
    log "ERROR: Could not fetch auth token from NodeAgent after 2 minutes"
    exit 1
fi

POLL_INTERVAL_ISOLATED=30   # seconds between polls when no remote peers
POLL_INTERVAL_CONNECTED=60  # seconds between polls when connected

# ═══════════════════════════════════════════════════════════════════
# Wait for block store binary to start
# ═══════════════════════════════════════════════════════════════════
log "Waiting for block store binary to start..."
PEER_ID=""

for i in $(seq 1 60); do
    if [ -f "/var/lib/decloud-blockstore/peer-id" ]; then
        PEER_ID=$(cat /var/lib/decloud-blockstore/peer-id 2>/dev/null | tr -d '\n')
    fi

    if [ -z "$PEER_ID" ] || [ "$PEER_ID" = "null" ]; then
        HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || true
        PEER_ID=$(echo "$HEALTH" | jq -r '.peerId // ""' 2>/dev/null) || true
    fi

    if [ -n "$PEER_ID" ] && [ "$PEER_ID" != "null" ]; then
        log "Block store binary started — peer ID: $PEER_ID"
        break
    fi

    if [ $((i % 10)) -eq 0 ]; then
        log "  Waiting for binary... (attempt $i/60)"
    fi
    sleep 2
done

if [ -z "$PEER_ID" ] || [ "$PEER_ID" = "null" ]; then
    log "Block store binary did not start within 120 seconds"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════
# Compute HMAC-SHA256 authentication token
# ═══════════════════════════════════════════════════════════════════
compute_token() {
    local message="${NODE_ID}:${VM_ID}"
    echo -n "$message" | openssl dgst -sha256 -hmac "$AUTH_TOKEN" -binary | base64
}

TOKEN=$(compute_token)

# ═══════════════════════════════════════════════════════════════════
# Local sibling discovery — connect to co-located DHT via NodeAgent.
#
# The NodeAgent is the authoritative source for "what's running on this
# host"; the orchestrator only knows about cross-node federation. 404 from
# the NodeAgent means the sibling is still booting — silent retry next
# iteration. The binary's /connect endpoint is idempotent, so calling it
# every iteration costs nothing and self-heals after sibling redeploys.
# ═══════════════════════════════════════════════════════════════════
LOCAL_DHT_CONNECTED=false

connect_local_dht() {
    local response peer_id ip port multiaddr
    response=$(curl -sf --max-time 3 \
        "${NODE_AGENT}/api/system-vms/dht/peer-info" 2>/dev/null) || return 0

    peer_id=$(echo "$response" | jq -r '.peerId // empty' 2>/dev/null)
    ip=$(echo      "$response" | jq -r '.ipAddress // empty' 2>/dev/null)
    port=$(echo    "$response" | jq -r '.port // empty' 2>/dev/null)

    if [ -z "$peer_id" ] || [ -z "$ip" ] || [ -z "$port" ]; then
        return 0
    fi

    multiaddr="/ip4/${ip}/tcp/${port}/p2p/${peer_id}"

    # Idempotent — /connect is a no-op if already connected.
    curl -s -X POST "http://127.0.0.1:${API_PORT}/connect" \
        -H "Content-Type: application/json" \
        -d "{\"peers\":[\"${multiaddr}\"]}" \
        --max-time 5 >/dev/null 2>&1 || return 0

    if [ "$LOCAL_DHT_CONNECTED" != "true" ]; then
        log "Connected to local DHT sibling at ${ip}:${port}"
        LOCAL_DHT_CONNECTED=true
    fi
}

# ═══════════════════════════════════════════════════════════════════
# Main polling loop
# ═══════════════════════════════════════════════════════════════════
log "Entering bootstrap poll loop..."
CONSECUTIVE_FAILURES=0
INITIAL_POLL_DONE=false

while true; do
    # ── 1. Always (re)connect local sibling — idempotent, ~ms latency ──
    connect_local_dht

    # ── 2. Check current peer count from binary ────────────────────────
    HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || true
    CONNECTED=$(echo "$HEALTH" | jq -r '.connectedPeers // 0' 2>/dev/null) || CONNECTED=0

    # ── 3. If we have remote blockstore peers (more than just local DHT)
    #       and DHT routing is healthy, back off to maintenance polling.
    REMOTE_BS_CONNECTED=false
    if [ "$CONNECTED" -gt 1 ]; then
        REMOTE_BS_CONNECTED=true
    fi

    if [ "$INITIAL_POLL_DONE" = "true" ] && [ "$REMOTE_BS_CONNECTED" = "true" ]; then
        # Verify DHT announces are actually working — connectedPeers alone
        # can be misleading if local DHT is up but remote DHTs are unreachable.
        DIAG=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/diagnostics" 2>/dev/null) || DIAG="{}"
        DHT_OK=$(echo "$DIAG"   | jq -r '.dhtAnnounce.success // 0' 2>/dev/null) || DHT_OK=0
        DHT_FAIL=$(echo "$DIAG" | jq -r '.dhtAnnounce.fail    // 0' 2>/dev/null) || DHT_FAIL=0

        if [ "$DHT_FAIL" -gt 0 ] && [ "$DHT_OK" -eq 0 ]; then
            log "DHT routing table empty (fail=$DHT_FAIL, ok=$DHT_OK) — re-polling orchestrator"
            # Fall through to re-poll
        else
            if [ "$CONSECUTIVE_FAILURES" -gt 0 ]; then
                log "Connected to $CONNECTED peer(s) — resuming maintenance polling"
                CONSECUTIVE_FAILURES=0
            fi
            sleep "$POLL_INTERVAL_CONNECTED"
            continue
        fi
    fi

    if [ "$CONNECTED" -gt 0 ]; then
        log "Connected to $CONNECTED peer(s) — polling orchestrator for remote blockstores..."
    else
        log "No connected peers — polling orchestrator for bootstrap peers..."
    fi
    INITIAL_POLL_DONE=true

    # ── 4. Refresh advertise IP from watcher-owned environment file. ────
    # BLOCKSTORE_ADVERTISE_IP is a Dynamic (Restart) variable, populated by
    # the watcher from /api/obligations/blockstore/environment.
    source /etc/decloud-blockstore/environment 2>/dev/null || true
    ADVERTISE_IP="${BLOCKSTORE_ADVERTISE_IP:-}"
    if ! echo "${ADVERTISE_IP}" | grep -qE '^10\.20\.'; then
        log "WARN: BLOCKSTORE_ADVERTISE_IP='${ADVERTISE_IP}' is not a WireGuard mesh IP — skipping join"
        sleep "$POLL_INTERVAL_ISOLATED"
        continue
    fi

    # ── 5. Call orchestrator /api/blockstore/join ─────────────────────
    RESPONSE=$(curl -X POST "${ORCHESTRATOR_URL}/api/blockstore/join" \
        -H "Content-Type: application/json" \
        -H "X-BlockStore-Token: $TOKEN" \
        -d "{\"nodeId\":\"$NODE_ID\",\"vmId\":\"$VM_ID\",\"peerId\":\"$PEER_ID\",\"advertiseIp\":\"$ADVERTISE_IP\"}" \
        --max-time 10 \
        -s \
        -w "\nHTTP_CODE:%{http_code}" \
        2>&1) || true

    HTTP_CODE=$(echo "$RESPONSE" | grep -oE 'HTTP_CODE:[0-9]+' | cut -d: -f2 || echo "000")
    BODY=$(echo "$RESPONSE" | sed '/HTTP_CODE:/d')

    if [ "$HTTP_CODE" = "200" ]; then
        CONSECUTIVE_FAILURES=0
        PEER_COUNT=$(echo "$BODY" | jq '.bootstrapPeers | length' 2>/dev/null || echo 0)

        log "Orchestrator returned $PEER_COUNT remote bootstrap peer(s)"

        if [ "$PEER_COUNT" -gt 0 ]; then
            PEERS_JSON=$(echo "$BODY" | jq '.bootstrapPeers')
            CONNECT_RESP=$(curl -s -X POST "http://127.0.0.1:${API_PORT}/connect" \
                -H "Content-Type: application/json" \
                -d "{\"peers\": $PEERS_JSON}" \
                --max-time 15 2>/dev/null) || true

            CONNECTED_COUNT=$(echo "$CONNECT_RESP" | jq -r '.connected // 0' 2>/dev/null) || CONNECTED_COUNT=0
            log "Connected to $CONNECTED_COUNT/$PEER_COUNT remote bootstrap peer(s)"
        else
            log "No remote blockstores yet — will retry in ${POLL_INTERVAL_ISOLATED}s"
        fi
    elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
        log "ERROR: Authentication failed (HTTP $HTTP_CODE) — check blockstore auth token"
        sleep 60
        continue
    else
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        log "Orchestrator join failed (HTTP ${HTTP_CODE:-timeout}), failures: $CONSECUTIVE_FAILURES"

        # Exponential backoff capped at 5 minutes
        BACKOFF=$((POLL_INTERVAL_ISOLATED * CONSECUTIVE_FAILURES))
        BACKOFF=$((BACKOFF > 300 ? 300 : BACKOFF))
        sleep "$BACKOFF"
        continue
    fi

    sleep "$POLL_INTERVAL_ISOLATED"
done