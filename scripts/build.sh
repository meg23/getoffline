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

if ! COOMER_DIR="$COOMER_DIR" PYTHON_VENDOR_DIR="$PYTHON_VENDOR_DIR" python3 - <<'PY'; then
    echo "Failed to stage Coomer CLI Python sources" >&2
    exit 1
fi

import os
import shutil
import sys
from pathlib import Path

coomer_dir = Path(os.environ["COOMER_DIR"])
vendor_dir = Path(os.environ["PYTHON_VENDOR_DIR"])

staged_anything = False


def relative_target(path: Path) -> Path:
    """Return the vendor destination relative path for a staged source directory."""

    parts = list(path.relative_to(coomer_dir).parts)
    if parts and parts[0] in {"src", "python"}:
        parts = parts[1:]
    if not parts:
        # Nothing meaningful to stage if we ended up at the checkout root.
        return None
    return Path(*parts)


try:
    package_dirs = set()
    for init_file in coomer_dir.rglob("__init__.py"):
        package_dirs.add(init_file.parent)

    for package_dir in sorted(package_dirs, key=lambda p: p.as_posix()):
        target_rel = relative_target(package_dir)
        if target_rel is None:
            continue

        destination = vendor_dir / target_rel
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            package_dir,
            destination,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        staged_anything = True

    for module in coomer_dir.glob("*.py"):
        vendor_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module, vendor_dir / module.name)
        staged_anything = True

except Exception as exc:  # pragma: no cover - defensive, script context only
    print(f"Encountered error while staging Coomer CLI sources: {exc}", file=sys.stderr)
    sys.exit(2)

if not staged_anything:
    print("No Python modules discovered in Coomer CLI checkout", file=sys.stderr)
    sys.exit(1)
PY

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
