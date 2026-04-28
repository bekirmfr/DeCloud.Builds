#!/bin/bash
#
# DeCloud WireGuard Config Fetch (Building Block)
# Version: 1.0
#
# Polls the NodeAgent for WireGuard relay connection parameters.
# Used by DHT and BlockStore VMs when CgnatInfo was not yet available
# at deploy time. Writes /etc/decloud/wg-mesh.env then runs wg-mesh-enroll.sh.
#
# Required environment variables:
#   DECLOUD_ROLE    — "dht" or "blockstore"
#   NODE_ID         — this node's ID (for WG description)
#   VM_ID           — this VM's ID (for WG description)
#
# Optional:
#   WG_CONFIG_MAX_WAIT   — timeout in seconds (default: 600)
#   WG_CONFIG_INTERVAL   — poll interval in seconds (default: 15)
#

set -uo pipefail

LOG_TAG="wg-config-fetch"
LOG_FILE="/var/log/wg-config-fetch.log"
log()     { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LOG_TAG] $*" | tee -a "$LOG_FILE"; }
log_err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LOG_TAG] ERROR: $*" | tee -a "$LOG_FILE" >&2; }

ROLE="${DECLOUD_ROLE:?DECLOUD_ROLE must be set (dht or blockstore)}"
NODE_ID="${NODE_ID:-unknown}"
VM_ID="${VM_ID:-unknown}"
MAX_WAIT="${WG_CONFIG_MAX_WAIT:-600}"
INTERVAL="${WG_CONFIG_INTERVAL:-15}"

# Discover NodeAgent via virbr0 default gateway
GATEWAY=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
if [ -z "$GATEWAY" ]; then
    log_err "Could not determine default gateway — cannot reach NodeAgent"
    exit 1
fi
NODE_AGENT="http://${GATEWAY}:5100"

log "Fetching WireGuard config for role=${ROLE} from ${NODE_AGENT}"

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
    HTTP_CODE=$(curl -s -o /tmp/wg-config-response.json \
        -w "%{http_code}" \
        --connect-timeout 5 \
        --max-time 10 \
        "${NODE_AGENT}/api/obligations/${ROLE}/wg-config" \
        2>/dev/null || echo "000")

    if [ "$HTTP_CODE" = "200" ]; then
        RELAY_ENDPOINT=$(jq -r '.relayEndpoint  // empty' /tmp/wg-config-response.json 2>/dev/null)
        RELAY_PUBKEY=$(jq   -r '.relayPublicKey // empty' /tmp/wg-config-response.json 2>/dev/null)
        RELAY_API=$(jq      -r '.relayApiUrl    // empty' /tmp/wg-config-response.json 2>/dev/null)
        TUNNEL_IP=$(jq      -r '.tunnelIp       // empty' /tmp/wg-config-response.json 2>/dev/null)

        if [ -n "$RELAY_ENDPOINT" ] && [ -n "$RELAY_PUBKEY" ] && [ -n "$TUNNEL_IP" ]; then
            log "✓ Got WG config: endpoint=${RELAY_ENDPOINT}, tunnel=${TUNNEL_IP}"

            # Write wg-mesh.env — consumed by wg-mesh-enroll.sh
            mkdir -p /etc/decloud
            cat > /etc/decloud/wg-mesh.env <<EOF
WG_RELAY_ENDPOINT=${RELAY_ENDPOINT}
WG_RELAY_PUBKEY=${RELAY_PUBKEY}
WG_TUNNEL_IP=${TUNNEL_IP}
WG_RELAY_API=${RELAY_API:-}
WG_INTERFACE=wg-mesh
WG_PEER_TYPE=system-vm
WG_PARENT_NODE_ID=${NODE_ID}
WG_DESCRIPTION=${ROLE}-${VM_ID}
EOF
            chmod 600 /etc/decloud/wg-mesh.env
            log "Wrote /etc/decloud/wg-mesh.env"

            # Update advertise IP in role-specific env file
            TUNNEL_IP_BARE="${TUNNEL_IP%%/*}"
            if [ -f /etc/decloud-blockstore/blockstore.env ]; then
                sed -i "s|^BLOCKSTORE_ADVERTISE_IP=.*|BLOCKSTORE_ADVERTISE_IP=${TUNNEL_IP_BARE}|" \
                    /etc/decloud-blockstore/blockstore.env
                log "Updated BLOCKSTORE_ADVERTISE_IP → ${TUNNEL_IP_BARE}"
            fi
            if [ -f /etc/decloud-dht/dht.env ]; then
                sed -i "s|^DHT_ADVERTISE_IP=.*|DHT_ADVERTISE_IP=${TUNNEL_IP_BARE}|" \
                    /etc/decloud-dht/dht.env
                log "Updated DHT_ADVERTISE_IP → ${TUNNEL_IP_BARE}"
            fi

            # Run WireGuard mesh enrollment
            log "Running wg-mesh-enroll.sh..."
            if /usr/local/bin/wg-mesh-enroll.sh; then
                log "✓ WireGuard mesh enrollment complete"
                exit 0
            else
                log_err "wg-mesh-enroll.sh failed — will retry"
                sleep "$INTERVAL"
                elapsed=$((elapsed + INTERVAL))
                continue
            fi
        else
            log "Response missing fields (endpoint=${RELAY_ENDPOINT}, tunnel=${TUNNEL_IP}) — retrying"
        fi
    elif [ "$HTTP_CODE" = "202" ]; then
        log "Relay not yet assigned (HTTP 202) — retrying in ${INTERVAL}s (${elapsed}/${MAX_WAIT}s elapsed)"
    else
        log "NodeAgent not ready (HTTP ${HTTP_CODE}) — retrying in ${INTERVAL}s (${elapsed}/${MAX_WAIT}s elapsed)"
    fi

    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
done

log_err "Timed out waiting for WireGuard config after ${MAX_WAIT}s — VM will start without mesh connectivity"
# Exit 0: don't block cloud-init. VM will retry via wg-mesh-watchdog.timer.
exit 0