#!/bin/bash
#
# DeCloud DHT Node Ready Callback (NodeAgent)
# Version: 1.1
#
# BACKUP PATH: Notifies the NodeAgent of the DHT peer ID so it can
# include it in heartbeat reports. This is a secondary registration path.
#
# PRIMARY PATH: dht-bootstrap-poll.sh registers the peerId directly
# with the orchestrator via POST /api/dht/join (relay callback pattern).
#
# This callback is kept for:
# - Heartbeat reporting (NodeAgent needs peerId in VM service StatusMessage)
# - Backward compatibility if orchestrator /api/dht/join is not yet deployed
#

set -e

LOG_FILE="/var/log/decloud-dht-callback.log"
MARKER_FILE="/var/lib/decloud-dht/callback-complete"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# =====================================================
# Check if callback already completed
# =====================================================
if [ -f "$MARKER_FILE" ]; then
    log "DHT callback already completed - skipping"
    exit 0
fi

log "Starting DHT ready callback..."

API_PORT="__DHT_API_PORT__"
VM_ID="__VM_ID__"

# =====================================================
# Detect gateway IP (node agent host)
# =====================================================
GATEWAY_IP=$(ip route | grep default | awk '{print $3}' | head -1)
if [ -z "$GATEWAY_IP" ]; then
    GATEWAY_IP="192.168.122.1"
fi

NODE_AGENT_URL="http://${GATEWAY_IP}:5100"
log "Node agent URL: $NODE_AGENT_URL"

# =====================================================
# Wait for DHT binary to start and obtain peer ID
# =====================================================
log "Waiting for DHT node to start..."
PEER_ID=""

for i in {1..60}; do
    HEALTH=$(curl -s --max-time 3 "http://127.0.0.1:${API_PORT}/health" 2>/dev/null)
    if [ $? -eq 0 ]; then
        PEER_ID=$(echo "$HEALTH" | jq -r '.peerId' 2>/dev/null)
        CONNECTED=$(echo "$HEALTH" | jq -r '.connectedPeers' 2>/dev/null)

        if [ -n "$PEER_ID" ] && [ "$PEER_ID" != "null" ]; then
            log "DHT node started - peer ID: $PEER_ID (connected peers: $CONNECTED)"
            echo "$PEER_ID" > /var/lib/decloud-dht/peer-id
            break
        fi
    fi

    if [ $((i % 10)) -eq 0 ]; then
        log "  Still waiting for DHT node... (attempt $i/60)"
    fi
    sleep 2
done

if [ -z "$PEER_ID" ] || [ "$PEER_ID" = "null" ]; then
    log "Failed to get peer ID after 120 seconds"
    exit 1
fi

# =====================================================
# Verify node agent is reachable
# =====================================================
log "Checking if node agent is reachable..."
for i in {1..12}; do
    if curl -s -m 2 "$NODE_AGENT_URL/health" >/dev/null 2>&1; then
        log "Node agent is reachable"
        break
    fi

    if [ $i -eq 12 ]; then
        log "Node agent not reachable after 60 seconds - will retry"
        exit 1
    fi

    sleep 5
done

# =====================================================
# Compute authentication token
# =====================================================
MACHINE_ID="__HOST_MACHINE_ID__"
MESSAGE="${VM_ID}:${PEER_ID}"
TOKEN=$(echo -n "$MESSAGE" | openssl dgst -sha256 -hmac "$MACHINE_ID" -binary | base64)

# =====================================================
# Notify node agent with our peer ID
# =====================================================
log "Notifying node agent of DHT peer ID..."

RESPONSE=$(curl -X POST "$NODE_AGENT_URL/api/dht/ready" \
    -H "Content-Type: application/json" \
    -H "X-DHT-Token: $TOKEN" \
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

HTTP_CODE=$(echo "$RESPONSE" | grep -oP 'HTTP_CODE:\K\d+')

if [ "$HTTP_CODE" = "200" ]; then
    log "Successfully notified node agent - DHT node is active"
    mkdir -p "$(dirname "$MARKER_FILE")"
    echo "DHT callback completed at $(date) - peer ID: $PEER_ID" > "$MARKER_FILE"
    logger -t decloud-dht "DHT node active with peer ID $PEER_ID"
    exit 0
else
    log "Failed to notify node agent (HTTP ${HTTP_CODE:-timeout})"
    log "Response: $RESPONSE"
    exit 1
fi
