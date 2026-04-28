#!/bin/bash
#
# DeCloud Block Store Ready Callback (NodeAgent)
# Version: 1.0
#
# Notifies the NodeAgent of the block store peer ID so it can
# mark the VM's System service as Ready and include peerId in
# heartbeat reports.
#
# Authentication: HMAC-SHA256(machineId, vmId:peerId)
# Endpoint:       POST /api/blockstore/ready  (NodeAgent :5100)
#
# PRIMARY registration path: blockstore-bootstrap-poll.sh registers
# the peerId directly with the orchestrator via POST /api/blockstore/join.
# This callback handles the NodeAgent side (readiness tracking).

set -e

LOG_FILE="/var/log/decloud-blockstore-callback.log"
MARKER_FILE="/var/lib/decloud-blockstore/callback-complete"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ═══════════════════════════════════════════════════════════════════
# Idempotency guard
# ═══════════════════════════════════════════════════════════════════
if [ -f "$MARKER_FILE" ]; then
    log "Block store callback already completed — skipping"
    exit 0
fi

log "Starting block store ready callback..."

API_PORT="__BLOCKSTORE_API_PORT__"
VM_ID="__VM_ID__"

# ═══════════════════════════════════════════════════════════════════
# Detect NodeAgent host (default gateway via virbr0)
# ═══════════════════════════════════════════════════════════════════
GATEWAY_IP=$(ip route | grep default | awk '{print $3}' | head -1)
if [ -z "$GATEWAY_IP" ]; then
    GATEWAY_IP="192.168.122.1"
fi
NODE_AGENT_URL="http://${GATEWAY_IP}:5100"
log "Node agent URL: $NODE_AGENT_URL"

# ═══════════════════════════════════════════════════════════════════
# Wait for block store binary to start and obtain peer ID
# ═══════════════════════════════════════════════════════════════════
log "Waiting for block store node to start..."
PEER_ID=""

for i in $(seq 1 60); do
    HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || true

    if [ $? -eq 0 ]; then
        PEER_ID=$(echo "$HEALTH" | jq -r '.peerId // ""' 2>/dev/null)

        if [ -n "$PEER_ID" ] && [ "$PEER_ID" != "null" ]; then
            CONNECTED=$(echo "$HEALTH" | jq -r '.connectedPeers // 0' 2>/dev/null)
            log "Block store node started — peer ID: $PEER_ID (connected peers: $CONNECTED)"
            echo "$PEER_ID" > /var/lib/decloud-blockstore/peer-id
            break
        fi
    fi

    if [ $((i % 10)) -eq 0 ]; then
        log "  Still waiting for block store node... (attempt $i/60)"
    fi
    sleep 2
done

if [ -z "$PEER_ID" ] || [ "$PEER_ID" = "null" ]; then
    log "Failed to get peer ID after 120 seconds"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════
# Verify NodeAgent is reachable
# ═══════════════════════════════════════════════════════════════════
log "Checking if node agent is reachable..."
for i in $(seq 1 12); do
    if curl -s -m 2 "$NODE_AGENT_URL/health" >/dev/null 2>&1; then
        log "Node agent is reachable"
        break
    fi
    if [ "$i" -eq 12 ]; then
        log "Node agent not reachable after 60 seconds — will retry"
        exit 1
    fi
    sleep 5
done

# ═══════════════════════════════════════════════════════════════════
# Compute HMAC-SHA256 authentication token
# Token: HMAC-SHA256(machineId, vmId:peerId)
# ═══════════════════════════════════════════════════════════════════
MACHINE_ID="__HOST_MACHINE_ID__"
MESSAGE="${VM_ID}:${PEER_ID}"
TOKEN=$(echo -n "$MESSAGE" | openssl dgst -sha256 -hmac "$MACHINE_ID" -binary | base64)

# ═══════════════════════════════════════════════════════════════════
# Notify NodeAgent
# ═══════════════════════════════════════════════════════════════════
log "Notifying node agent of block store peer ID..."

RESPONSE=$(curl -X POST "$NODE_AGENT_URL/api/blockstore/ready" \
    -H "Content-Type: application/json" \
    -H "X-BlockStore-Token: $TOKEN" \
    -d "{
        \"vmId\": \"$VM_ID\",
        \"peerId\": \"$PEER_ID\"
    }" \
    --max-time 10 \
    --retry 2 \
    --retry-delay 5 \
    -w "\nHTTP_CODE:%{http_code}" \
    -s \
    2>&1)

HTTP_CODE=$(echo "$RESPONSE" | grep -oE 'HTTP_CODE:[0-9]+' | cut -d: -f2)

if [ "$HTTP_CODE" = "200" ]; then
    log "Successfully notified node agent — block store node is active"
    mkdir -p "$(dirname "$MARKER_FILE")"
    echo "Block store callback completed at $(date) - peer ID: $PEER_ID" > "$MARKER_FILE"
    logger -t decloud-blockstore "Block store node active with peer ID $PEER_ID"
    exit 0
else
    log "Failed to notify node agent (HTTP ${HTTP_CODE:-timeout})"
    log "Response: $RESPONSE"
    exit 1
fi
