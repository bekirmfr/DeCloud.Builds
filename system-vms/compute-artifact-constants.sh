#!/usr/bin/env bash
# ============================================================
# system-vms/compute-artifact-constants.sh
#
# Discovers every file under system-vms/{role}/assets/ and
# system-vms/shared/assets/, then outputs a C# file containing
# the SHA256 and data: URI constants needed by SystemVmTemplateSeeder.cs.
#
# Usage (run from system-vms/ directory):
#   bash compute-artifact-constants.sh
#
# Output:
#   artifact-constants.cs   ← paste relevant sections into
#                              SystemVmTemplateSeeder.cs, then bump
#                              the affected TemplateRevision constant.
#
# Expected directory layout:
#   system-vms/
#     compute-artifact-constants.sh   ← this file
#     shared/
#       assets/
#         wg-mesh-enroll.sh
#         wg-config-fetch.sh
#     dht/
#       src/            ← Go source (not processed here)
#       assets/
#         dht-dashboard.py
#         dashboard.html
#         ...
#       cloud-init.yaml
#     blockstore/
#       src/
#       assets/
#         ...
#       cloud-init.yaml
#     relay/
#       assets/
#         relay-api.py
#         ...
#       cloud-init.yaml
#
# Constant naming convention:
#   {Role}{FileStem}{Ext?}
#   Role is title-cased: Shared, Dht, Blockstore, Relay
#   FileStem is camel-cased from the filename (dashes/dots → camelCase)
#   Ext is omitted for .sh; included for ambiguous types (.py → Py, .html → Html)
#   Examples:
#     shared/assets/wg-mesh-enroll.sh   → WgMeshEnroll{Sha256,DataUri}
#     dht/assets/dht-dashboard.py       → DhtDashboardPy{Sha256,DataUri}
#     dht/assets/dashboard.html         → DhtDashboardHtml{Sha256,DataUri}
#     blockstore/assets/dashboard.css   → BlockstoreDashboardCss{Sha256,DataUri}
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${SCRIPT_DIR}/artifact-constants.cs"

# ── Helpers ──────────────────────────────────────────────────────────────────

# Detect media type from file extension
media_type() {
    local file="$1"
    case "${file##*.}" in
        sh)         echo "text/x-sh" ;;
        py)         echo "text/x-python" ;;
        html)       echo "text/html" ;;
        css)        echo "text/css" ;;
        js)         echo "text/javascript" ;;
        json)       echo "application/json" ;;
        yaml|yml)   echo "text/yaml" ;;
        txt)        echo "text/plain" ;;
        png)        echo "image/png" ;;
        jpg|jpeg)   echo "image/jpeg" ;;
        svg)        echo "image/svg+xml" ;;
        ico)        echo "image/x-icon" ;;
        gif)        echo "image/gif" ;;
        *)          echo "application/octet-stream" ;;
    esac
}

# Convert a filename to a C# PascalCase identifier segment.
# Rules:
#   - Split on dashes, dots, and underscores
#   - Title-case each segment
#   - Omit .sh extension (implied by Script type)
#   - Keep other extensions as a suffix (Py, Html, Css, Js, Json, etc.)
# Examples:
#   wg-mesh-enroll.sh   → WgMeshEnroll   (no Sh suffix)
#   dht-dashboard.py    → DhtDashboardPy
#   dashboard.html      → DashboardHtml
#   relay-api.py        → RelayApiPy
filename_to_pascal() {
    local filename="$1"
    local ext="${filename##*.}"
    local stem="${filename%.*}"

    # Split on dash, dot, underscore → title-case each segment
    local result=""
    IFS='-._' read -ra parts <<< "$stem"
    for part in "${parts[@]}"; do
        if [ -n "$part" ]; then
            result+="$(echo "${part:0:1}" | tr '[:lower:]' '[:upper:]')${part:1}"
        fi
    done

    # Append extension segment (except .sh — it's implied)
    if [ "$ext" != "sh" ] && [ -n "$ext" ]; then
        result+="$(echo "${ext:0:1}" | tr '[:lower:]' '[:upper:]')${ext:1}"
    fi

    echo "$result"
}

# Title-case a role directory name for use as C# prefix.
# dht → Dht, blockstore → Blockstore, shared → Shared, relay → Relay
role_prefix() {
    local role="$1"
    echo "$(echo "${role:0:1}" | tr '[:lower:]' '[:upper:]')${role:1}"
}

# Emit one constant pair (Sha256 + DataUri) for a single file.
emit_constant() {
    local const_name="$1"   # e.g. DhtDashboardPy
    local file="$2"         # absolute path
    local rel_path="$3"     # relative path for comment

    local sha256 b64 size mime
    sha256=$(sha256sum "$file" | awk '{print $1}')
    b64=$(base64 -w0 "$file")
    size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)
    mime=$(media_type "$file")

    cat >> "$OUTPUT" << EOF
    // ${rel_path}  (${size} bytes, ${mime})
    private const string ${const_name}Sha256  = "${sha256}";
    private const string ${const_name}DataUri = "data:${mime};base64,${b64}";

EOF
}

# ── Main ─────────────────────────────────────────────────────────────────────

{
cat << 'HEADER'
// ============================================================
// artifact-constants.cs  —  AUTO-GENERATED
// Run: bash system-vms/compute-artifact-constants.sh
// DO NOT EDIT MANUALLY — regenerate from source files.
//
// Usage:
//   1. Copy the constants for changed artifacts into SystemVmTemplateSeeder.cs
//   2. Bump the affected TemplateRevision constant (DhtTemplateRevision, etc.)
//   3. Commit to the Orchestrator repo
// ============================================================

// Paste this block inside the SystemVmTemplateSeeder class body.
// Replace the COMPUTE_FROM_FILE placeholders with the values below.

HEADER
} > "$OUTPUT"

TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
echo "// Generated: ${TIMESTAMP}" >> "$OUTPUT"
echo "" >> "$OUTPUT"

# Track which roles had assets (for summary)
declare -A role_counts
total_files=0
errors=0

# Process roles in a defined order: shared first (used by multiple roles),
# then per-role in alphabetical order.
ROLES=("shared" "dht" "blockstore" "relay")

for role in "${ROLES[@]}"; do
    assets_dir="${SCRIPT_DIR}/${role}/assets"

    if [ ! -d "$assets_dir" ]; then
        echo "// [skipped ${role}/assets — directory not found]" >> "$OUTPUT"
        echo "" >> "$OUTPUT"
        continue
    fi

    prefix=$(role_prefix "$role")
    role_file_count=0

    echo "// ── ${prefix} ─────────────────────────────────────────────────────────" >> "$OUTPUT"

    # Find all files, sorted for deterministic output.
    # Exclude hidden files, backup files, and the compute script itself.
    while IFS= read -r -d '' file; do
        filename=$(basename "$file")

        # Skip hidden files, backup files, compiled outputs
        case "$filename" in
            .* | *~ | *.pyc | *.pyo | __pycache__) continue ;;
        esac

        rel_path="${role}/assets/${filename}"

        # Derive constant name: {RolePrefix}{FileStem}
        file_pascal=$(filename_to_pascal "$filename")
        const_name="${prefix}${file_pascal}"

        if emit_constant "$const_name" "$file" "$rel_path"; then
            role_file_count=$((role_file_count + 1))
            total_files=$((total_files + 1))
        else
            echo "  ERROR processing: $rel_path" >&2
            errors=$((errors + 1))
        fi

    done < <(find "$assets_dir" -maxdepth 1 -type f -print0 | sort -z)

    if [ "$role_file_count" -eq 0 ]; then
        echo "// [no files found in ${role}/assets/]" >> "$OUTPUT"
        echo "" >> "$OUTPUT"
    fi

    role_counts["$role"]=$role_file_count
done

# ── Summary comment ──────────────────────────────────────────────────────────

{
echo ""
echo "// ── Summary ─────────────────────────────────────────────────────────────"
echo "// Generated: ${TIMESTAMP}"
echo "// Files processed:"
for role in "${ROLES[@]}"; do
    count="${role_counts[$role]:-0}"
    echo "//   ${role}/assets/: ${count} files"
done
echo "// Total: ${total_files} artifact constants"
if [ "$errors" -gt 0 ]; then
    echo "// ERRORS: ${errors} files failed to process — check output above"
fi
} >> "$OUTPUT"

# ── Console output ───────────────────────────────────────────────────────────

echo ""
echo "✓ artifact-constants.cs written (${total_files} artifacts)"
echo ""
echo "Roles processed:"
for role in "${ROLES[@]}"; do
    count="${role_counts[$role]:-0}"
    if [ "$count" -gt 0 ]; then
        echo "  ${role}/assets/: ${count} files"
    else
        echo "  ${role}/assets/: (none found)"
    fi
done

if [ "$errors" -gt 0 ]; then
    echo ""
    echo "WARNING: ${errors} files failed to process." >&2
    exit 1
fi

echo ""
echo "Next steps:"
echo "  1. Open artifact-constants.cs"
echo "  2. Copy the updated constant pairs into SystemVmTemplateSeeder.cs"
echo "  3. Bump the affected TemplateRevision constant"
echo "  4. Commit to the Orchestrator repo"