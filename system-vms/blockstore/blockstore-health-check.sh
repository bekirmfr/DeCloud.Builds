#!/bin/bash
#
# DeCloud Block Store Health Check
# Called by systemd as the service health check.
# Exits 0 if healthy, non-zero otherwise.

set -euo pipefail

API_PORT="${BLOCKSTORE_API_PORT:-5090}"
TIMEOUT=5

HEALTH=$(curl -s --max-time "$TIMEOUT" "http://127.0.0.1:${API_PORT}/health" 2>/dev/null) || {
    echo "Block store API not responding on port $API_PORT"
    exit 1
}

STATUS=$(echo "$HEALTH" | jq -r '.status // "unknown"' 2>/dev/null)
PEER_ID=$(echo "$HEALTH" | jq -r '.peerId // ""' 2>/dev/null)
PEERS=$(echo "$HEALTH" | jq -r '.connectedPeers // 0' 2>/dev/null)
USAGE=$(echo "$HEALTH" | jq -r '.usagePercent // 0' 2>/dev/null)

if [ "$STATUS" != "ok" ]; then
    echo "Block store status: $STATUS"
    exit 1
fi

echo "Block store healthy — peerId=${PEER_ID:0:12}... peers=$PEERS usage=${USAGE}%"
exit 0
