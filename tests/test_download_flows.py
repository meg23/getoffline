import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import podcasts  # noqa: E402
import youtube  # noqa: E402


class FakeYoutubeDL:
    instances = []

    def __init__(self, opts):
        self.opts = opts
        self.urls = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def download(self, urls):
        self.urls.extend(urls)

        outtmpl = self.opts.get("outtmpl")
        output_path = None
        if outtmpl:
            output_path = (
                outtmpl.replace("%(upload_date)s", "20260101")
                .replace("%(title)s", "Test Video")
                .replace("%(ext)s", "mp3")
            )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("dummy audio")

        info_dict = {
            "id": "video-1",
            "title": "Test Video",
            "webpage_url": "https://youtube.com/watch?v=video-1",
            "_filename": output_path,
        }
        for hook in self.opts.get("progress_hooks", []):
            hook(
                {
                    "status": "finished",
                    "info_dict": info_dict,
                    "filename": output_path,
                    "total_bytes": 1024,
                }
            )


def _fake_subtitle_generator(media_path, subtitle_settings):
    _ = subtitle_settings
    media_path = Path(media_path)
    srt_path = media_path.with_suffix(".srt")
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
    return srt_path


def _build_sample_config_from_repo_config(output_root):
    with open("config.yaml", encoding="utf-8") as f:
        source = yaml.safe_load(f)

    youtube_entry = next(
        item
        for item in source.get("youtube", [])
        if item.get("type", "audio").lower() == "audio" and item.get("subtitles")
    )
    podcast_entry = next(item for item in source.get("podcasts", []) if item.get("subtitles"))

    return {
        "defaults": {
            "cookie_path": os.path.join(output_root, "cookies.txt"),
            "playlist_end": 1,
            "max_downloads": 1,
            "output_root": output_root,
            "audio_format": "mp3",
            "audio_quality": 0,
            "processing_workers": 1,
        },
        "youtube": [{
            "name": youtube_entry["name"],
            "url": youtube_entry["url"],
            "type": "audio",
            "subtitles": True,
        }],
        "podcasts": [{
            "name": podcast_entry["name"],
            "url": podcast_entry["url"],
            "subtitles": True,
        }],
    }


class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_sample_config_single_youtube_and_podcast_with_subtitles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _build_sample_config_from_repo_config(tmpdir)

            mp3_url = "https://cdn.example.com/episode-1.mp3"
            fake_feed = SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        title="Episode 1",
                        enclosures=[SimpleNamespace(href=mp3_url)],
                    )
                ]
            )

            downloaded_items = []
            with patch("youtube.YoutubeDL", FakeYoutubeDL), patch("podcasts.YoutubeDL", FakeYoutubeDL), patch(
                "podcasts.feedparser.parse", return_value=fake_feed
            ), patch("subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator):
                youtube.download_youtube_items(config, downloaded_items)
                podcasts.download_podcasts(config, downloaded_items)

            self.assertEqual(len(FakeYoutubeDL.instances), 2)
            self.assertEqual(FakeYoutubeDL.instances[0].urls, [config["youtube"][0]["url"]])
            self.assertEqual(FakeYoutubeDL.instances[1].urls, [mp3_url])

            youtube_folder = Path(tmpdir) / youtube.sanitize(config["youtube"][0]["name"])
            podcast_folder = Path(tmpdir) / podcasts.sanitize(config["podcasts"][0]["name"])

            youtube_mp3 = next(youtube_folder.glob("*.mp3"), None)
            podcast_mp3 = next(podcast_folder.glob("*.mp3"), None)
            self.assertIsNotNone(youtube_mp3)
            self.assertIsNotNone(podcast_mp3)

            self.assertTrue(youtube_mp3.with_suffix(".srt").exists())
            self.assertTrue(podcast_mp3.with_suffix(".srt").exists())
            self.assertFalse(any(youtube_folder.glob("*.visualizer.mp4")))
            self.assertFalse(any(podcast_folder.glob("*.visualizer.mp4")))

            self.assertTrue(any(item.startswith("YouTube: ") for item in downloaded_items))
            self.assertTrue(any(item.startswith("Podcast: ") for item in downloaded_items))
            self.assertTrue(any(item.startswith("Subtitles: Podcast") for item in downloaded_items))



    def test_downloads_are_tracked_in_sqlite_database(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmpdir:
            config = _build_sample_config_from_repo_config(tmpdir)
            config["defaults"]["database_path"] = os.path.join(tmpdir, "downloads.sqlite3")

            mp3_url = "https://cdn.example.com/episode-1.mp3"
            fake_feed = SimpleNamespace(
                entries=[SimpleNamespace(title="Episode 1", enclosures=[SimpleNamespace(href=mp3_url)])]
            )

            downloaded_items = []
            with patch("youtube.YoutubeDL", FakeYoutubeDL), patch("podcasts.YoutubeDL", FakeYoutubeDL), patch(
                "podcasts.feedparser.parse", return_value=fake_feed
            ), patch("subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator):
                youtube.download_youtube_items(config, downloaded_items)
                podcasts.download_podcasts(config, downloaded_items)

            with sqlite3.connect(config["defaults"]["database_path"]) as conn:
                rows = conn.execute(
                    "SELECT source_type, source_name, title, file_path, raw_metadata_json FROM downloads ORDER BY source_type"
                ).fetchall()

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "podcast")
            self.assertEqual(rows[1][0], "youtube")
            self.assertTrue(rows[0][3])
            self.assertIn("title", rows[0][4])
            self.assertIn("title", rows[1][4])

class SubtitleDefaultsAndYoutubeCaptionTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_youtube_download_configures_english_caption_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                        },
                "youtube": [{
                    "name": "Sample",
                    "url": "https://youtube.com/watch?v=video-1",
                    "type": "audio",
                        }],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDL), patch(
                "subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator
            ):
                youtube.download_youtube_items(config, [])

            opts = FakeYoutubeDL.instances[0].opts
            self.assertTrue(opts["writesubtitles"])
            self.assertTrue(opts["writeautomaticsub"])
            self.assertEqual(opts["subtitlesformat"], "srt/best")
            self.assertIn("en", opts["subtitleslangs"])


    def test_youtube_summary_ignores_subtitle_sidecar_finished_events(self):
        class FakeYoutubeDLWithSubtitleEvents(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                info_main = {
                    "id": "video-1",
                    "title": "Main Title",
                    "webpage_url": "https://youtube.com/watch?v=video-1",
                }
                for hook in self.opts.get("progress_hooks", []):
                    hook(
                        {
                            "status": "finished",
                            "info_dict": info_main,
                            "filename": "/tmp/Main Title.mp4",
                            "total_bytes": 4096,
                        }
                    )
                    hook(
                        {
                            "status": "finished",
                            "info_dict": {"_filename": "/tmp/Main Title.en.vtt"},
                            "filename": "/tmp/Main Title.en.vtt",
                            "total_bytes": 512,
                        }
                    )
                    hook(
                        {
                            "status": "finished",
                            "info_dict": {"_filename": "/tmp/Main Title.en.srt"},
                            "filename": "/tmp/Main Title.en.srt",
                            "total_bytes": 512,
                        }
                    )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                        },
                "youtube": [
                    {
                        "name": "WarFronts",
                        "url": "https://youtube.com/watch?v=video-1",
                        "type": "video",
                                    "subtitles": True,
                    }
                ],
            }

            downloaded_items = []
            with patch("youtube.YoutubeDL", FakeYoutubeDLWithSubtitleEvents):
                youtube.download_youtube_items(config, downloaded_items)

            youtube_items = [item for item in downloaded_items if item.startswith("YouTube: ")]
            self.assertEqual(youtube_items, ["YouTube: WarFronts – Main Title"])

    def test_podcast_subtitles_default_to_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                        },
                "podcasts": [{
                    "name": "PodcastA",
                    "url": "https://example.com/rss",
                        }],
            }
            mp3_url = "https://cdn.example.com/episode-1.mp3"
            fake_feed = SimpleNamespace(entries=[SimpleNamespace(title="Episode 1", enclosures=[SimpleNamespace(href=mp3_url)])])

            with patch("podcasts.YoutubeDL", FakeYoutubeDL), patch(
                "podcasts.feedparser.parse", return_value=fake_feed
            ), patch("subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator):
                downloaded_items = []
                podcasts.download_podcasts(config, downloaded_items)

            self.assertTrue(any(item.startswith("Subtitles: Podcast") for item in downloaded_items))


class SubtitleSidecarCleanupTests(unittest.TestCase):
    def test_reused_english_sidecars_are_consolidated_to_single_srt(self):
        import subtitles

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "20260311-Cuba_is_Next.mp3"
            media.write_text("fake", encoding="utf-8")

            en_orig = media.with_name(f"{media.stem}.en-orig.srt")
            en_srt = media.with_name(f"{media.stem}.en.srt")
            en_vtt = media.with_name(f"{media.stem}.en.vtt")
            en_orig.write_text("orig", encoding="utf-8")
            en_srt.write_text("en srt", encoding="utf-8")
            en_vtt.write_text("vtt", encoding="utf-8")

            subtitle_path = subtitles.create_subtitles(
                media_file=media,
                subtitle_offset_seconds=None,
                entry_subtitles_enabled=True,
                logger=youtube.log,
                context_name="test",
                context_label="YouTube",
            )

            self.assertIsNotNone(subtitle_path)
            self.assertEqual(subtitle_path, media.with_suffix(".srt"))
            self.assertTrue(media.with_suffix(".srt").exists())
            self.assertFalse(en_orig.exists())
            self.assertFalse(en_srt.exists())
            self.assertFalse(en_vtt.exists())

    def test_folder_cleanup_removes_existing_en_sidecars_without_new_download(self):
        import subtitles

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "20260310-Pakistan_and_Afghanistan_are_Still_At_War.mp3"
            media.write_text("fake", encoding="utf-8")

            srt_main = media.with_suffix('.srt')
            srt_main.write_text('main', encoding='utf-8')
            en_orig = media.with_name(f"{media.stem}.en-orig.srt")
            en_srt = media.with_name(f"{media.stem}.en.srt")
            en_orig.write_text("orig", encoding="utf-8")
            en_srt.write_text("en", encoding="utf-8")

            subtitles.cleanup_subtitle_sidecars_for_folder(Path(tmpdir))

            self.assertTrue(srt_main.exists())
            self.assertFalse(en_orig.exists())
            self.assertFalse(en_srt.exists())


class SubtitleFailureCachingTests(unittest.TestCase):
    def test_known_empty_audio_transcription_failure_is_cached(self):
        import subtitles

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "broken.mp3"
            media.write_text("fake", encoding="utf-8")

            settings = {"subtitle_model": "base"}
            calls = {"count": 0}

            def _boom(*args, **kwargs):
                _ = args, kwargs
                calls["count"] += 1
                raise RuntimeError(
                    "Transcription failed for broken.mp3 (base): cannot reshape tensor of 0 elements into shape [1, 0, 8, -1] because the unspecified dimension size -1 can be any value and is ambiguous"
                )

            with patch("subtitles.transcribe_with_whisper", side_effect=_boom):
                first = subtitles.generate_whisper_subtitles(media, settings)
                second = subtitles.generate_whisper_subtitles(media, settings)

            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(calls["count"], 1)
            marker = media.with_suffix(".srt.failed")
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
