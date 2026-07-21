#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: sync-media-downloads.sh [--force] <downloads_dir> <sync_dir> <owner[:group]>

Copies audio/video files from <downloads_dir> into <sync_dir> for cron jobs. The
destination filename is "<artist> - <original filename>", where <artist> is the
source file's parent directory name.

Examples:
  sync-media-downloads.sh /srv/getoffline/downloads /mnt/media getoffline:getoffline
  sync-media-downloads.sh "$HOME/Downloads/GetOffline" /media/offline "$USER"

Environment:
  DRY_RUN=1        Print planned copies without writing files.
  FORCE_RESYNC=1   Re-copy files even when the destination is up to date.
  VERBOSE=1        Print skipped up-to-date files.

Options:
  -f, --force      Force a resync (equivalent to FORCE_RESYNC=1).

Validation:
  ffprobe must find an audio or video stream before a file is published. Invalid
  sources are reported as failures and an existing destination is left untouched.

Ownership:
  Every destination file is chown'ed recursively as <owner[:group]> after each run.
USAGE
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

FORCE_RESYNC=${FORCE_RESYNC:-0}
if [[ ${1:-} == "-f" || ${1:-} == "--force" ]]; then
  FORCE_RESYNC=1
  shift
fi

if [[ $# -ne 3 ]]; then
  usage >&2
  exit 64
fi

DOWNLOADS_DIR=$1
SYNC_DIR=$2
OWNER=$3
DRY_RUN=${DRY_RUN:-0}
VERBOSE=${VERBOSE:-0}

if [[ ! -d "$DOWNLOADS_DIR" ]]; then
  echo "downloads directory does not exist: $DOWNLOADS_DIR" >&2
  exit 66
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required but was not found on PATH" >&2
  exit 69
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe is required but was not found on PATH" >&2
  exit 69
fi

mkdir -p "$SYNC_DIR"

# Validate the requested owner before doing copy work. chown accepts either
# user, user:group, or :group; keep that flexibility for cron deployments.
if [[ $DRY_RUN != "1" ]]; then
  chown -R "$OWNER" "$SYNC_DIR"
fi

sanitize_component() {
  local value=$1
  value=${value//$'\n'/ }
  value=${value//$'\r'/ }
  value=${value//\//-}
  value=${value//\\/-}
  value=${value//:/-}
  value=${value//\*/-}
  value=${value//\?/-}
  value=${value//\"/-}
  value=${value//</-}
  value=${value//>/-}
  value=${value//|/-}
  # Collapse leading/trailing whitespace and dots that are awkward on common filesystems.
  value=$(printf '%s' "$value" | sed -E 's/[[:space:]]+/ /g; s/^[ .-]+//; s/[ .-]+$//')
  printf '%s' "${value:-Unknown Artist}"
}

is_media_file() {
  case "${1,,}" in
    *.aac|*.aiff|*.aif|*.alac|*.flac|*.m4a|*.m4v|*.mka|*.mkv|*.mov|*.mp3|*.mp4|*.mpeg|*.mpg|*.oga|*.ogg|*.opus|*.wav|*.webm|*.wma|*.wmv) return 0 ;;
    *) return 1 ;;
  esac
}

verify_media() {
  local media_path=$1
  local streams

  streams=$(ffprobe -v error -show_entries stream=codec_type \
    -of default=noprint_wrappers=1:nokey=1 "$media_path") || return 1
  grep -Eq '^(audio|video)$' <<<"$streams"
}

copied=0
skipped=0
failed=0

while IFS= read -r -d '' source_path; do
  source_basename=$(basename "$source_path")

  if ! is_media_file "$source_path"; then
    continue
  fi

  parent_dir=$(basename "$(dirname "$source_path")")
  artist=$(sanitize_component "$parent_dir")
  filename=$(sanitize_component "$source_basename")
  dest_path="$SYNC_DIR/$artist - $filename"

  if ! verify_media "$source_path"; then
    ((failed += 1))
    echo "invalid media (ffprobe): $source_path" >&2
    continue
  fi

  if [[ $FORCE_RESYNC != "1" && -e "$dest_path" && ! "$source_path" -nt "$dest_path" ]] && verify_media "$dest_path"; then
    ((skipped += 1))
    if [[ $VERBOSE == "1" ]]; then
      echo "skip: $dest_path"
    fi
    continue
  fi

  echo "copy: $source_path -> $dest_path"
  if [[ $DRY_RUN == "1" ]]; then
    ((copied += 1))
    continue
  fi

  temp_path=$(mktemp --tmpdir="$SYNC_DIR" ".sync-media.XXXXXX")
  if rsync -a -- "$source_path" "$temp_path" && verify_media "$temp_path" && mv -f -- "$temp_path" "$dest_path" && chown -R "$OWNER" "$dest_path"; then
    ((copied += 1))
  else
    rm -f -- "$temp_path"
    ((failed += 1))
    echo "failed: $source_path" >&2
  fi
done < <(find "$DOWNLOADS_DIR" -type f -print0)

if [[ $DRY_RUN != "1" ]]; then
  chown -R "$OWNER" "$SYNC_DIR"
fi

echo "sync complete: copied=$copied skipped=$skipped failed=$failed destination=$SYNC_DIR"

if [[ $failed -gt 0 ]]; then
  exit 1
fi
