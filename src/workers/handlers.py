from models.models import Job


def update_downloads(job: Job) -> None:
    # Download work is intentionally isolated to the downloads queue, which should
    # be run with concurrency 1 to avoid hitting YouTube too quickly.
    return None


def download_single(job: Job) -> None:
    return None


def sync_media(job: Job) -> None:
    return None


def summarize_missing(job: Job) -> None:
    return None


HANDLERS = {
    "update_downloads": update_downloads,
    "download_single": download_single,
    "sync_media": sync_media,
    "summarize_missing": summarize_missing,
}
