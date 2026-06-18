def queue_name(job_type: str) -> str:
    if job_type in {"update_downloads", "download_single"}:
        return "getoffline.downloads"
    return f"getoffline.{job_type}"
