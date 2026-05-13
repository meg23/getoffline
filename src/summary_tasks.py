import re
import sqlite3
from pathlib import Path
from typing import List, Tuple

from logger import get_logger
from summarization import summarize_segments

log = get_logger("summary")


def _parse_srt_segments(subtitle_path: Path) -> List[Tuple[float, float, str]]:
    text = subtitle_path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: List[Tuple[float, float, str]] = []
    for block in blocks:
        lines: List[str] = []
        for raw_line in block.splitlines():
            stripped_line = raw_line.strip()
            if stripped_line:
                lines.append(stripped_line)
        if len(lines) < 2:
            continue
        ts_line = lines[1] if re.search(r"-->", lines[1]) else lines[0]
        if "-->" not in ts_line:
            continue
        body = " ".join(lines[2:] if ts_line == lines[1] else lines[1:]).strip()
        if body:
            segments.append((0.0, 0.0, body))
    return segments


def _load_segments_from_subtitle(path: Path) -> List[str]:
    if not path.exists() or path.suffix.lower() != ".srt":
        return []
    segments: List[str] = []
    for segment in _parse_srt_segments(path):
        text_value = segment[2]
        if text_value:
            segments.append(text_value)
    return segments


def clear_all_summaries(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        deleted = conn.execute("DELETE FROM media_summaries").rowcount
        conn.commit()
    log.info("Cleared all media summaries rows=%s", deleted)
    return int(deleted or 0)


def generate_missing_summaries(db_path: str, limit: int = 20) -> int:
    generated = 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.id, COALESCE(d.title, ''), COALESCE(d.subtitle_path, '')
            FROM downloads d
            LEFT JOIN media_summaries ms ON ms.download_id = d.id
            WHERE d.download_status = 'downloaded'
              AND COALESCE(d.subtitle_path, '') <> ''
              AND (ms.download_id IS NULL OR COALESCE(ms.summary_text, '') = '')
            ORDER BY d.last_seen_at DESC, d.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    for row_id, title, subtitle_path in rows:
        path = Path(str(subtitle_path)).expanduser().resolve()
        segments = _load_segments_from_subtitle(path)
        if not segments:
            log.info("Summary skipped id=%s title=%s reason=no_segments", row_id, title)
            continue
        result = summarize_segments(segments, mode="subprocess")
        summary = str(result.get("summary_text") or "").strip()
        if not summary:
            log.warning("Summary generation empty for id=%s title=%s", row_id, title)
            continue
        model_name = str(result.get("model_name") or "unknown")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO media_summaries (download_id, summary_text, model_name, source_segment_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(download_id) DO UPDATE SET
                  summary_text=excluded.summary_text,
                  model_name=excluded.model_name,
                  source_segment_count=excluded.source_segment_count,
                  updated_at=excluded.updated_at
                """,
                (int(row_id), summary, model_name, len(segments), str(result.get("updated_at") or "")),
            )
            conn.commit()
        generated += 1
        log.info("Summary generated id=%s title=%s model=%s chars=%s", row_id, title, model_name, len(summary))

    if rows:
        log.info("Summary generation pass complete candidates=%s generated=%s", len(rows), generated)
    return generated
