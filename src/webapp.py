import html
import mimetypes
import os
import posixpath
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse


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


@dataclass
class AppState:
    output_root: Path
    database_path: Path


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
                   file_ext, file_size_bytes, upload_date
            FROM downloads
            WHERE download_status = 'downloaded'
            ORDER BY last_seen_at DESC, id DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        result.append(
            MediaRow(
                row_id=row[0],
                source_type=row[1],
                source_name=row[2],
                title=row[3],
                file_path=row[4],
                file_ext=row[5],
                file_size_bytes=row[6],
                upload_date=row[7],
            )
        )
    return result


def _render_index(rows: List[MediaRow], output_root: Path, database_path: Path) -> str:
    cards = []
    for row in rows:
        path = Path(row.file_path)
        if not row.file_path:
            continue
        safe = _resolve_safe_media_path(output_root, row.file_path)
        if not safe:
            continue

        title = html.escape(row.title or path.name)
        source = html.escape(f"{row.source_type}: {row.source_name}")
        size = html.escape(_human_size(row.file_size_bytes))
        ext = html.escape((row.file_ext or path.suffix.lstrip(".")) or "?")
        cards.append(
            f"""
            <tr>
                <td>{title}</td>
                <td>{source}</td>
                <td>{ext}</td>
                <td>{size}</td>
                <td><a href=\"/play?id={row.row_id}\">Play</a></td>
            </tr>
            """
        )

    table_rows = "\n".join(cards) if cards else "<tr><td colspan='5'>No playable media found yet.</td></tr>"
    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>GetOffline Media Library</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ border-bottom: 1px solid #ddd; padding: .5rem; text-align: left; }}
    a {{ color: #0a58ca; text-decoration: none; }}
  </style>
</head>
<body>
  <h1>GetOffline Media Library</h1>
  <p>Database: <code>{html.escape(str(database_path))}</code></p>
  <table>
    <thead><tr><th>Title</th><th>Source</th><th>Type</th><th>Size</th><th>Action</th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</body>
</html>"""


def _render_player(row: MediaRow, media_path: Path) -> str:
    title = html.escape(row.title or media_path.name)
    media_kind = "video" if media_path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} else "audio"
    source = html.escape(f"{row.source_type}: {row.source_name}")

    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>{title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; }}
    {media_kind} {{ width: 100%; max-width: 1000px; background: #000; }}
  </style>
</head>
<body>
  <p><a href=\"/\">← Back to Library</a></p>
  <h2>{title}</h2>
  <p>{source}</p>
  <{media_kind} controls preload=\"metadata\">
    <source src=\"/media?id={row.row_id}\" />
    Your browser does not support this media type.
  </{media_kind}>
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
                body = _render_index(rows, state.output_root, state.database_path)
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

        def log_message(self, fmt, *args):
            _ = fmt, args

    return _Handler


def run_webapp(output_root: str, database_path: str, host: str = "127.0.0.1", port: int = 8080):
    state = AppState(output_root=Path(output_root), database_path=Path(database_path))
    server = ThreadingHTTPServer((host, int(port)), make_handler(state))
    print(f"Web app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
