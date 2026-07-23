# Cron jobs

The Python implementation in this directory can be run directly by the host
cron daemon or from a worker image. It accepts the same positional arguments and environment flags as
`scripts/sync-media-downloads.sh`:

```text
sync_media_downloads.py [--force] <downloads_dir> <sync_dir> <owner[:group]>
```

Example:

```cron
*/15 * * * * /path/to/getoffline/.venv/bin/python /path/to/getoffline/crons/sync_media_downloads.py /srv/getoffline/downloads /mnt/offline-media jellyfin:jellyfin >> /var/log/getoffline-media-sync.log 2>&1
```

To run it interactively with the same image used by the download workers:

```bash
docker build -f deploy/docker/worker-download.Dockerfile \
  -t getoffline-worker-download:local .

DOWNLOADS_DIR="${GETOFFLINE_DOWNLOADS_DIR:-$PWD/downloads}"
SYNC_DIR="${GETOFFLINE_SYNC_DIR:-$PWD/offline-media}"
mkdir -p "$SYNC_DIR"

docker run --rm -it \
  --entrypoint python \
  -v "$PWD/crons:/app/crons:ro" \
  -v "$DOWNLOADS_DIR:/app/downloads:ro" \
  -v "$SYNC_DIR:/app/sync" \
  getoffline-worker-download:local \
  /app/crons/sync_media_downloads.py \
  /app/downloads /app/sync "$(id -u):$(id -g)"
```

The worker image provides Python and `ffprobe`; the `crons` directory is
mounted so the command always runs the local version of the sync tool. The
host `SYNC_DIR` is the directory shared with the consuming Docker container.

The destination can be any host path, including a directory that is mounted
into another Docker container. The script uses a temporary file and atomic
rename so a consumer never sees a partially copied media file. It validates
audio/video streams with `ffprobe`, so `ffprobe` must be installed on the host.

Set `DRY_RUN=1` to preview copies, `FORCE_RESYNC=1` or `--force` to copy files
regardless of timestamps, and `VERBOSE=1` to print skipped up-to-date files.
Ownership accepts names or numeric IDs in `user[:group]` or `uid[:gid]` form.
