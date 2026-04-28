#!/bin/bash
#
# DeCloud DHT Node Health Check
# Queries the local HTTP API exposed by the DHT binary
# Called by the node agent to monitor DHT VM health.
#
# Exit 0 = healthy, Exit 1 = unhealthy
# Outputs JSON to stdout for the node agent to parse.
#

API_PORT="__DHT_API_PORT__"
API_URL="http://127.0.0.1:${API_PORT}/health"

RESPONSE=$(curl -s --max-time 5 "$API_URL" 2>/dev/null)
CURL_EXIT=$?

if [ $CURL_EXIT -ne 0 ]; then
    echo '{"status":"unreachable","error":"curl failed"}'
    exit 1
fi

echo "$RESPONSE"

# Check status field
STATUS=$(echo "$RESPONSE" | jq -r '.status' 2>/dev/null)

if [ "$STATUS" = "active" ]; then
    exit 0
else
    exit 1
fi
