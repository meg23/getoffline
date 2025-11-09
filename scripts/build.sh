#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/src"
BIN_DIR="$ROOT_DIR/bin"
VENDOR_DIR="$ROOT_DIR/.vendor"
COOMER_REPO="https://github.com/Emy69/Coomer-cli.git"
COOMER_DIR="$VENDOR_DIR/Coomer-cli"

mkdir -p "$VENDOR_DIR"

if [ -d "$COOMER_DIR" ]; then
    rm -rf "$COOMER_DIR"
fi

echo "Cloning Coomer CLI from $COOMER_REPO ..."
if ! git clone --depth=1 "$COOMER_REPO" "$COOMER_DIR"; then
    echo "Failed to clone Coomer CLI repository" >&2
    exit 1
fi

declare -a SOURCE_ARGS=(
    "--sources-directory=$SRC_DIR"
    "--sources-directory=$COOMER_DIR"
)

if [ -d "$COOMER_DIR/src" ]; then
    SOURCE_ARGS+=("--sources-directory=$COOMER_DIR/src")
fi

mkdir -p "$BIN_DIR"

pex "${SOURCE_ARGS[@]}" \
    -r "$SRC_DIR/requirements.txt" \
    -o "$BIN_DIR/getthem" \
    -m downloads \
    --venv append
