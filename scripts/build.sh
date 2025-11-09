#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/src"
BIN_DIR="$ROOT_DIR/bin"
VENDOR_DIR="$ROOT_DIR/.vendor"
PYTHON_VENDOR_DIR="$VENDOR_DIR/python"
COOMER_REPO="https://github.com/Emy69/Coomer-cli.git"
COOMER_DIR="$VENDOR_DIR/Coomer-cli"

mkdir -p "$VENDOR_DIR"

if [ -d "$COOMER_DIR" ]; then
    rm -rf "$COOMER_DIR"
fi

if [ -d "$PYTHON_VENDOR_DIR" ]; then
    rm -rf "$PYTHON_VENDOR_DIR"
fi

mkdir -p "$PYTHON_VENDOR_DIR"

echo "Cloning Coomer CLI from $COOMER_REPO ..."
if ! git clone --depth=1 "$COOMER_REPO" "$COOMER_DIR"; then
    echo "Failed to clone Coomer CLI repository" >&2
    exit 1
fi

copy_python_package() {
    local source_dir="$1"
    local package_name="$2"

    if [ ! -d "$source_dir" ]; then
        return
    fi

    rm -rf "$PYTHON_VENDOR_DIR/$package_name"
    cp -R "$source_dir" "$PYTHON_VENDOR_DIR/$package_name"
}

copy_python_modules() {
    local source_root="$1"

    find "$source_root" -maxdepth 1 -type f -name "*.py" -print0 | while IFS= read -r -d '' file; do
        local filename
        filename="$(basename "$file")"
        cp "$file" "$PYTHON_VENDOR_DIR/$filename"
    done
}

copy_python_package "$COOMER_DIR/coomer_cli" "coomer_cli"
copy_python_package "$COOMER_DIR/coomer" "coomer"
copy_python_modules "$COOMER_DIR"

if [ ! -d "$PYTHON_VENDOR_DIR/coomer_cli" ]; then
    echo "Failed to stage Coomer CLI Python sources" >&2
    exit 1
fi

declare -a SOURCE_ARGS=(
    "--sources-directory=$SRC_DIR"
    "--sources-directory=$PYTHON_VENDOR_DIR"
)

mkdir -p "$BIN_DIR"

pex "${SOURCE_ARGS[@]}" \
    -r "$SRC_DIR/requirements.txt" \
    -o "$BIN_DIR/getthem" \
    -m downloads \
    --venv append
