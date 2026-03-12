import os
from pathlib import Path

import feedparser
from yt_dlp import YoutubeDL

from logger import get_logger
from scrubbing import scrub_media_file
from subtitles import create_subtitles_and_optional_visualizer
from utils import ensure_dir, sanitize


class _YoutubeDlQuietLogger:
    def debug(self, msg):
        _ = msg

    def warning(self, msg):
        _ = msg

    def error(self, msg):
        if msg:
            log.error("%s", msg)

log = get_logger("podcast")


def download_podcasts(config, downloaded_items):
    defaults = config["defaults"]
    scrubber_cfg = defaults.get("ad_scrubber", {})

    for entry in config.get("podcasts", []):
        try:
            name = sanitize(entry["name"])
            url = entry["url"]
            entry_scrub_enabled = entry.get("scrub", True)
            entry_subtitles_enabled = entry.get("subtitles", False)
            entry_visualize_enabled = entry.get("visualize", False)
            subtitle_offset_seconds = entry.get("subtitle_offset_seconds")
            folder = os.path.join(defaults["output_root"], name)
            archive = os.path.join(folder, f"{name}_downloaded.txt")
            ensure_dir(folder)

            downloaded = set()
            if os.path.exists(archive):
                with open(archive, encoding="utf-8") as f:
                    downloaded = set(line.strip() for line in f)

            feed = feedparser.parse(url)
            entries = feed.entries[: defaults["max_downloads"]]

            for ep in entries:
                if not ep.enclosures:
                    continue

                mp3_url = ep.enclosures[0].href
                if mp3_url in downloaded:
                    continue

                ep_title = sanitize(ep.title)
                out_path = f"{folder}/{ep_title}.%(ext)s"

                ydl_opts = {
                    "extract_audio": True,
                    "audio_format": defaults["audio_format"],
                    "audio_quality": str(defaults["audio_quality"]),
                    "restrictfilenames": True,
                    "outtmpl_na_placeholder": "NA",
                    "outtmpl": out_path,
                    "quiet": True,
                    "no_warnings": True,
                    "noprogress": True,
                    "logger": _YoutubeDlQuietLogger(),
                }

                log.info(f"Downloading podcast: {name} – {ep_title}")
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([mp3_url])

                final_audio = Path(folder) / f"{ep_title}.{defaults['audio_format']}"
                playback_audio = final_audio
                skip_subtitles_after_scrub_failure = False

                if final_audio.exists():
                    playback_audio, skip_subtitles_after_scrub_failure = scrub_media_file(
                        media_file=final_audio,
                        scrubber_cfg=scrubber_cfg,
                        scrubber_enabled=scrubber_cfg.get("enabled", False),
                        entry_scrub_enabled=entry_scrub_enabled,
                        logger=log,
                        context_name=name,
                        context_label="podcast",
                    )

                subtitle_path = create_subtitles_and_optional_visualizer(
                    media_file=playback_audio,
                    scrubber_cfg=scrubber_cfg,
                    subtitle_offset_seconds=subtitle_offset_seconds,
                    entry_subtitles_enabled=entry_subtitles_enabled,
                    entry_visualize_enabled=entry_visualize_enabled,
                    logger=log,
                    context_name=f"podcast {name}",
                    context_label="podcast",
                    skip_subtitles_after_scrub_failure=skip_subtitles_after_scrub_failure,
                )
                if subtitle_path:
                    downloaded_items.append(f"Subtitles: Podcast – {subtitle_path.name}")

                with open(archive, "a", encoding="utf-8") as f:
                    f.write(mp3_url + "\n")

                downloaded_items.append(f"Podcast: {name} – {ep_title}")

        except Exception as e:
            log.error(f"Failed to process podcast {entry}: {e}")
