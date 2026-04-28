#!/bin/bash
#
# Build script for DeCloud Block Store Node binary
# Cross-compiles for amd64 and arm64, then gzip+base64 encodes the output.
#
# Self-bootstrapping: downloads Go automatically if not installed.
# Hash-based idempotency: skips build when source is unchanged and
# binaries already exist (mirrors dht-node-src/build.sh exactly).
#
# Usage:
#   ./build.sh              # Build for both architectures
#   ./build.sh amd64        # Build for amd64 only
#   ./build.sh arm64        # Build for arm64 only
#
# Output (placed in parent blockstore-vm/ directory):
#   ../blockstore-node-amd64.gz.b64
#   ../blockstore-node-arm64.gz.b64
#   ../.blockstore-node-source.sha256  (cache invalidation marker)
#

set -euo pipefail

GO_VERSION="1.23.7"
GO_MIN_VERSION="1.23"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$(dirname "$SCRIPT_DIR")"  # blockstore-vm/
ARCHITECTURES="${1:-amd64 arm64}"

cd "$SCRIPT_DIR"

echo "=== DeCloud Block Store Node Build ==="
echo "Source: $SCRIPT_DIR"
echo "Output: $OUTPUT_DIR"
echo ""

# =====================================================
# Ensure Go is available (auto-install if missing)
# =====================================================
ensure_go() {
    if command -v go &>/dev/null; then
        local current_version
        current_version=$(go version | grep -oP 'go\K[0-9]+\.[0-9]+' | head -1)
        local major minor req_minor
        major=$(echo "$current_version" | cut -d. -f1)
        minor=$(echo "$current_version" | cut -d. -f2)
        req_minor=$(echo "$GO_MIN_VERSION" | cut -d. -f2)

        if [ "$major" -ge 1 ] && [ "$minor" -ge "$req_minor" ]; then
            echo "Using system Go $(go version)"
            return 0
        fi
        echo "System Go $current_version is too old (need $GO_MIN_VERSION+)"
    fi

    local GO_DIR="${SCRIPT_DIR}/.go-${GO_VERSION}"
    if [ -x "${GO_DIR}/bin/go" ]; then
        echo "Using cached Go $GO_VERSION"
        export PATH="${GO_DIR}/bin:$PATH"
        return 0
    fi

    echo "Downloading Go $GO_VERSION..."
    local HOST_ARCH
    HOST_ARCH=$(uname -m)
    case "$HOST_ARCH" in
        x86_64|amd64) HOST_ARCH="amd64" ;;
        aarch64|arm64) HOST_ARCH="arm64" ;;
        *) echo "Unsupported host architecture: $HOST_ARCH"; exit 1 ;;
    esac

    local GO_TARBALL="go${GO_VERSION}.linux-${HOST_ARCH}.tar.gz"
    local GO_URL="https://golang.org/dl/${GO_TARBALL}"

    mkdir -p "$GO_DIR"
    if command -v curl &>/dev/null; then
        curl -sSL "$GO_URL" | tar -xz -C "$GO_DIR" --strip-components=1
    elif command -v wget &>/dev/null; then
        wget -qO- "$GO_URL" | tar -xz -C "$GO_DIR" --strip-components=1
    else
        echo "Neither curl nor wget found — cannot download Go"; exit 1
    fi

    export PATH="${GO_DIR}/bin:$PATH"
    echo "Go $GO_VERSION downloaded to $GO_DIR"
}

ensure_go

# =====================================================
# Go environment (isolated from system Go paths)
# =====================================================
export HOME="${HOME:-/tmp}"
export GOPATH="${GOPATH:-${SCRIPT_DIR}/.gopath}"
export GOMODCACHE="${GOMODCACHE:-${GOPATH}/pkg/mod}"
export GOCACHE="${GOCACHE:-${SCRIPT_DIR}/.gocache}"
mkdir -p "$GOPATH" "$GOMODCACHE" "$GOCACHE"

echo "Go env: HOME=$HOME GOPATH=$GOPATH GOMODCACHE=$GOMODCACHE GOCACHE=$GOCACHE"

# =====================================================
# Source hash (detect stale binaries)
# =====================================================
SOURCE_HASH=$(find "$SCRIPT_DIR" \
    -not -path '*/.go-*' \
    -not -path '*/.gopath/*' \
    -not -path '*/.gocache/*' \
    \( -name '*.go' -o -name 'go.mod' -o -name 'go.sum' \) \
    | sort | xargs sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1 || true)
HASH_FILE="${OUTPUT_DIR}/.blockstore-node-source.sha256"

echo "Source hash: ${SOURCE_HASH}"

if [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE" 2>/dev/null)" = "$SOURCE_HASH" ]; then
    ALL_EXIST=true
    for ARCH in $ARCHITECTURES; do
        if [ ! -f "${OUTPUT_DIR}/blockstore-node-${ARCH}.gz.b64" ]; then
            ALL_EXIST=false
            break
        fi
    done

    if [ "$ALL_EXIST" = true ]; then
        echo "Source unchanged and binaries exist — skipping build."
        ls -lh "${OUTPUT_DIR}"/*.gz.b64 2>/dev/null
        exit 0
    fi
fi

# =====================================================
# Resolve dependencies
# =====================================================
echo "Resolving Go dependencies..."
go mod tidy

# =====================================================
# Build
# =====================================================
for ARCH in $ARCHITECTURES; do
    BINARY_NAME="blockstore-node-${ARCH}"
    GZB64_NAME="${BINARY_NAME}.gz.b64"
    BINARY_PATH="${OUTPUT_DIR}/${BINARY_NAME}"
    GZB64_PATH="${OUTPUT_DIR}/${GZB64_NAME}"

    echo "Building for linux/${ARCH}..."

    CGO_ENABLED=0 GOOS=linux GOARCH="${ARCH}" \
        go build -trimpath -ldflags="-s -w" -o "${BINARY_PATH}" .

    BINARY_SIZE=$(stat -c%s "${BINARY_PATH}" 2>/dev/null || stat -f%z "${BINARY_PATH}" 2>/dev/null)
    echo "  Binary: ${BINARY_PATH} ($(( BINARY_SIZE / 1024 / 1024 ))MB)"

    gzip -9 -c "${BINARY_PATH}" | base64 -w0 > "${GZB64_PATH}"
    GZB64_SIZE=$(stat -c%s "${GZB64_PATH}" 2>/dev/null || stat -f%z "${GZB64_PATH}" 2>/dev/null)
    echo "  Gzip+Base64: ${GZB64_PATH} ($(( GZB64_SIZE / 1024 / 1024 ))MB, $(( GZB64_SIZE * 100 / BINARY_SIZE ))% of original)"

    rm -f "${BINARY_PATH}"
    echo "  Done."
    echo ""
done

echo "$SOURCE_HASH" > "$HASH_FILE"
echo "Saved source hash to ${HASH_FILE}"

echo "=== Build complete ==="
ls -lh "${OUTPUT_DIR}"/*.gz.b64 2>/dev/null || echo "(no .gz.b64 files found — build may have failed)"
