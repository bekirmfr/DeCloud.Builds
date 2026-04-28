#!/bin/bash
#
# DeCloud DHT Bootstrap Peer Polling
# Version: 1.0
#
# Runs as a long-lived systemd service inside the DHT VM.
# Polls the orchestrator's /api/dht/join endpoint to:
#   1. Register this node's DHT peer ID (so the orchestrator knows us)
#   2. Receive bootstrap peers to connect to (so we join the network)
#
# Auth: HMAC-SHA256(auth_token, nodeId:vmId) — same pattern as relay's
#       notify-orchestrator.sh uses HMAC(wireguard_private_key, nodeId:vmId).
#
# This replaces the previous flow where PeerId propagated via:
#   dht-notify-ready.sh → NodeAgent → heartbeat → orchestrator
# Now the DHT VM talks to the orchestrator directly.
#

set -euo pipefail

LOG_FILE="/var/log/decloud-dht-bootstrap-poll.log"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# =====================================================
# Configuration (injected by cloud-init)
# =====================================================
ORCHESTRATOR_URL="__ORCHESTRATOR_URL__"
NODE_ID="__NODE_ID__"
VM_ID="__VM_ID__"
API_PORT="__DHT_API_PORT__"

# Auth token from NodeAgent obligation state — persistent across redeployments.
# Queried live so the token is always current even after NodeAgent key rotation.
GATEWAY=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
NODE_AGENT="http://${GATEWAY}:5100"

log "Fetching auth token from NodeAgent obligation state..."
AUTH_TOKEN=""
for i in $(seq 1 24); do  # up to 2 minutes
    AUTH_TOKEN=$(curl -sf --max-time 5 \
        "${NODE_AGENT}/api/obligations/dht/state" 2>/dev/null \
        | jq -r '.authToken // empty' 2>/dev/null || true)
    [ -n "$AUTH_TOKEN" ] && break
    sleep 5
done
if [ -z "$AUTH_TOKEN" ]; then
    log "ERROR: Could not fetch auth token from NodeAgent after 2 minutes"
    exit 1
fi

POLL_INTERVAL_ISOLATED=15    # seconds between polls when no peers
POLL_INTERVAL_CONNECTED=300  # seconds between polls when connected (maintenance)
MAX_CONNECT_FAILURES=0       # track consecutive orchestrator failures

log "Starting DHT bootstrap poll service"
log "  Orchestrator: $ORCHESTRATOR_URL"
log "  Node ID:      $NODE_ID"
log "  VM ID:        $VM_ID"
log "  DHT API port: $API_PORT"

# =====================================================
# Wait for DHT binary to start
# =====================================================
log "Waiting for DHT binary to start..."
PEER_ID=""

for i in {1..60}; do
    HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || true

    if [ -n "$HEALTH" ]; then
        PEER_ID=$(echo "$HEALTH" | jq -r '.peerId // empty' 2>/dev/null) || true

        if [ -n "$PEER_ID" ]; then
            log "DHT binary started - peer ID: $PEER_ID"
            break
        fi
    fi

    if [ $((i % 10)) -eq 0 ]; then
        log "  Still waiting for DHT binary... (attempt $i/60)"
    fi
    sleep 2
done

if [ -z "$PEER_ID" ]; then
    log "ERROR: Failed to get peer ID after 120 seconds"
    exit 1
fi

# =====================================================
# Compute authentication token (relay pattern)
# HMAC-SHA256(auth_token, nodeId:vmId)
# =====================================================
compute_token() {
    local message="${NODE_ID}:${VM_ID}"
    echo -n "$message" | openssl dgst -sha256 -hmac "$AUTH_TOKEN" -binary | base64
}

TOKEN=$(compute_token)

# =====================================================
# Main polling loop
# =====================================================
log "Entering bootstrap poll loop..."
CONSECUTIVE_FAILURES=0
# Always poll orchestrator at least once on startup, regardless of peer count.
# Without this, if the DHT binary connects to a stale baked-in bootstrap peer
# on startup, CONNECTED > 0 causes an immediate 300s back-off — the node never
# calls /api/dht/join, never re-registers its PeerId, and never receives the
# current peer list. Other nodes remain undiscovered until the 300s timer fires.
INITIAL_POLL_DONE=false

while true; do
    # Check current peer count
    HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || true
    CONNECTED=$(echo "$HEALTH" | jq -r '.connectedPeers // 0' 2>/dev/null) || CONNECTED=0

    if [ "$CONNECTED" -gt 0 ] && [ "$INITIAL_POLL_DONE" = "true" ]; then
        # Back off only if the Kademlia routing table is populated.
        # connectedPeers includes blockstores that dial into us — they are not
        # routing table participants. If routingTable.size == 0 we have no DHT
        # peers and must re-poll the orchestrator to reconnect.
        ROUTING_SIZE=$(echo "$HEALTH" | jq -r '.routingTable // 0' 2>/dev/null) || ROUTING_SIZE=0
        if [ "$ROUTING_SIZE" -gt 0 ]; then
            if [ "$CONSECUTIVE_FAILURES" -gt 0 ]; then
                log "Connected to $CONNECTED peer(s), routingTable=$ROUTING_SIZE — resuming maintenance polling"
                CONSECUTIVE_FAILURES=0
            fi
            sleep "$POLL_INTERVAL_CONNECTED"
            continue
        fi
        log "routingTable empty (size=0) despite $CONNECTED connected peer(s) — re-polling orchestrator for DHT bootstrap peers"
    fi

    if [ "$CONNECTED" -gt 0 ]; then
        log "Connected to $CONNECTED peer(s) — performing initial orchestrator poll to register PeerId and discover current peers..."
    else
        log "No connected peers — polling orchestrator for bootstrap peers..."
    fi
    INITIAL_POLL_DONE=true

    # Call orchestrator /api/dht/join
    RESPONSE=$(curl -X POST "${ORCHESTRATOR_URL}/api/dht/join" \
        -H "Content-Type: application/json" \
        -H "X-DHT-Token: $TOKEN" \
        -d "{
            \"nodeId\": \"$NODE_ID\",
            \"vmId\": \"$VM_ID\",
            \"peerId\": \"$PEER_ID\"
        }" \
        --max-time 10 \
        -s \
        -w "\nHTTP_CODE:%{http_code}" \
        2>&1) || true

    HTTP_CODE=$(echo "$RESPONSE" | grep -oP 'HTTP_CODE:\K\d+' || echo "000")
    BODY=$(echo "$RESPONSE" | sed '/HTTP_CODE:/d')

    if [ "$HTTP_CODE" = "200" ]; then
        CONSECUTIVE_FAILURES=0

        # Extract bootstrap peers from response
        PEER_COUNT=$(echo "$BODY" | jq -r '.bootstrapPeers | length // 0' 2>/dev/null) || PEER_COUNT=0

        if [ "$PEER_COUNT" -gt 0 ]; then
            log "Received $PEER_COUNT bootstrap peer(s) from orchestrator"

            # Build JSON array for POST /connect
            PEERS_JSON=$(echo "$BODY" | jq -c '.bootstrapPeers' 2>/dev/null)

            # Inject peers via DHT binary's POST /connect endpoint (localhost-only)
            CONNECT_RESULT=$(curl -X POST "http://127.0.0.1:${API_PORT}/connect" \
                -H "Content-Type: application/json" \
                -d "{\"peers\": $PEERS_JSON}" \
                --max-time 10 \
                -s \
                2>/dev/null) || true

            CONNECTED_COUNT=$(echo "$CONNECT_RESULT" | jq -r '.connected // 0' 2>/dev/null) || CONNECTED_COUNT=0
            log "Connected to $CONNECTED_COUNT/$PEER_COUNT bootstrap peer(s)"
        else
            log "Orchestrator returned 0 bootstrap peers (may be the first node)"
        fi
    elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
        log "ERROR: Authentication failed (HTTP $HTTP_CODE) — check DHT_AUTH_TOKEN"
        # Don't spam on auth failures — back off significantly
        sleep 60
        continue
    else
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        if [ "$CONSECUTIVE_FAILURES" -le 3 ] || [ $((CONSECUTIVE_FAILURES % 20)) -eq 0 ]; then
            log "WARNING: Orchestrator unreachable (HTTP ${HTTP_CODE:-timeout}, failures: $CONSECUTIVE_FAILURES)"
        fi
    fi

    sleep "$POLL_INTERVAL_ISOLATED"
done
