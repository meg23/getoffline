import hashlib
from typing import Iterable

import feedparser
from yt_dlp import YoutubeDL

from app.queue import publish_job
from logger import get_logger
from models.jobs import create_job
from models.models import Download, Job, SourceConfig


log = get_logger("workers.handlers")


class _WorkerYtDlpLogger:
    def debug(self, msg):
        if msg:
            log.info("yt-dlp debug: %s", msg)

    def warning(self, msg):
        if msg:
            log.warning("yt-dlp warning: %s", msg)

    def error(self, msg):
        if msg:
            log.error("yt-dlp error: %s", msg)


def _yt_dlp_progress_hook(event: dict) -> None:
    status = event.get("status")
    filename = event.get("filename") or event.get("tmpfilename")
    downloaded = event.get("downloaded_bytes")
    total = event.get("total_bytes") or event.get("total_bytes_estimate")
    speed = event.get("speed")
    eta = event.get("eta")
    if status == "downloading":
        log.info(
            "yt-dlp downloading filename=%s downloaded_bytes=%s total_bytes=%s speed=%s eta=%s",
            filename,
            downloaded,
            total,
            speed,
            eta,
        )
    elif status == "finished":
        log.info("yt-dlp download finished filename=%s total_bytes=%s", filename, total or downloaded)
    elif status == "error":
        log.error("yt-dlp download error filename=%s event=%s", filename, event)
    else:
        log.info("yt-dlp progress status=%s filename=%s event=%s", status, filename, event)


def _yt_dlp_base_options(**overrides) -> dict:
    options = {
        "logger": _WorkerYtDlpLogger(),
        "progress_hooks": [_yt_dlp_progress_hook],
        "verbose": True,
        "quiet": False,
        "no_warnings": False,
    }
    options.update(overrides)
    return options


def _log_youtube_response(prefix: str, payload: dict) -> None:
    entries = payload.get("entries") if isinstance(payload, dict) else None
    log.info(
        "%s extractor=%s extractor_key=%s id=%s title=%s webpage_url=%s entries=%s live_status=%s availability=%s",
        prefix,
        payload.get("extractor"),
        payload.get("extractor_key"),
        payload.get("id"),
        payload.get("title"),
        payload.get("webpage_url"),
        len(entries or []) if entries is not None else 0,
        payload.get("live_status"),
        payload.get("availability"),
    )


def _publish_created_job(job: Job) -> None:
    log.info("Publishing child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    log.info("Published child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)


def _fallback_uid(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    return f"generated:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def _idempotency_key(*parts: object) -> str:
    text = "|".join(str(part or "") for part in parts).strip() or "unknown"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    prefix = ":".join(str(part or "") for part in parts[:3])[:160]
    return f"{prefix}:{digest}"[:255]


def _episode_was_downloaded(*, profile_id: str, source: SourceConfig, item_uid: str, item_url: str, title: str) -> bool:
    rows = Download.objects.filter(profile_id=profile_id, source_type=source.source_type, source_name=source.name)
    if item_uid and rows.filter(item_uid=item_uid).exists():
        return True
    if item_url and rows.filter(item_url=item_url).exists():
        return True
    if title and rows.filter(title=title).exists():
        return True
    return False


def _source_limit(source: SourceConfig) -> int:
    return max(1, int(source.max_downloads or 10))


def _podcast_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info("Checking podcast feed source_id=%s source_name=%s url=%s", source.id, source.name, source.url)
    feed = feedparser.parse(source.url)
    if getattr(feed, "bozo", False):
        log.warning("Podcast feed parse warning source_id=%s source_name=%s error=%s", source.id, source.name, getattr(feed, "bozo_exception", "unknown"))
    entries = list(getattr(feed, "entries", []) or [])[: _source_limit(source)]
    feed_meta = getattr(feed, "feed", {}) or {}
    feed_title = str(getattr(feed_meta, "title", "") or getattr(feed_meta, "get", lambda _key, _default="": _default)("title", "") or "")
    log.info("Podcast feed parsed source_id=%s source_name=%s feed_title=%s entries_considered=%s limit=%s", source.id, source.name, feed_title, len(entries), _source_limit(source))
    for entry in entries:
        enclosure_url = ""
        for enclosure in getattr(entry, "enclosures", []) or []:
            enclosure_url = str(getattr(enclosure, "href", "") or enclosure.get("href", "")).strip()
            if enclosure_url:
                break
        item_url = enclosure_url or str(getattr(entry, "link", "") or "").strip()
        title = str(getattr(entry, "title", "") or item_url or "Untitled podcast episode").strip()
        published = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "").strip()
        item_uid = str(getattr(entry, "id", "") or getattr(entry, "guid", "") or item_url or "").strip()
        item_uid = item_uid or _fallback_uid(source.url, title, published)
        log.info("Podcast episode candidate source_id=%s source_name=%s item_uid=%s title=%s media_url=%s published=%s", source.id, source.name, item_uid[:255], title, enclosure_url or item_url, published)
        yield {
            "item_uid": item_uid[:255],
            "item_url": item_url,
            "media_url": enclosure_url or item_url,
            "title": title,
            "published": published,
        }


def _youtube_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info("Checking YouTube source source_id=%s source_name=%s url=%s", source.id, source.name, source.url)
    ydl_opts = _yt_dlp_base_options(
        extract_flat=True,
        skip_download=True,
        playlistend=_source_limit(source),
    )
    log.info("yt-dlp extract starting source_id=%s source_name=%s url=%s options=%s", source.id, source.name, source.url, {k: v for k, v in ydl_opts.items() if k not in {"logger", "progress_hooks"}})
    with YoutubeDL(ydl_opts) as ydl:
        payload = ydl.extract_info(source.url, download=False) or {}
    if isinstance(payload, dict):
        _log_youtube_response("yt-dlp extract response", payload)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not entries:
        entries = [payload]
    entries = list(entries or [])[: _source_limit(source)]
    log.info("YouTube source parsed source_id=%s source_name=%s entries=%s", source.id, source.name, len(entries))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "").strip()
        item_url = str(entry.get("url") or entry.get("webpage_url") or "").strip()
        if item_url and item_url.startswith("http") is False and item_id:
            item_url = f"https://www.youtube.com/watch?v={item_id}"
        title = str(entry.get("title") or item_url or "Untitled YouTube episode").strip()
        item_uid = item_id or item_url or _fallback_uid(source.url, title)
        yield {
            "item_uid": item_uid[:255],
            "item_url": item_url,
            "media_url": item_url,
            "title": title,
            "published": str(entry.get("upload_date") or entry.get("timestamp") or ""),
        }


def _candidates_for_source(source: SourceConfig) -> Iterable[dict]:
    if source.source_type == SourceConfig.SOURCE_PODCAST:
        return _podcast_candidates(source)
    if source.source_type == SourceConfig.SOURCE_YOUTUBE:
        return _youtube_candidates(source)
    log.warning("Unsupported source type for episode check source_id=%s source_type=%s", source.id, source.source_type)
    return []


def check_for_episodes(job: Job) -> None:
    """Serial discovery worker: scan every profile's sources and enqueue never-downloaded episodes."""
    profile_ids = list(
        SourceConfig.objects.filter(enabled=True)
        .order_by("profile_id")
        .values_list("profile_id", flat=True)
        .distinct()
    )
    log.info("Episode check started job_id=%s profiles=%s", job.id, len(profile_ids))
    total_sources = 0
    total_seen = 0
    total_enqueued = 0
    for profile_id in profile_ids:
        sources = list(SourceConfig.objects.filter(profile_id=profile_id, enabled=True).order_by("position", "id"))
        log.info("Episode check profile started job_id=%s profile_id=%s sources=%s", job.id, profile_id, len(sources))
        for source in sources:
            total_sources += 1
            source_seen = 0
            source_enqueued = 0
            log.info("Episode check source started profile_id=%s source_id=%s source_type=%s source_name=%s url=%s", profile_id, source.id, source.source_type, source.name, source.url)
            for candidate in _candidates_for_source(source):
                source_seen += 1
                total_seen += 1
                item_uid = str(candidate.get("item_uid") or "")[:255]
                item_url = str(candidate.get("item_url") or "")
                title = str(candidate.get("title") or "")
                if _episode_was_downloaded(
                    profile_id=profile_id,
                    source=source,
                    item_uid=item_uid,
                    item_url=item_url,
                    title=title,
                ):
                    log.info(
                        "Episode already downloaded profile_id=%s source_id=%s item_uid=%s title=%s",
                        profile_id,
                        source.id,
                        item_uid,
                        title,
                    )
                    continue
                if source.source_type == SourceConfig.SOURCE_PODCAST:
                    log.info("New podcast episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s media_url=%s", profile_id, source.id, source.name, item_uid, title, candidate.get("media_url") or item_url)
                elif source.source_type == SourceConfig.SOURCE_YOUTUBE:
                    log.info("New YouTube episode found profile_id=%s source_id=%s source_name=%s item_uid=%s title=%s item_url=%s", profile_id, source.id, source.name, item_uid, title, item_url)
                child = create_job(
                    profile_id=profile_id,
                    job_type="download_episode",
                    payload={
                        "source_id": source.id,
                        "source_type": source.source_type,
                        "source_name": source.name,
                        "source_url": source.url,
                        "item_uid": item_uid,
                        "item_url": item_url,
                        "media_url": candidate.get("media_url") or item_url,
                        "title": title,
                        "published": candidate.get("published") or "",
                    },
                    idempotency_key=_idempotency_key("download_episode", profile_id, source.id, item_uid or item_url or title),
                )
                _publish_created_job(child)
                log.info("Download episode job enqueued profile_id=%s source_id=%s child_job_id=%s item_uid=%s title=%s", profile_id, source.id, child.id, item_uid, title)
                source_enqueued += 1
                total_enqueued += 1
            log.info(
                "Episode check source finished profile_id=%s source_id=%s source_type=%s seen=%s enqueued=%s",
                profile_id,
                source.id,
                source.source_type,
                source_seen,
                source_enqueued,
            )
        log.info("Episode check profile finished job_id=%s profile_id=%s", job.id, profile_id)
    log.info(
        "Episode check finished job_id=%s profiles=%s sources=%s episodes_seen=%s enqueued_download_jobs=%s",
        job.id,
        len(profile_ids),
        total_sources,
        total_seen,
        total_enqueued,
    )


def update_downloads(job: Job) -> None:
    log.info("update_downloads routed to episode checker job_id=%s", job.id)
    check_for_episodes(job)


def download_episode(job: Job) -> None:
    """Serial downloader worker placeholder.

    The queue is intentionally single-consumer/prefetch=1 so episode downloads happen one at a time.
    After download code writes a Download row, enqueue transcript generation with that download_id.
    """
    log.info("Download worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_url = str(payload.get("media_url") or payload.get("item_url") or "").strip()
    if download_url:
        ydl_opts = _yt_dlp_base_options(skip_download=True)
        log.info("yt-dlp download preflight starting job_id=%s url=%s options=%s", job.id, download_url, {k: v for k, v in ydl_opts.items() if k not in {"logger", "progress_hooks"}})
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(download_url, download=False) or {}
        if isinstance(info, dict):
            _log_youtube_response("yt-dlp download preflight response", info)
        log.info("yt-dlp actual download integration pending job_id=%s url=%s", job.id, download_url)
    download_id = payload.get("download_id")
    if not download_id:
        log.info("Download worker has no download_id yet; actual downloader integration pending job_id=%s", job.id)
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_transcript",
        payload={"download_id": download_id},
        idempotency_key=f"generate_transcript:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Download worker queued transcript job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download_id, child.id)


def download_single(job: Job) -> None:
    log.info("download_single routed to downloader job_id=%s", job.id)
    download_episode(job)


def generate_transcript(job: Job) -> None:
    """Parallel transcript worker placeholder; enqueue summary when transcript work is done."""
    log.info("Transcript worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
    if not download_id:
        log.warning("Transcript worker skipped job with no download_id job_id=%s", job.id)
        return
    if not Download.objects.filter(pk=download_id, profile_id=job.profile_id).exists():
        log.warning("Transcript worker skipped missing download job_id=%s download_id=%s profile_id=%s", job.id, download_id, job.profile_id)
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_summary",
        payload={"download_id": download_id},
        idempotency_key=f"generate_summary:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Transcript worker queued summary job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download_id, child.id)


def generate_summary(job: Job) -> None:
    log.info("Summary worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    log.info("Summary worker finished placeholder job_id=%s", job.id)
    return None


def summarize_missing(job: Job) -> None:
    downloads = list(Download.objects.filter(profile_id=job.profile_id, summary__isnull=True).order_by("-last_seen_at")[:100])
    log.info("Summarize-missing fanout started job_id=%s profile_id=%s candidates=%s", job.id, job.profile_id, len(downloads))
    enqueued = 0
    for download in downloads:
        child = create_job(
            profile_id=job.profile_id,
            job_type="generate_summary",
            payload={"download_id": download.id},
            idempotency_key=f"generate_summary:{job.profile_id}:{download.id}",
        )
        _publish_created_job(child)
        enqueued += 1
    log.info("Summarize-missing fanout finished job_id=%s profile_id=%s enqueued_summary_jobs=%s", job.id, job.profile_id, enqueued)


def sync_media(job: Job) -> None:
    log.info("Sync worker placeholder started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    log.info("Sync worker placeholder finished job_id=%s", job.id)
    return None


HANDLERS = {
    "check_for_episodes": check_for_episodes,
    "update_downloads": update_downloads,
    "download_episode": download_episode,
    "download_single": download_single,
    "generate_transcript": generate_transcript,
    "generate_summary": generate_summary,
    "summarize_missing": summarize_missing,
    "sync_media": sync_media,
}
