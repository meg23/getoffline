import hashlib
from typing import Iterable

import feedparser
from yt_dlp import YoutubeDL

from app.queue import publish_job
from logger import get_logger
from models.jobs import create_job
from models.models import Download, Job, SourceConfig


log = get_logger("workers.handlers")


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
    entries = list(getattr(feed, "entries", []) or [])[: _source_limit(source)]
    log.info("Podcast feed parsed source_id=%s source_name=%s entries=%s", source.id, source.name, len(entries))
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
        yield {
            "item_uid": item_uid[:255],
            "item_url": item_url,
            "media_url": enclosure_url or item_url,
            "title": title,
            "published": published,
        }


def _youtube_candidates(source: SourceConfig) -> Iterable[dict]:
    log.info("Checking YouTube source source_id=%s source_name=%s url=%s", source.id, source.name, source.url)
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
        "playlistend": _source_limit(source),
    }
    with YoutubeDL(ydl_opts) as ydl:
        payload = ydl.extract_info(source.url, download=False) or {}
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
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
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
