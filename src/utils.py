import os
import re
import subprocess
from pathlib import Path

def sanitize(name):
    sanitized = re.sub(r"[^\w.-]", "_", name)
    sanitized = re.sub(r"\.{2,}", ".", sanitized)
    sanitized = sanitized.strip(". ")
    return sanitized or "item"

def ensure_dir(path):
    Path(path).expanduser().mkdir(parents=True, exist_ok=True)


def _escape_subtitle_path_for_ffmpeg(path: Path) -> str:
    escaped = str(path)
    escaped = escaped.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    return escaped


def create_audio_visualizer_video(audio_path: Path, subtitle_path: Path) -> Path:
    output_path = audio_path.with_name(f"{audio_path.stem}_visualizer.mp4")
    subtitle_filter_path = _escape_subtitle_path_for_ffmpeg(subtitle_path)

    filter_complex = (
        "color=c=black:s=1280x720[bg];"
        "[0:a]showwaves=s=1100x220:mode=line:colors=white:scale=lin,"
        "format=rgba,colorchannelmixer=aa=0.9[wave];"
        "[bg][wave]overlay=(W-w)/2:240[tmp];"
        f"[tmp]subtitles=filename='{subtitle_filter_path}':"
        "force_style='Fontsize=32,Outline=2,Shadow=1,MarginV=55,Alignment=2'[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(output_path),
    ]

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_path


def normalize_media_filename(path: Path) -> Path:
    path = Path(path)
    normalized_stem = re.sub(r"\.{2,}", ".", path.stem).rstrip(". ")
    if not normalized_stem:
        normalized_stem = "item"

    normalized_path = path.with_name(f"{normalized_stem}{path.suffix}")
    if normalized_path == path:
        return path

    counter = 1
    candidate = normalized_path
    while candidate.exists():
        candidate = path.with_name(f"{normalized_stem}_{counter}{path.suffix}")
        counter += 1

    path.rename(candidate)
    return candidate
