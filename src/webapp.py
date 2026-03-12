import html
import mimetypes
import posixpath
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from database import mark_all_downloads_played, mark_download_played


MEDIA_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
}


@dataclass
class MediaRow:
    row_id: int
    source_type: str
    source_name: str
    title: str
    file_path: str
    file_ext: Optional[str]
    file_size_bytes: Optional[int]
    upload_date: Optional[str]
    played: bool


@dataclass
class UpdateStatus:
    lock: threading.Lock = field(default_factory=threading.Lock)
    is_running: bool = False
    last_started_at: Optional[float] = None
    last_finished_at: Optional[float] = None
    last_result: str = "idle"
    last_error: Optional[str] = None
    last_items_count: int = 0


@dataclass
class AppState:
    output_root: Path
    database_path: Path
    config: Dict
    update_runner: Callable[[Dict, List[str]], None]
    update_status: UpdateStatus = field(default_factory=UpdateStatus)


def _default_update_runner(config: Dict, downloaded_items: List[str]) -> None:
    from podcasts import download_podcasts
    from youtube import download_youtube_items

    download_youtube_items(config, downloaded_items)
    download_podcasts(config, downloaded_items)


def _human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "unknown"

    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def _resolve_safe_media_path(output_root: Path, candidate_path: str) -> Optional[Path]:
    candidate = Path(candidate_path).expanduser().resolve()
    root = output_root.expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file() or not _is_media_file(candidate):
        return None
    return candidate


def fetch_downloaded_media_rows(db_path: Path) -> List[MediaRow]:
    if not db_path.exists():
        return []

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, source_type, source_name, COALESCE(title, ''), COALESCE(file_path, ''),
                   file_ext, file_size_bytes, upload_date, COALESCE(played, 0)
            FROM downloads
            WHERE download_status = 'downloaded'
            ORDER BY last_seen_at DESC, id DESC
            """
        ).fetchall()

    return [
        MediaRow(
            row_id=row[0],
            source_type=row[1],
            source_name=row[2],
            title=row[3],
            file_path=row[4],
            file_ext=row[5],
            file_size_bytes=row[6],
            upload_date=row[7],
            played=bool(row[8]),
        )
        for row in rows
    ]


def _format_timestamp(ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _snapshot_status(status: UpdateStatus) -> Dict[str, str]:
    with status.lock:
        return {
            "is_running": "yes" if status.is_running else "no",
            "last_started_at": _format_timestamp(status.last_started_at),
            "last_finished_at": _format_timestamp(status.last_finished_at),
            "last_result": status.last_result,
            "last_error": status.last_error or "none",
            "last_items_count": str(status.last_items_count),
        }


def _run_update_job(state: AppState) -> None:
    downloaded_items: List[str] = []
    with state.update_status.lock:
        state.update_status.is_running = True
        state.update_status.last_started_at = time.time()
        state.update_status.last_result = "running"
        state.update_status.last_error = None
        state.update_status.last_items_count = 0

    try:
        state.update_runner(state.config, downloaded_items)
        with state.update_status.lock:
            state.update_status.last_result = "ok"
            state.update_status.last_items_count = len(downloaded_items)
    except Exception as exc:
        with state.update_status.lock:
            state.update_status.last_result = "failed"
            state.update_status.last_error = str(exc)
    finally:
        with state.update_status.lock:
            state.update_status.is_running = False
            state.update_status.last_finished_at = time.time()


def trigger_background_update(state: AppState) -> bool:
    with state.update_status.lock:
        if state.update_status.is_running:
            return False

    thread = threading.Thread(target=_run_update_job, args=(state,), daemon=True)
    thread.start()
    return True


def _render_index(rows: List[MediaRow], output_root: Path, database_path: Path, status: Dict[str, str]) -> str:
    cards = []
    visible_rows = []
    for row in rows:
        path = Path(row.file_path)
        if not row.file_path:
            continue
        safe = _resolve_safe_media_path(output_root, row.file_path)
        if not safe:
            continue

        visible_rows.append(row)
        if row.played:
            continue
        title = html.escape(row.title or path.name)
        source = html.escape(f"{row.source_type}: {row.source_name}")
        size = html.escape(_human_size(row.file_size_bytes))
        ext = html.escape((row.file_ext or path.suffix.lstrip(".")) or "?")
        status_label = "played" if row.played else "new"
        status_class = "status-played" if row.played else "status-new"
        mark_action = "unplay" if row.played else "played"
        mark_label = "Mark unplayed" if row.played else "Mark played"
        cards.append(
            f"""
            <tr>
                <td class="title-cell" data-label="Title">{title}</td>
                <td data-label="Source">{source}</td>
                <td data-label="Type"><span class="pill">{ext}</span></td>
                <td data-label="Size">{size}</td>
                <td data-label="Status"><span class="pill {status_class}">{status_label}</span></td>
                <td class="actions" data-label="Action">
                  <a class="btn btn-link" href="/play?id={row.row_id}">Play</a>
                  <form method="post" action="/mark-{mark_action}" class="inline-form">
                    <input type="hidden" name="id" value="{row.row_id}" />
                    <button class="btn btn-subtle" type="submit">{mark_label}</button>
                  </form>
                </td>
            </tr>
            """
        )

    table_rows = "\n".join(cards) if cards else "<tr><td colspan='6'>No playable media found yet.</td></tr>"
    button_disabled = "disabled" if status["is_running"] == "yes" else ""
    total_items = len(visible_rows)
    played_items = sum(1 for item in visible_rows if item.played)
    unplayed_items = max(total_items - played_items, 0)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GetOffline Media Library</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-2: #f3f6ff;
      --text: #17213a;
      --muted: #5d6780;
      --accent: #2f62f2;
      --accent-2: #1f4fe0;
      --ok-bg: #dbf8e8;
      --ok-text: #0f7a43;
      --new-bg: #e7ecff;
      --new-text: #3147aa;
      --border: #dbe3f3;
      --shadow: 0 10px 30px rgba(40, 65, 120, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: linear-gradient(180deg, #f8faff 0%, #f3f6fc 100%);
      color: var(--text);
      padding: 1rem;
    }}
    .container {{ max-width: 1280px; margin: 0 auto; }}
    .hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
      padding: 1.1rem 1.1rem .95rem;
      margin-bottom: 1rem;
    }}
    h1 {{ margin: 0 0 .35rem 0; font-size: clamp(1.5rem, 2.8vw, 2.1rem); }}
    .meta {{ color: var(--muted); margin: 0; font-size: .95rem; }}
    .meta code {{ color: #263a78; background: #eef3ff; padding: .12rem .4rem; border-radius: 6px; }}

    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: .65rem;
      margin: .85rem 0 .2rem;
    }}
    .summary-card {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: .55rem .65rem;
    }}
    .summary-label {{ color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; }}
    .summary-value {{ font-weight: 700; margin-top: .15rem; }}

    .panel {{
      margin: 0 0 1rem;
      padding: 1rem;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--surface);
      box-shadow: var(--shadow);
      display: grid;
      gap: .35rem;
    }}
    .status-line {{ color: var(--muted); margin: 0; }}
    .status-line strong {{ color: var(--text); }}
    .toolbar {{ display: flex; justify-content: flex-start; margin-bottom: .35rem; }}

    table {{
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      overflow: hidden;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    thead th {{
      background: var(--surface-2);
      color: #3f4e75;
      text-align: left;
      font-weight: 600;
      letter-spacing: .02em;
      font-size: .9rem;
      padding: .7rem .75rem;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    td {{
      border-bottom: 1px solid #edf1fa;
      padding: .7rem .75rem;
      vertical-align: middle;
      color: var(--text);
    }}
    tbody tr:nth-child(even) td {{ background: #fbfcff; }}
    tr:last-child td {{ border-bottom: none; }}
    .title-cell {{ max-width: 520px; font-weight: 600; overflow-wrap: anywhere; line-height: 1.25; }}
    .pill {{
      display: inline-block;
      padding: .18rem .5rem;
      border-radius: 999px;
      background: #eef3ff;
      color: #43507b;
      font-size: .78rem;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .status-played {{ background: var(--ok-bg); color: var(--ok-text); }}
    .status-new {{ background: var(--new-bg); color: var(--new-text); }}
    .actions {{ white-space: nowrap; }}
    .inline-form {{ display:inline; margin-left:.5rem; }}

    .btn {{
      border: 1px solid transparent;
      border-radius: 9px;
      padding: .38rem .7rem;
      font-size: .88rem;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
      color: #fff;
      background: transparent;
    }}
    .btn-link {{ background: var(--accent); }}
    .btn-link:hover {{ background: var(--accent-2); }}
    .btn-subtle {{ border-color: #c9d5ef; color: #2c3e74; }}
    .btn-subtle:hover {{ background: #eef3ff; }}
    .btn-update {{ background: linear-gradient(180deg, #4f7fff, #3f6ff1); }}
    .btn-update:disabled {{ opacity: .5; cursor: not-allowed; }}

    @media (max-width: 980px) {{
      .summary-grid {{ grid-template-columns: 1fr; }}
      .actions {{ white-space: normal; }}
      .inline-form {{ margin-left: 0; margin-top: .35rem; display: inline-block; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--border); padding: .4rem 0; }}
      td {{ border: none; display: flex; gap: .6rem; align-items: center; }}
      tbody tr:nth-child(even) td {{ background: transparent; }}
      td::before {{
        content: attr(data-label);
        min-width: 70px;
        font-size: .75rem;
        text-transform: uppercase;
        color: var(--muted);
        letter-spacing: .05em;
      }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="hero">
      <h1>GetOffline Media Library</h1>
      <p class="meta">Database: <code>{html.escape(str(database_path))}</code></p>
      <div class="summary-grid">
        <div class="summary-card">
          <div class="summary-label">Visible Items</div>
          <div class="summary-value">{total_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Played</div>
          <div class="summary-value">{played_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">New</div>
          <div class="summary-value">{unplayed_items}</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <form method="post" action="/update" class="toolbar">
        <button class="btn btn-update" type="submit" {button_disabled}>Update Downloads</button>
      </form>
      <form method="post" action="/mark-all-played" class="toolbar">
        <button class="btn btn-subtle" type="submit" {'disabled' if unplayed_items == 0 else ''}>Mark all as played</button>
      </form>
      <p class="status-line">Status: <strong>{html.escape(status['last_result'])}</strong> (running: {html.escape(status['is_running'])})</p>
      <p class="status-line">Last started: {html.escape(status['last_started_at'])} | Last finished: {html.escape(status['last_finished_at'])}</p>
      <p class="status-line">Items downloaded in last run: {html.escape(status['last_items_count'])}</p>
      <p class="status-line">Last error: {html.escape(status['last_error'])}</p>
    </div>

    <table>
      <thead><tr><th>Title</th><th>Source</th><th>Type</th><th>Size</th><th>Status</th><th>Action</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</body>
</html>"""


def _render_player(row: MediaRow, media_path: Path) -> str:
    title = html.escape(row.title or media_path.name)
    media_kind = "video" if media_path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} else "audio"
    source = html.escape(f"{row.source_type}: {row.source_name}")

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: #0b1020;
      color: #e9eefc;
      padding: 1.25rem;
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    a {{ color: #9dbbff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .meta {{ color: #a9b4d0; margin: .25rem 0 1rem 0; }}
    .player {{
      width: 100%;
      max-width: 1000px;
      background: #000;
      border: 1px solid #2a3761;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,.35);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <p><a href="/">← Back to Library</a></p>
    <h2>{title}</h2>
    <p class="meta">{source}</p>
    <{media_kind} class="player" controls preload="metadata">
      <source src="/media?id={row.row_id}" />
      Your browser does not support this media type.
    </{media_kind}>
  </div>
</body>
</html>"""


def _find_row_by_id(rows: List[MediaRow], row_id: int) -> Optional[MediaRow]:
    for row in rows:
        if row.row_id == row_id:
            return row
    return None


def _parse_range_header(range_header: str, file_size: int) -> Optional[Dict[str, int]]:
    if not range_header or not range_header.startswith("bytes="):
        return None

    value = range_header[len("bytes="):].strip()
    if "," in value:
        return None

    start_text, _, end_text = value.partition("-")
    if not start_text and not end_text:
        return None

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix = int(end_text)
        if suffix <= 0:
            return None
        start = max(0, file_size - suffix)
        end = file_size - 1

    if start > end or start < 0 or end >= file_size:
        return None

    return {"start": start, "end": end}


def _stream_media(handler: BaseHTTPRequestHandler, media_path: Path) -> None:
    file_size = media_path.stat().st_size
    content_type = mimetypes.guess_type(str(media_path))[0] or "application/octet-stream"

    range_header = handler.headers.get("Range")
    parsed = _parse_range_header(range_header, file_size) if range_header else None

    if parsed is None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()

        with media_path.open("rb") as f:
            handler.wfile.write(f.read())
        return

    start = parsed["start"]
    end = parsed["end"]
    length = end - start + 1

    handler.send_response(206)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
    handler.send_header("Accept-Ranges", "bytes")
    handler.end_headers()

    with media_path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def make_handler(state: AppState):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = posixpath.normpath(parsed.path)
            query = parse_qs(parsed.query)
            rows = fetch_downloaded_media_rows(state.database_path)

            if path == "/":
                status = _snapshot_status(state.update_status)
                body = _render_index(rows, state.output_root, state.database_path, status)
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path in {"/play", "/media"}:
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                row = _find_row_by_id(rows, int(raw_id))
                if row is None:
                    self.send_error(404, "Item not found")
                    return

                media_path = _resolve_safe_media_path(state.output_root, row.file_path)
                if media_path is None:
                    self.send_error(404, "Media file unavailable")
                    return

                if path == "/play":
                    body = _render_player(row, media_path)
                    body_bytes = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return

                _stream_media(self, media_path)
                return

            self.send_error(404, "Not found")

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = posixpath.normpath(parsed.path)

            if path == "/update":
                trigger_background_update(state)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path in {"/mark-played", "/mark-unplay"}:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                raw_id = (form.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                played_value = path == "/mark-played"
                mark_download_played(str(state.database_path), int(raw_id), played=played_value)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/mark-all-played":
                mark_all_downloads_played(str(state.database_path))
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            self.send_error(404, "Not found")

        def log_message(self, fmt, *args):
            _ = fmt, args

    return _Handler


def run_webapp(config: Dict, host: str = "127.0.0.1", port: int = 8080):
    defaults = config["defaults"]
    state = AppState(
        output_root=Path(defaults["output_root"]),
        database_path=Path(defaults["database_path"]),
        config=config,
        update_runner=_default_update_runner,
    )
    server = ThreadingHTTPServer((host, int(port)), make_handler(state))
    print(f"Web app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
