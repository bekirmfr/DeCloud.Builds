#!/bin/bash
#
# Build script for DeCloud DHT Node binary
# Cross-compiles for amd64 and arm64, then base64-encodes the output.
#
# Self-bootstrapping: downloads Go automatically if not installed.
#
# Usage:
#   ./build.sh              # Build for both architectures
#   ./build.sh amd64        # Build for amd64 only
#   ./build.sh arm64        # Build for arm64 only
#
# Output:
#   ../dht-node-amd64.b64   (placed next to templates, not in source dir)
#   ../dht-node-arm64.b64
#

set -euo pipefail

GO_VERSION="1.23.7"
GO_MIN_VERSION="1.23"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$(dirname "$SCRIPT_DIR")"
ARCHITECTURES="${1:-amd64 arm64}"

cd "$SCRIPT_DIR"

echo "=== DeCloud DHT Node Build ==="
echo "Source: $SCRIPT_DIR"
echo "Output: $OUTPUT_DIR"
echo ""

# =====================================================
# Ensure Go is available (auto-install if missing)
# =====================================================
ensure_go() {
    # Check if go is already in PATH and meets minimum version
    if command -v go &>/dev/null; then
        local current_version
        current_version=$(go version | grep -oP 'go\K[0-9]+\.[0-9]+' | head -1)
        if [ -n "$current_version" ]; then
            local major minor
            major=$(echo "$current_version" | cut -d. -f1)
            minor=$(echo "$current_version" | cut -d. -f2)
            local req_major req_minor
            req_major=$(echo "$GO_MIN_VERSION" | cut -d. -f1)
            req_minor=$(echo "$GO_MIN_VERSION" | cut -d. -f2)

            if [ "$major" -gt "$req_major" ] || { [ "$major" -eq "$req_major" ] && [ "$minor" -ge "$req_minor" ]; }; then
                echo "Using system Go: $(go version)"
                return 0
            fi
            echo "System Go $current_version is too old (need $GO_MIN_VERSION+)"
        fi
    fi

    # Check for previously downloaded Go in persistent cache
    # Using /opt instead of inside the repo dir (which gets wiped on reinstall)
    local local_go="/opt/decloud-go/go-${GO_VERSION}/go/bin/go"
    if [ -x "$local_go" ]; then
        echo "Using cached Go at $local_go"
        export GOROOT="/opt/decloud-go/go-${GO_VERSION}/go"
        export PATH="${GOROOT}/bin:${PATH}"
        return 0
    fi

    # Download Go
    echo "Go not found — downloading Go ${GO_VERSION}..."

    # Detect host architecture for the Go toolchain itself
    local host_arch
    host_arch=$(uname -m)
    case "$host_arch" in
        x86_64|amd64)  host_arch="amd64" ;;
        aarch64|arm64) host_arch="arm64" ;;
        *)
            echo "ERROR: Unsupported host architecture: $host_arch"
            exit 1
            ;;
    esac

    local go_tarball="go${GO_VERSION}.linux-${host_arch}.tar.gz"
    local go_url="https://go.dev/dl/${go_tarball}"
    local download_dir="/opt/decloud-go/go-${GO_VERSION}"

    mkdir -p "$download_dir"

    echo "  Downloading: $go_url"
    if command -v curl &>/dev/null; then
        curl -fsSL "$go_url" -o "${download_dir}/${go_tarball}"
    elif command -v wget &>/dev/null; then
        wget -q "$go_url" -O "${download_dir}/${go_tarball}"
    else
        echo "ERROR: Neither curl nor wget found — cannot download Go"
        exit 1
    fi

    echo "  Extracting..."
    tar -xzf "${download_dir}/${go_tarball}" -C "$download_dir"
    rm -f "${download_dir}/${go_tarball}"

    export GOROOT="${download_dir}/go"
    export PATH="${GOROOT}/bin:${PATH}"

    echo "  Installed Go $(go version) at ${GOROOT}"
}

ensure_go

# =====================================================
# Ensure Go environment is usable (critical for service execution)
# When run by CommandExecutor/systemd, HOME/GOPATH/GOMODCACHE may not be set.
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
HASH_FILE="${OUTPUT_DIR}/.dht-node-source.sha256"

echo "Source hash: ${SOURCE_HASH}"

if [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE" 2>/dev/null)" = "$SOURCE_HASH" ]; then
    # Check if all requested binaries exist
    ALL_EXIST=true
    for ARCH in $ARCHITECTURES; do
        if [ ! -f "${OUTPUT_DIR}/dht-node-${ARCH}.gz.b64" ]; then
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
# Build
# =====================================================

# Ensure dependencies are resolved (always run — Go version upgrades may
# require go.sum updates even when the file already exists)
echo "Resolving Go dependencies..."
go mod tidy

for ARCH in $ARCHITECTURES; do
    BINARY_NAME="dht-node-${ARCH}"
    GZB64_NAME="${BINARY_NAME}.gz.b64"
    BINARY_PATH="${OUTPUT_DIR}/${BINARY_NAME}"
    GZB64_PATH="${OUTPUT_DIR}/${GZB64_NAME}"

    echo "Building for linux/${ARCH}..."

    CGO_ENABLED=0 GOOS=linux GOARCH="${ARCH}" \
        go build -trimpath -ldflags="-s -w" -o "${BINARY_PATH}" .

    BINARY_SIZE=$(stat -c%s "${BINARY_PATH}" 2>/dev/null || stat -f%z "${BINARY_PATH}" 2>/dev/null)
    echo "  Binary: ${BINARY_PATH} ($(( BINARY_SIZE / 1024 / 1024 ))MB)"

    # Gzip + base64 encode (cloud-init uses encoding: gz+b64 to decode)
    gzip -9 -c "${BINARY_PATH}" | base64 -w0 > "${GZB64_PATH}"
    GZB64_SIZE=$(stat -c%s "${GZB64_PATH}" 2>/dev/null || stat -f%z "${GZB64_PATH}" 2>/dev/null)
    echo "  Gzip+Base64: ${GZB64_PATH} ($(( GZB64_SIZE / 1024 / 1024 ))MB, $(( GZB64_SIZE * 100 / BINARY_SIZE ))% of original)"

    # Clean up raw binary (only the .gz.b64 is needed at runtime)
    rm -f "${BINARY_PATH}"

    echo "  Done."
    echo ""
done

# Save source hash so we can detect stale binaries next time
echo "$SOURCE_HASH" > "$HASH_FILE"
echo "Saved source hash to ${HASH_FILE}"

echo "=== Build complete ==="
ls -lh "${OUTPUT_DIR}"/*.gz.b64 2>/dev/null || echo "(no .gz.b64 files found — build may have failed)"
