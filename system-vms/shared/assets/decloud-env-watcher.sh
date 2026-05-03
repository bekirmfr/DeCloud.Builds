#!/bin/bash
# ============================================================
# decloud-env-watcher.sh
#
# In-VM watcher for runtime-mutable environmental values.
#
# Polls the node-local environment endpoint every minute (driven by
# decloud-env-watcher.timer). Compares values against the locally-cached
# environment file. On change: rewrites the local file and applies the
# max-scope reaction across changed variables.
#
# Generic across all mesh-participant roles. Same script, role-parameterized
# via systemd unit's ExecStart argument:
#
#   ExecStart=/usr/local/bin/decloud-env-watcher.sh dht
#   ExecStart=/usr/local/bin/decloud-env-watcher.sh blockstore
#
# The role's scope policy is read from /etc/decloud-{role}/variable-scopes.conf
# at every tick. The scope file is generated at cloud-init render time from
# the template's Variables declaration (see VariableScopesBlockResolver).
#
# Wire shape of the env endpoint response (JSON):
#   { "values":     { "VARNAME": "value", ... },
#     "scopes":     { "VARNAME": "noop|reload|restart", ... },
#     "generation": "sha256-truncated" }
#
# Wire shape of /etc/decloud-{role}/environment (key=value, sourceable):
#   ENV_GENERATION=<gen>
#   VARNAME=value
#   ...
#
# Wire shape of /etc/decloud-{role}/variable-scopes.conf:
#   VARNAME=scope    # e.g. ADVERTISE_IP=restart
#   ...              # one per declared dynamic
#
# Exit codes:
#   0  — normal (no change, or change applied successfully)
#   1  — endpoint unreachable / malformed response (transient; timer retries next tick)
#   2  — usage error (missing role argument)
# ============================================================

set -uo pipefail

# ── Arguments ─────────────────────────────────────────────────────────────────
ROLE="${1:-}"
if [ -z "$ROLE" ]; then
    echo "[env-watcher] usage: $0 <role>" >&2
    exit 2
fi

# ── Paths ─────────────────────────────────────────────────────────────────────
ENV_DIR="/etc/decloud-${ROLE}"
ENV_FILE="${ENV_DIR}/environment"
SCOPE_FILE="${ENV_DIR}/variable-scopes.conf"
ENDPOINT="http://192.168.122.1:5100/api/obligations/${ROLE}/environment"
SERVICE="decloud-${ROLE}"
WG_QUICK_UNIT="wg-quick@wg-mesh"

LOG_TAG="env-watcher[${ROLE}]"
log() {
    # journald via systemd-cat — survives across script invocations
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" \
        | systemd-cat -t "decloud-env-watcher" -p info 2>/dev/null \
        || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${LOG_TAG}: $*" >&2
}

# ── Sanity checks ─────────────────────────────────────────────────────────────
if [ ! -f "$SCOPE_FILE" ]; then
    log "scope file missing: $SCOPE_FILE — nothing to watch (exiting)"
    exit 0
fi

# ── Fetch fresh environment from node-local endpoint ──────────────────────────
NEW=$(curl -sf --max-time 5 "$ENDPOINT" 2>/dev/null)
if [ -z "$NEW" ]; then
    # Endpoint unreachable. Could be node agent restart, network blip, or genuine
    # outage. Don't escalate — timer will retry next tick.
    log "endpoint unreachable, will retry next tick"
    exit 1
fi

# ── Compare generations ───────────────────────────────────────────────────────
NEW_GEN=$(echo "$NEW" | jq -r '.generation // empty')
if [ -z "$NEW_GEN" ]; then
    log "malformed response (no generation field)"
    exit 1
fi

OLD_GEN=""
if [ -f "$ENV_FILE" ]; then
    OLD_GEN=$(grep -oP '^ENV_GENERATION=\K.*' "$ENV_FILE" 2>/dev/null || echo "")
fi

if [ "$NEW_GEN" = "$OLD_GEN" ]; then
    # Nothing changed. Most common path. Silent.
    exit 0
fi

# ── Diff each declared dynamic, accumulate worst scope ────────────────────────
# Read scope policy. Format: VARNAME=scope, with optional inline comments.
declare -A SCOPES
while IFS='=' read -r key val; do
    [ -z "$key" ] && continue
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    # Strip inline comment + surrounding whitespace from value
    val="${val%%#*}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    SCOPES["$key"]="$val"
done < "$SCOPE_FILE"

# Helper: rank ordering for max-scope folding.
scope_rank() {
    case "$1" in
        noop)    echo 0 ;;
        reload)  echo 1 ;;
        restart) echo 2 ;;
        *)       echo 0 ;;  # unknown → noop (conservative)
    esac
}

WORST="noop"
WORST_RANK=0
CHANGED_VARS=()
WG_VAR_CHANGED=0

for VAR in "${!SCOPES[@]}"; do
    OLD_VAL=""
    if [ -f "$ENV_FILE" ]; then
        OLD_VAL=$(grep -oP "^${VAR}=\K.*" "$ENV_FILE" 2>/dev/null || echo "")
    fi
    NEW_VAL=$(echo "$NEW" | jq -r ".values.\"${VAR}\" // empty")

    if [ "$OLD_VAL" != "$NEW_VAL" ]; then
        CHANGED_VARS+=("$VAR")
        SCOPE="${SCOPES[$VAR]}"
        RANK=$(scope_rank "$SCOPE")
        if [ "$RANK" -gt "$WORST_RANK" ]; then
            WORST="$SCOPE"
            WORST_RANK="$RANK"
        fi
        # Track WG-prefixed variable changes — they require additional wg-quick bounce
        # (see commentary at top of file). The role service alone restarting is not
        # enough to push a new endpoint into the kernel's WireGuard config.
        if [[ "$VAR" =~ ^WG_ ]]; then
            WG_VAR_CHANGED=1
        fi
    fi
done

if [ "${#CHANGED_VARS[@]}" -eq 0 ]; then
    # Generation changed but no declared variable's value moved. Could happen if
    # the orchestrator added a new dynamic the local scope file doesn't track yet
    # (template revision skew). Rewrite env file (so the new variable lands) but
    # take no service action.
    log "generation bumped but no tracked variable changed (possible template revision skew)"
fi

# ── Always rewrite env file ───────────────────────────────────────────────────
# Even on noop scope, the file gets refreshed so a future cold restart sees
# current values.
mkdir -p "$ENV_DIR"
TMP_ENV=$(mktemp)
{
    echo "ENV_GENERATION=$NEW_GEN"
    echo "$NEW" | jq -r '.values | to_entries[] | "\(.key)=\(.value)"'
} > "$TMP_ENV"
mv -f "$TMP_ENV" "$ENV_FILE"
chmod 0640 "$ENV_FILE" 2>/dev/null || true

# ── Apply max-scope reaction ──────────────────────────────────────────────────
case "$WORST" in
    noop)
        log "changed: ${CHANGED_VARS[*]} (noop) — env file refreshed, no service action"
        ;;
    reload)
        log "changed: ${CHANGED_VARS[*]} (reload) — sending SIGHUP to ${SERVICE}"
        systemctl reload "$SERVICE" 2>&1 | systemd-cat -t "decloud-env-watcher" -p info || true
        ;;
    restart)
        log "changed: ${CHANGED_VARS[*]} (restart) — restarting ${SERVICE}"
        systemctl restart "$SERVICE" 2>&1 | systemd-cat -t "decloud-env-watcher" -p info || true
        # Special case: any WG_* variable change requires bouncing wg-quick alongside
        # the role service. The role service alone won't push new endpoint config
        # into the kernel's WireGuard interface — wg-quick has to re-read the env.
        # Only mesh-participant roles have a wg-quick@wg-mesh unit; for non-mesh
        # roles (relay) this is a no-op since the unit doesn't exist.
        if [ "$WG_VAR_CHANGED" -eq 1 ] && systemctl list-unit-files | grep -q "^${WG_QUICK_UNIT}.service"; then
            log "WG_* variable changed — also restarting ${WG_QUICK_UNIT}"
            systemctl restart "$WG_QUICK_UNIT" 2>&1 | systemd-cat -t "decloud-env-watcher" -p info || true
        fi
        ;;
    *)
        log "unexpected scope ${WORST}, treating as noop"
        ;;
esac

exit 0
