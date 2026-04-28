#!/bin/bash
#
# DeCloud Relay VM NAT Configuration Callback
# Version: 3.0 - Sequential orchestrator notification
#
# This script runs on relay VM boot to notify the node agent
# of the VM's IP address so NAT rules can be configured.
#
# Flow:
# 1. Configure NAT rules on host (node agent)
# 2. On success → notify orchestrator that relay is fully ready
# 3. Orchestrator sets IsActive=true only when relay can serve traffic
#
# Key improvements:
# - Sequential callbacks (NAT first, then orchestrator)
# - Idempotent (checks marker file)
# - Validates node agent is ready before calling
# - Better error handling and logging
#

set -e

LOG_FILE="/var/log/decloud-nat-callback.log"
MARKER_FILE="/var/lib/decloud/nat-callback-complete"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# =====================================================
# Check if callback already completed successfully
# =====================================================
if [ -f "$MARKER_FILE" ]; then
    log "NAT callback already completed successfully - skipping"
    log "Marker file exists: $MARKER_FILE"
    log "Delete this file to force re-run: rm $MARKER_FILE"
    exit 0
fi

log "Starting NAT callback process..."

# =====================================================
# Detect gateway IP (node agent host)
# =====================================================
log "Detecting gateway IP..."

# Method 1: Default route (most reliable)
GATEWAY_IP=$(ip route | grep default | awk '{print $3}' | head -1)

# Method 2: Parse route to external IP
if [ -z "$GATEWAY_IP" ]; then
    GATEWAY_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'via \K[\d.]+' | head -1)
fi

# Method 3: Fallback to common libvirt gateway
if [ -z "$GATEWAY_IP" ]; then
    log "⚠ Using fallback gateway 192.168.122.1"
    GATEWAY_IP="192.168.122.1"
fi

NODE_AGENT_URL="http://${GATEWAY_IP}:5100"
VM_ID="__VM_ID__"

log "Detected gateway IP: $GATEWAY_IP"
log "Node agent URL: $NODE_AGENT_URL"

# =====================================================
# Wait for network and obtain IP address
# =====================================================
log "Waiting for IP address..."
for i in {1..30}; do
    # Dynamically detect the default route interface (works for eth0, enp1s0, ens3, etc.)
    DEFAULT_INTERFACE=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' | head -1)
    
    if [ -n "$DEFAULT_INTERFACE" ]; then
        VM_IP=$(ip -4 addr show "$DEFAULT_INTERFACE" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
        
        if [ -n "$VM_IP" ]; then
            log "✓ Obtained IP address: $VM_IP on interface $DEFAULT_INTERFACE"
            break
        fi
    fi
    sleep 2
done

if [ -z "$VM_IP" ]; then
    log "❌ Failed to obtain IP address after 60 seconds"
    exit 1
fi

# =====================================================
# Verify node agent is reachable
# =====================================================
log "Checking if node agent is reachable..."
for i in {1..12}; do
    if curl -s -m 2 "$NODE_AGENT_URL/health" >/dev/null 2>&1; then
        log "✓ Node agent is reachable"
        break
    fi
    
    if [ $i -eq 12 ]; then
        log "❌ Node agent not reachable after 60 seconds"
        log "   This may be expected if node agent is still starting"
        log "   Systemd will retry this service automatically"
        exit 1
    fi
    
    log "   Waiting for node agent... (attempt $i/12)"
    sleep 5
done

# =====================================================
# Compute authentication token
# =====================================================
MACHINE_ID="__HOST_MACHINE_ID__"
log "Using machine ID for authentication: ${MACHINE_ID:0:8}..."

MESSAGE="${VM_ID}:${VM_IP}"
TOKEN=$(echo -n "$MESSAGE" | openssl dgst -sha256 -hmac "$MACHINE_ID" -binary | base64)

# =====================================================
# STEP 1: Call NAT callback endpoint on node agent
# =====================================================
log "Notifying node agent at $NODE_AGENT_URL/api/relay/nat-ready..."

RESPONSE=$(curl -X POST "$NODE_AGENT_URL/api/relay/nat-ready" \
    -H "Content-Type: application/json" \
    -H "X-Relay-Token: $TOKEN" \
    -d "{
        \"vmId\": \"$VM_ID\",
        \"vmIp\": \"$VM_IP\"
    }" \
    --max-time 10 \
    --retry 2 \
    --retry-delay 5 \
    -w "\nHTTP_CODE:%{http_code}" \
    -s \
    2>&1)

HTTP_CODE=$(echo "$RESPONSE" | grep -oP 'HTTP_CODE:\K\d+')

# =====================================================
# Handle NAT callback response
# =====================================================
if [ "$HTTP_CODE" = "200" ]; then
    log "✓ Successfully notified node agent - NAT rule configured!"
    
    # Check if idempotent (already configured)
    if echo "$RESPONSE" | grep -q "alreadyConfigured.*true"; then
        log "✓ NAT rules were already configured (idempotent response)"
    else
        log "✓ NAT rules newly configured"
    fi
    
    log "✓ CGNAT nodes can now connect to this relay"
    
    # Create marker file to prevent re-runs
    mkdir -p "$(dirname "$MARKER_FILE")"
    echo "NAT callback completed successfully at $(date)" > "$MARKER_FILE"
    log "✓ Created completion marker: $MARKER_FILE"
    
    # Log to system journal
    logger -t decloud-relay "NAT rule configured successfully for $VM_IP via gateway $GATEWAY_IP"
    
    # =====================================================
    # STEP 2: Now notify orchestrator that relay is fully ready
    # =====================================================
    log ""
    log "=========================================="
    log "NAT configuration complete - notifying orchestrator..."
    log "=========================================="
    
    # Start the persistent registration polling service.
    # decloud-relay-register.service retries until the orchestrator responds with 200
    # and exits cleanly on success — resilient to orchestrator downtime during install.
    log "Starting orchestrator registration service (decloud-relay-register)..."
    if systemctl start decloud-relay-register 2>/dev/null; then
        log "✓ Registration service started — will poll until orchestrator confirms"
        logger -t decloud-relay "Relay registration service started"
    else
        log "⚠ Could not start registration service — will be started by systemd at boot"
        logger -t decloud-relay "Registration service start deferred"
    fi
    
    exit 0
    
elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    log "❌ Authentication failed (HTTP $HTTP_CODE)"
    log "   Token may be invalid - this is a configuration error"
    log "   Response: $RESPONSE"
    logger -t decloud-relay "NAT callback authentication failed - check configuration"
    exit 1
else
    log "⚠ Failed to notify node agent (HTTP ${HTTP_CODE:-timeout})"
    log "   Response: $RESPONSE"
    log "   Systemd will retry automatically"
    logger -t decloud-relay "NAT callback failed - will retry"
    exit 1
fi