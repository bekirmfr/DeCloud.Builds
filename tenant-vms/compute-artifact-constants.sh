#!/usr/bin/env bash
# ============================================================
# tenant-vms/compute-artifact-constants.sh
#
# Auto-discovers every {role}/assets/ directory under tenant-vms/,
# computes SHA256 + data: URI for each file, and outputs a C# file
# containing the constants needed by GeneralVmTemplateSeeder.cs (and
# any future per-marketplace-template seeders that include inline assets).
#
# No hardcoded role lists or prefix maps. Adding a new role to
# tenant-vms/ automatically includes it — no script changes needed.
#
# Sibling to system-vms/compute-artifact-constants.sh: same algorithm,
# same output format, separate output files. Each tree owns its own
# constants so a script change in one tree doesn't force regeneration
# of the other.
#
# Usage (run from tenant-vms/ directory):
#   bash compute-artifact-constants.sh
#
# Output:
#   artifact-constants.cs
#
# Naming convention  —  {RolePrefix}{FileStem}{ExtSuffix}
#
#   RolePrefix:
#     Directory named "shared" → empty prefix (scripts identified by name alone)
#     Any other directory      → PascalCase(directory name)
#                                e.g. "general" → "General"
#
#   FileStem:
#     PascalCase of the filename stem, with the role name stripped if the
#     filename already begins with it — avoids doubled prefixes.
#     e.g. "general-api.py" in general/ → stem "api" → "GeneralApiPy"
#          "index.html"     in general/ → stem "index" → "GeneralIndexHtml"
#
#   ExtSuffix:
#     .sh  → omitted (implied by Script ArtifactType)
#     .py  → Py
#     .html → Html
#     .css  → Css
#     .js   → Js
#     other → title-cased extension
#
#   Full examples:
#     general/assets/general-api.py    → GeneralApiPy
#     general/assets/index.html        → GeneralIndexHtml
#
# Note: decloud-agent binaries are NOT in assets/ — they ship via
# HTTPS-from-GitHub-Releases as binary artifacts (see implementation
# plan §1 for canonical URLs and SHA256s). This script only handles
# inline data: URI artifacts.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${SCRIPT_DIR}/artifact-constants.cs"

# ── Pure functions ────────────────────────────────────────────────────────────

# Infer MIME type from file extension.
media_type() {
    case "${1##*.}" in
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
        webp)       echo "image/webp" ;;
        *)          echo "application/octet-stream" ;;
    esac
}

# Convert a dash/dot/underscore-separated string to PascalCase.
# e.g. "general-api" → "GeneralApi"
to_pascal() {
    local input="$1"
    local result=""
    IFS='-._' read -ra parts <<< "$input"
    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        result+="$(echo "${part:0:1}" | tr '[:lower:]' '[:upper:]')${part:1}"
    done
    echo "$result"
}

# Derive the C# role prefix from a directory name.
# "shared" is the only special case — it produces an empty prefix
# because shared scripts are identified by their own name alone.
# All other directories produce PascalCase(name).
role_to_prefix() {
    local role="$1"
    if [ "$role" = "shared" ]; then
        echo ""
    else
        to_pascal "$role"
    fi
}

# Derive the C# constant name for one file in one role.
#
# Algorithm:
#   1. Split filename into stem and extension.
#   2. Strip the role name from the beginning of the stem if present,
#      followed by a separator (dash/underscore). This removes the
#      redundant role prefix that many filenames carry.
#      e.g. role=general, stem=general-api → api
#           role=general, stem=index       → index  (no match, unchanged)
#   3. Convert the remaining stem to PascalCase.
#   4. Append the extension suffix (omit for .sh).
#   5. Prepend the role prefix.
file_const_name() {
    local filename="$1"
    local role="$2"
    local prefix="$3"

    local ext="${filename##*.}"
    local stem="${filename%.*}"
    local stem_lower ext_lower role_lower
    stem_lower=$(echo "$stem"  | tr '[:upper:]' '[:lower:]')
    role_lower=$(echo "$role"  | tr '[:upper:]' '[:lower:]')

    # Strip role prefix from stem when filename starts with "{role}-" or "{role}_"
    if [[ "$stem_lower" == "${role_lower}-"* || "$stem_lower" == "${role_lower}_"* ]]; then
        stem="${stem:$(( ${#role} + 1 ))}"
    fi

    local stem_pascal
    stem_pascal=$(to_pascal "$stem")

    # Extension suffix — omit for shell scripts
    local ext_suffix=""
    if [ "$ext" != "sh" ] && [ -n "$ext" ]; then
        ext_suffix="$(echo "${ext:0:1}" | tr '[:lower:]' '[:upper:]')${ext:1}"
    fi

    echo "${prefix}${stem_pascal}${ext_suffix}"
}

# Write one constant pair (Sha256 + DataUri) for a file.
emit_constant() {
    local const_name="$1"
    local file="$2"
    local rel_path="$3"

    local sha256 b64 size mime
    sha256=$(sha256sum "$file" | awk '{print $1}')
    b64=$(base64 -w0 "$file")
    size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)
    mime=$(media_type "$file")

    cat >> "$OUTPUT" << EOF
    // ${rel_path}  (${size} bytes)
    private const string ${const_name}Sha256  = "${sha256}";
    private const string ${const_name}DataUri = "data:${mime};base64,${b64}";

EOF
}

# ── Discover roles ────────────────────────────────────────────────────────────
# Find every directory directly under tenant-vms/ that contains an assets/ subdirectory.
# Sort so "shared" comes first (its scripts are referenced by all other roles),
# then remaining roles in alphabetical order.

discover_roles() {
    local roles=()
    local others=()

    while IFS= read -r -d '' assets_dir; do
        local role
        role=$(basename "$(dirname "$assets_dir")")
        if [ "$role" = "shared" ]; then
            roles=("shared" "${roles[@]+"${roles[@]}"}")
        else
            others+=("$role")
        fi
    done < <(find "$SCRIPT_DIR" -mindepth 2 -maxdepth 2 -name "assets" -type d -print0 | sort -z)

    # Sort the non-shared roles alphabetically for deterministic output
    IFS=$'\n' sorted_others=($(printf '%s\n' "${others[@]+"${others[@]}"}" | sort))
    unset IFS

    echo "${roles[@]+"${roles[@]}"} ${sorted_others[@]+"${sorted_others[@]}"}"
}

# ── Main ─────────────────────────────────────────────────────────────────────

# File header
cat > "$OUTPUT" << 'HEADER'
// ============================================================
// artifact-constants.cs  —  AUTO-GENERATED
// Run: bash tenant-vms/compute-artifact-constants.sh
// DO NOT EDIT MANUALLY — regenerate from source files.
//
// Usage:
//   1. Copy the constants for changed artifacts into GeneralVmTemplateSeeder.cs
//      (or other tenant-side seeders).
//   2. Bump the affected TemplateRevision constant (GeneralTemplateRevision, etc.)
//   3. Commit to the Orchestrator repo
// ============================================================

// Paste this block inside the appropriate tenant template seeder class body.
// Replace the COMPUTE_FROM_FILE placeholders with the generated values.

HEADER

echo "// Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$OUTPUT"
echo "" >> "$OUTPUT"

total_files=0
errors=0
declare -A role_counts
declare -A role_prefixes
declare -A role_names_generated  # for the summary

# Read discovered roles into array
read -ra ROLES <<< "$(discover_roles)"

for role in "${ROLES[@]}"; do
    assets_dir="${SCRIPT_DIR}/${role}/assets"
    [ -d "$assets_dir" ] || continue

    prefix=$(role_to_prefix "$role")
    role_file_count=0
    role_names=()

    # Section header
    section="${prefix:-Shared}"
    echo "// ── ${section} ────────────────────────────────────────────────────────────────" >> "$OUTPUT"

    while IFS= read -r -d '' file; do
        filename=$(basename "$file")

        # Skip hidden files, Python bytecode, backup files
        case "$filename" in
            .* | *~ | *.pyc | *.pyo) continue ;;
        esac

        rel_path="${role}/assets/${filename}"
        const_name=$(file_const_name "$filename" "$role" "$prefix")

        if emit_constant "$const_name" "$file" "$rel_path" 2>/dev/null; then
            role_file_count=$((role_file_count + 1))
            total_files=$((total_files + 1))
            role_names+=("${const_name}")
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
    role_prefixes["$role"]="${prefix:-<none>}"
    role_names_generated["$role"]="${role_names[*]+"${role_names[*]}"}"
done

# Summary comment in output file
{
echo ""
echo "// ── Summary ─────────────────────────────────────────────────────────────────"
echo "// Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "// Roles discovered:"
for role in "${ROLES[@]}"; do
    echo "//   ${role}/assets/ [prefix='${role_prefixes[$role]:-}']: ${role_counts[$role]:-0} files"
done
echo "// Total: ${total_files} artifact constants"
[ "$errors" -gt 0 ] && echo "// ERRORS: ${errors} files failed to process"
} >> "$OUTPUT"

# ── Console output ────────────────────────────────────────────────────────────

echo ""
echo "✓ artifact-constants.cs written (${total_files} artifacts)"
echo ""

for role in "${ROLES[@]}"; do
    count="${role_counts[$role]:-0}"
    prefix="${role_prefixes[$role]:-<none>}"
    names="${role_names_generated[$role]:-}"
    echo "  ${role}/assets/ [prefix='${prefix}']: ${count} files"
    if [ -n "$names" ]; then
        # Print names wrapped at ~80 chars
        echo "    $(echo "$names" | tr ' ' '\n' | awk '{printf "%s, ", $0} NR%4==0{print ""}' | sed 's/, $//')"
        echo ""
    fi
done

if [ "$errors" -gt 0 ]; then
    echo ""
    echo "WARNING: ${errors} files failed to process." >&2
    exit 1
fi

echo "Next steps:"
echo "  1. Open artifact-constants.cs"
echo "  2. Copy updated constant pairs into GeneralVmTemplateSeeder.cs"
echo "  3. Bump the affected TemplateRevision constant"
echo "  4. Commit to the Orchestrator repo"