#!/usr/bin/env bash
# Install argo CLI binary for the current OS/arch from GitHub Releases.
#
# Environment variables (with defaults):
#   ARGO_VERSION   - argo version to install, e.g. v3.6.7  (default: v3.6.7)
#   INSTALL_DIR    - target directory for the binary         (default: /usr/local/bin)
#
# Usage:
#   bash scripts/install-argo.sh
#   ARGO_VERSION=v3.6.7 bash scripts/install-argo.sh

set -euo pipefail

ARGO_VERSION="${ARGO_VERSION:-v3.6.7}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  ARCH="amd64" ;;
  aarch64) ARCH="arm64" ;;
  arm64)   ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

BINARY_NAME="argo-${OS}-${ARCH}"
DOWNLOAD_URL="https://github.com/argoproj/argo-workflows/releases/download/${ARGO_VERSION}/${BINARY_NAME}.gz"

echo "Installing argo ${ARGO_VERSION} (${OS}/${ARCH}) to ${INSTALL_DIR}..."
echo "Download URL: ${DOWNLOAD_URL}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl -sSfL "${DOWNLOAD_URL}" -o "${TMP_DIR}/${BINARY_NAME}.gz"
gunzip "${TMP_DIR}/${BINARY_NAME}.gz"
chmod +x "${TMP_DIR}/${BINARY_NAME}"
mv "${TMP_DIR}/${BINARY_NAME}" "${INSTALL_DIR}/argo"

echo "argo installed: $(argo version --short 2>/dev/null || echo 'ok')"
