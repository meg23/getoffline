import os
from pathlib import Path

import feedparser
from yt_dlp import YoutubeDL

from ad_scrubber import generate_whisper_subtitles, scrub_audio_file
from logger import log
from utils import create_audio_visualizer_video, ensure_dir, sanitize


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
                }

                log.info(f"🎙️ Downloading podcast: {name} – {ep_title}")
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([mp3_url])

                final_audio = Path(folder) / f"{ep_title}.{defaults['audio_format']}"
                playback_audio = final_audio

                if scrubber_cfg.get("enabled", False) and entry_scrub_enabled and final_audio.exists():
                    try:
                        scrubbed_output = scrub_audio_file(final_audio, scrubber_cfg)
                        if scrubbed_output:
                            playback_audio = scrubbed_output
                    except Exception as scrub_exc:
                        log.warning(f"Ad scrub failed for {final_audio}: {scrub_exc}")
                elif final_audio.exists() and not entry_scrub_enabled:
                    log.info("⏭️ Ad scrub disabled for podcast %s", name)

                if entry_subtitles_enabled and playback_audio.exists():
                    try:
                        subtitle_settings = dict(scrubber_cfg)
                        if subtitle_offset_seconds is not None:
                            subtitle_settings["subtitle_time_offset_seconds"] = float(subtitle_offset_seconds)
                        subtitle_path = generate_whisper_subtitles(playback_audio, subtitle_settings)
                        downloaded_items.append(f"Subtitles: Podcast – {subtitle_path.name}")
                        log.info("✅ Generated podcast subtitles: %s", subtitle_path.name)

                        if entry_visualize_enabled:
                            try:
                                visualizer_path = create_audio_visualizer_video(playback_audio, subtitle_path)
                                log.info("🎬 Generated podcast visualizer: %s", visualizer_path.name)
                                downloaded_items.append(f"Visualizer: Podcast – {visualizer_path.name}")
                            except Exception as viz_exc:
                                log.warning("Visualizer generation failed for %s: %s", playback_audio, viz_exc)
                    except Exception as subtitle_exc:
                        log.warning("Subtitle generation failed for %s: %s", playback_audio, subtitle_exc)
                elif entry_visualize_enabled:
                    log.info("⏭️ Visualizer skipped for podcast %s because subtitles are disabled", name)

                with open(archive, "a", encoding="utf-8") as f:
                    f.write(mp3_url + "\n")

                downloaded_items.append(f"Podcast: {name} – {ep_title}")

        except Exception as e:
            log.error(f"❌ Failed to process podcast {entry}: {e}")
