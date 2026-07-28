"""Profile configuration service used by web and API layers."""

from __future__ import annotations

from pathlib import Path

from models.models import AppConfigValue, ProfileConfigValue

PROFILE_DEFAULTS = {
    "output_root": "./downloads/default",
    "processing_workers": "2",
    "auto_update_minutes": "20",
    "auto_delete_content_days": "0",
    "manual_upload_delete_explicit_content": "0",
    "audio_format": "mp3",
    "video_format": "mp4",
    "video_codec": "h264",
    "ffmpeg_path": "ffmpeg",
    "audio_quality": "0",
    "ffmpeg_audio_filter": "loudnorm=I=-14:TP=-1.5:LRA=11",
    "ytdlp_video_max_height": "720",
    "max_downloads": "3",
    "js_runtime_path": "qjs",
}


def profile_settings(profile_id: str) -> dict[str, str]:
    values = dict(PROFILE_DEFAULTS)
    values["output_root"] = f"./downloads/{profile_id}"
    values.update(
        {row.key: row.value for row in AppConfigValue.objects.order_by("key")}
    )
    values.update(
        {
            row.key: row.value
            for row in ProfileConfigValue.objects.filter(profile_id=profile_id)
        }
    )
    return values


def profile_output_root(profile_id: str) -> Path:
    value = (
        ProfileConfigValue.objects.filter(profile_id=profile_id, key="output_root")
        .values_list("value", flat=True)
        .first()
        or AppConfigValue.objects.filter(key="output_root")
        .values_list("value", flat=True)
        .first()
        or PROFILE_DEFAULTS["output_root"]
    )
    return Path(str(value)).expanduser().resolve()
