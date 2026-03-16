import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import podcasts  # noqa: E402
import youtube  # noqa: E402
from database import has_episode_title_for_source, is_downloaded, upsert_download, init_database  # noqa: E402


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


def _build_sample_config(output_root):
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
            "name": "Test YouTube Source",
            "url": "https://youtube.com/playlist?list=test-playlist",
            "type": "audio",
            "subtitles": True,
        }],
        "podcasts": [{
            "name": "Test Podcast Source",
            "url": "https://feeds.example.com/test-podcast.xml",
            "subtitles": True,
        }],
    }


class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_sample_config_single_youtube_and_podcast_with_subtitles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _build_sample_config(tmpdir)

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

            youtube_folder = Path(tmpdir) / youtube.sanitize_channel_name(config["youtube"][0]["name"])
            podcast_folder = Path(tmpdir) / podcasts.sanitize_channel_name(config["podcasts"][0]["name"])

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
            config = _build_sample_config(tmpdir)
            config["defaults"]["database_path"] = os.path.join(tmpdir, "downloads.sqlite3")

            mp3_url = "https://cdn.example.com/episode-1.mp3"
            podcast_title = "Episode 1: A Normal Podcast Title"
            fake_feed = SimpleNamespace(
                entries=[SimpleNamespace(title=podcast_title, enclosures=[SimpleNamespace(href=mp3_url)])]
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
            self.assertEqual(rows[0][2], podcast_title)
            self.assertTrue(rows[0][3])
            self.assertIn(podcasts.sanitize(podcast_title), rows[0][3])
            self.assertIn("title", rows[0][4])
            self.assertIn("title", rows[1][4])

class SubtitleDefaultsAndYoutubeWhisperTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_youtube_download_does_not_request_youtube_caption_download(self):
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
            self.assertNotIn("writesubtitles", opts)
            self.assertNotIn("writeautomaticsub", opts)
            self.assertNotIn("subtitlesformat", opts)
            self.assertNotIn("subtitleslangs", opts)


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



    def test_youtube_download_uses_full_entry_extraction(self):
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
                        "name": "FullExtract",
                        "url": "https://youtube.com/watch?v=video-1",
                        "type": "video",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDL):
                youtube.download_youtube_items(config, [])

            opts = FakeYoutubeDL.instances[0].opts
            self.assertTrue("extract_flat" not in opts or not opts.get("extract_flat"))

    def test_youtube_no_download_warning_uses_unique_filter_counts(self):
        class FakeYoutubeDLDuplicateFilterCalls(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                flt = self.opts.get("match_filter")
                entries = [
                    {"id": "a1", "title": "Alpha", "webpage_url": "https://youtube.com/watch?v=a1"},
                    {"id": "a1", "title": "Alpha", "webpage_url": "https://youtube.com/watch?v=a1"},
                    {"id": "b2", "title": "Beta", "webpage_url": "https://youtube.com/watch?v=b2"},
                    {"id": "b2", "title": "Beta", "webpage_url": "https://youtube.com/watch?v=b2"},
                ]
                for entry in entries:
                    if flt:
                        flt(entry)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 3,
                    "max_downloads": 3,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                },
                "youtube": [
                    {
                        "name": "DupCounts",
                        "url": "https://youtube.com/playlist?list=dup",
                        "type": "video",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLDuplicateFilterCalls), self.assertLogs("getoffline", level="WARNING") as logs:
                youtube.download_youtube_items(config, [])

            combined = "\n".join(logs.output)
            self.assertIn("No new YouTube media downloaded for DupCounts (playlist_items_seen=2, allowed_after_filters=2, skipped_by_filters=0", combined)
            self.assertIn("ytdlp_items_announced=0", combined)
            self.assertIn("YouTube accepted playlist entries for DupCounts but did not emit item download events.", combined)


    def test_youtube_no_download_warning_includes_ytdlp_announced_item_count(self):
        class FakeYoutubeDLAnnouncedButNoProgress(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                flt = self.opts.get("match_filter")
                if flt:
                    flt({"id": "x1", "title": "Alpha", "webpage_url": "https://youtube.com/watch?v=x1"})
                    flt({"id": "x2", "title": "Beta", "webpage_url": "https://youtube.com/watch?v=x2"})

                logger = self.opts.get("logger")
                if logger:
                    logger.debug("[download] Downloading item 1 of 2")
                    logger.debug("[download] Downloading item 2 of 2")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 3,
                    "max_downloads": 3,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                },
                "youtube": [
                    {
                        "name": "AnnouncedNoProgress",
                        "url": "https://youtube.com/playlist?list=announced",
                        "type": "video",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLAnnouncedButNoProgress), self.assertLogs("getoffline", level="WARNING") as logs:
                youtube.download_youtube_items(config, [])

            combined = "\n".join(logs.output)
            self.assertIn("ytdlp_items_announced=2", combined)
            self.assertIn("yt-dlp announced 2 playlist item(s) for AnnouncedNoProgress but produced no file events", combined)

    def test_youtube_logs_item_failures_when_progress_hook_reports_error(self):
        class FakeYoutubeDLErrorStatus(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                info_dict = {
                    "id": "video-error",
                    "title": "Broken Item",
                    "webpage_url": "https://youtube.com/watch?v=video-error",
                }
                for hook in self.opts.get("progress_hooks", []):
                    hook(
                        {
                            "status": "error",
                            "info_dict": info_dict,
                            "error": "HTTP Error 403: Forbidden",
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
                        "name": "ErrorChannel",
                        "url": "https://youtube.com/watch?v=video-error",
                        "type": "video",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLErrorStatus), self.assertLogs("getoffline", level="WARNING") as logs:
                youtube.download_youtube_items(config, [])

            combined = "\n".join(logs.output)
            self.assertIn("YouTube item failed for ErrorChannel: HTTP Error 403: Forbidden", combined)
            self.assertIn("YouTube download errors for ErrorChannel: HTTP Error 403: Forbidden=1", combined)

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

    def test_youtube_database_file_path_tracks_normalized_filename(self):
        import sqlite3

        class FakeYoutubeDLWithOddDots(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)

                outtmpl = self.opts.get("outtmpl")
                output_path = (
                    outtmpl.replace("%(upload_date)s", "20260312")
                    .replace("%(title)s", "They_re..FINALLY_Doing_It_-_BIG_Xbox_News")
                    .replace("%(ext)s", "mp3")
                )
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write("dummy audio")

                info_dict = {
                    "id": "video-odd-dots",
                    "title": "They_re..FINALLY_Doing_It_-_BIG_Xbox_News",
                    "webpage_url": "https://youtube.com/watch?v=video-odd-dots",
                    "_filename": output_path,
                }
                for hook in self.opts.get("progress_hooks", []):
                    hook(
                        {
                            "status": "finished",
                            "info_dict": info_dict,
                            "filename": output_path,
                            "total_bytes": 2048,
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
                    "database_path": os.path.join(tmpdir, "downloads.sqlite3"),
                },
                "youtube": [
                    {
                        "name": "Sample",
                        "url": "https://youtube.com/watch?v=video-odd-dots",
                        "type": "audio",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLWithOddDots), patch(
                "subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator
            ):
                youtube.download_youtube_items(config, [])

            with sqlite3.connect(config["defaults"]["database_path"]) as conn:
                stored_path = conn.execute(
                    "SELECT file_path FROM downloads WHERE source_type='youtube' LIMIT 1"
                ).fetchone()[0]

            self.assertTrue(stored_path.endswith("20260312-They_re.FINALLY_Doing_It_-_BIG_Xbox_News.mp3"))
            self.assertTrue(Path(stored_path).exists())

    def test_youtube_database_prefers_postprocessed_audio_path_and_size(self):
        import sqlite3

        class FakeYoutubeDLWithSeparatePostprocess(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)

                outtmpl = self.opts.get("outtmpl")
                webm_path = (
                    outtmpl.replace("%(upload_date)s", "20260312")
                    .replace("%(title)s", "They_re_FINALLY_Doing_It_-_BIG_Xbox_News....")
                    .replace("%(ext)s", "webm")
                )
                mp3_path = webm_path.replace("....webm", ".mp3")

                os.makedirs(os.path.dirname(webm_path), exist_ok=True)
                with open(mp3_path, "w", encoding="utf-8") as f:
                    f.write("real-audio")

                info_dict = {
                    "id": "video-postprocessed",
                    "title": "They_re FINALLY Doing It - BIG Xbox News",
                    "webpage_url": "https://youtube.com/watch?v=video-postprocessed",
                    "_filename": webm_path,
                }

                for hook in self.opts.get("progress_hooks", []):
                    hook(
                        {
                            "status": "finished",
                            "info_dict": info_dict,
                            "filename": webm_path,
                            "total_bytes": 4096,
                        }
                    )

                for pp_hook in self.opts.get("postprocessor_hooks", []):
                    pp_hook(
                        {
                            "status": "finished",
                            "postprocessor": "FFmpegExtractAudio",
                            "info_dict": info_dict,
                            "filepath": webm_path,
                        }
                    )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                    "database_path": db_path,
                },
                "youtube": [
                    {
                        "name": "MrMattyPlays",
                        "url": "https://youtube.com/watch?v=video-postprocessed",
                        "type": "audio",
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLWithSeparatePostprocess), patch(
                "subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator
            ):
                youtube.download_youtube_items(config, [])

            with sqlite3.connect(db_path) as conn:
                stored_path, stored_ext, stored_size = conn.execute(
                    "SELECT file_path, file_ext, file_size_bytes FROM downloads WHERE source_type='youtube' LIMIT 1"
                ).fetchone()

            self.assertTrue(stored_path.endswith(".mp3"))
            self.assertNotIn(".webm", stored_path)
            self.assertTrue(Path(stored_path).exists())
            self.assertEqual(stored_ext, "mp3")
            self.assertEqual(stored_size, len("real-audio"))

    def test_video_download_tracks_merged_file_in_database(self):
        import sqlite3

        class FakeYoutubeDLVideoMerge(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                outtmpl = self.opts.get("outtmpl")
                base = (
                    outtmpl.replace("%(upload_date)s", "20260101")
                    .replace("%(title)s", "Merged Video")
                    .replace("%(ext)s", "mp4")
                )
                video_part = base.replace(".mp4", ".f398.mp4")
                audio_part = base.replace(".mp4", ".f140.m4a")
                merged = base
                os.makedirs(os.path.dirname(base), exist_ok=True)
                with open(video_part, "w", encoding="utf-8") as f:
                    f.write("video")
                with open(audio_part, "w", encoding="utf-8") as f:
                    f.write("audio")

                info_dict = {
                    "id": "video-merge-1",
                    "title": "Merged Video",
                    "webpage_url": "https://youtube.com/watch?v=video-merge-1",
                }
                for hook in self.opts.get("progress_hooks", []):
                    hook({"status": "finished", "info_dict": info_dict, "filename": video_part, "total_bytes": 4096})
                    hook({"status": "finished", "info_dict": info_dict, "filename": audio_part, "total_bytes": 2048})

                with open(merged, "w", encoding="utf-8") as f:
                    f.write("merged")

                for hook in self.opts.get("postprocessor_hooks", []):
                    hook(
                        {
                            "status": "finished",
                            "postprocessor": "Merger",
                            "info_dict": info_dict,
                            "filepath": merged,
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
                    "database_path": os.path.join(tmpdir, "downloads.sqlite3"),
                },
                "youtube": [{
                    "name": "MergeChannel",
                    "url": "https://youtube.com/watch?v=video-merge-1",
                    "type": "video",
                }],
                "podcasts": [],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLVideoMerge):
                youtube.download_youtube_items(config, [])

            with sqlite3.connect(config["defaults"]["database_path"]) as conn:
                row = conn.execute(
                    "SELECT file_path FROM downloads WHERE source_type = 'youtube' AND source_name = ? LIMIT 1",
                    ("MergeChannel",),
                ).fetchone()

            self.assertIsNotNone(row)
            self.assertTrue(row[0].endswith("20260101-Merged Video.mp4"))
            self.assertNotIn(".f398.mp4", row[0])
            self.assertNotIn(".f140.m4a", row[0])


    def test_video_download_generates_sidecar_subtitles_without_burning_into_video(self):
        class FakeYoutubeDLVideoOnly(FakeYoutubeDL):
            def download(self, urls):
                self.urls.extend(urls)
                outtmpl = self.opts.get("outtmpl")
                output_path = (
                    outtmpl.replace("%(upload_date)s", "20260101")
                    .replace("%(title)s", "Visible Captions Off")
                    .replace("%(ext)s", "mp4")
                )
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write("video")

                info_dict = {
                    "id": "video-with-subs",
                    "title": "Visible Captions Off",
                    "webpage_url": "https://youtube.com/watch?v=video-with-subs",
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
                    "name": "VideoSubs",
                    "url": "https://youtube.com/watch?v=video-with-subs",
                    "type": "video",
                    "subtitles": True,
                }],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLVideoOnly), patch(
                "subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator
            ):
                youtube.download_youtube_items(config, [])

            folder = Path(tmpdir) / youtube.sanitize_channel_name("VideoSubs")
            mp4_file = next(folder.glob("*.mp4"), None)
            self.assertIsNotNone(mp4_file)
            self.assertTrue(mp4_file.with_suffix(".srt").exists())



class SubtitleSidecarCleanupTests(unittest.TestCase):
    def test_whisper_subtitles_are_canonical_and_cleanup_youtube_sidecars(self):
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

            with patch("subtitles.generate_whisper_subtitles", side_effect=_fake_subtitle_generator):
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


class YoutubeSourceResolverTests(unittest.TestCase):
    def test_resolve_youtube_source_name_prefers_channel(self):
        class FakeYoutubeDLForMetadata(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                self.urls.append(url)
                self.download_called = download
                return {"channel": "Channel_Name", "uploader": "Uploader", "title": "Video Title"}

        with patch("youtube.YoutubeDL", FakeYoutubeDLForMetadata):
            source_name = youtube.resolve_youtube_source_name("https://youtube.com/watch?v=video-1")

        self.assertEqual(source_name, "ChannelName")

    def test_resolve_youtube_source_name_falls_back_to_title(self):
        class FakeYoutubeDLForMetadata(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                _ = url, download
                return {"title": "A_Title_Only"}

        with patch("youtube.YoutubeDL", FakeYoutubeDLForMetadata):
            source_name = youtube.resolve_youtube_source_name("https://youtube.com/watch?v=video-1")

        self.assertEqual(source_name, "ATitleOnly")


class PodcastRetryTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_podcast_download_retries_then_succeeds(self):
        class FlakyPodcastYoutubeDL(FakeYoutubeDL):
            attempts = 0

            def download(self, urls):
                FlakyPodcastYoutubeDL.attempts += 1
                if FlakyPodcastYoutubeDL.attempts < 3:
                    raise Exception("incomplete read")
                return super().download(urls)

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
                    "name": "RetryCast",
                    "url": "https://example.com/feed.rss",
                    "subtitles": False,
                }],
            }

            mp3_url = "https://cdn.example.com/retry-episode.mp3"
            fake_feed = SimpleNamespace(
                entries=[SimpleNamespace(title="Retry Episode", enclosures=[SimpleNamespace(href=mp3_url)])]
            )

            with patch("podcasts.YoutubeDL", FlakyPodcastYoutubeDL), patch(
                "podcasts.feedparser.parse", return_value=fake_feed
            ), patch("podcasts.time.sleep", return_value=None):
                downloaded_items = []
                podcasts.download_podcasts(config, downloaded_items)

            self.assertEqual(FlakyPodcastYoutubeDL.attempts, 3)
            self.assertTrue(any(item.startswith("Podcast: RetryCast") for item in downloaded_items))

            opts = FlakyPodcastYoutubeDL.instances[0].opts
            self.assertTrue(opts["continuedl"])
            self.assertEqual(opts["retries"], 10)
            self.assertEqual(opts["fragment_retries"], 10)
            self.assertEqual(opts["socket_timeout"], 30)


class YoutubeFilteringAndDuplicateTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_skip_filter_uses_video_id_from_url_when_id_missing(self):
        class FakeYoutubeDLForFilter(FakeYoutubeDL):
            match_filter_result = None

            def download(self, urls):
                self.urls.extend(urls)
                fn = self.opts.get("match_filter")
                self.__class__.match_filter_result = fn(
                    {
                        "title": "Daily Episode",
                        "webpage_url": "https://www.youtube.com/watch?v=abc123&list=xyz",
                        "url": "https://r1---sn-a5meknsz.googlevideo.com/videoplayback?expire=123",
                    },
                    incomplete=False,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "abc123",
                    "title": "Daily Episode",
                    "download_status": "downloaded",
                },
            )

            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                    "database_path": db_path,
                },
                "youtube": [{"name": "MyChannel", "url": "https://youtube.com/playlist?list=123", "type": "video"}],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLForFilter):
                youtube.download_youtube_items(config, [])

            self.assertIn("Skipping already downloaded item in DB", FakeYoutubeDLForFilter.match_filter_result)

    def test_skip_filter_allows_forced_redownload_when_db_row_exists(self):
        class FakeYoutubeDLForFilter(FakeYoutubeDL):
            match_filter_result = "not-called"

            def download(self, urls):
                self.urls.extend(urls)
                fn = self.opts.get("match_filter")
                self.__class__.match_filter_result = fn(
                    {
                        "id": "abc123",
                        "title": "Daily Episode",
                        "webpage_url": "https://www.youtube.com/watch?v=abc123",
                    },
                    incomplete=False,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "abc123",
                    "title": "Daily Episode",
                    "download_status": "downloaded",
                },
            )

            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                    "database_path": db_path,
                },
                "youtube": [
                    {
                        "name": "MyChannel",
                        "url": "https://www.youtube.com/watch?v=abc123",
                        "type": "video",
                        "redownload": True,
                    }
                ],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLForFilter):
                youtube.download_youtube_items(config, [])

            self.assertIsNone(FakeYoutubeDLForFilter.match_filter_result)

    def test_skip_filter_excludes_shorts_and_duplicate_titles(self):
        class FakeYoutubeDLForFilter(FakeYoutubeDL):
            match_filter_result = None

            def download(self, urls):
                self.urls.extend(urls)
                fn = self.opts.get("match_filter")
                self.__class__.match_filter_result = fn(
                    {
                        "id": "abc123",
                        "title": "Daily Episode",
                        "webpage_url": "https://www.youtube.com/shorts/abc123",
                    },
                    incomplete=False,
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "existing-1",
                    "title": "Daily Episode",
                    "download_status": "downloaded",
                },
            )

            config = {
                "defaults": {
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "playlist_end": 1,
                    "max_downloads": 1,
                    "output_root": tmpdir,
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "processing_workers": 1,
                    "database_path": db_path,
                },
                "youtube": [{"name": "MyChannel", "url": "https://youtube.com/playlist?list=123", "type": "video"}],
            }

            with patch("youtube.YoutubeDL", FakeYoutubeDLForFilter):
                youtube.download_youtube_items(config, [])

            self.assertEqual(FakeYoutubeDLForFilter.match_filter_result, "Skipping YouTube Shorts entry from playlist.")

    def test_has_episode_title_for_source_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            media_path = os.path.join(tmpdir, "episode-42.mp3")
            Path(media_path).write_text("audio", encoding="utf-8")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "existing-1",
                    "title": "Episode Forty Two",
                    "file_path": media_path,
                    "download_status": "downloaded",
                },
            )

            self.assertTrue(has_episode_title_for_source(db_path, "youtube", "MyChannel", "episode forty two"))
            self.assertFalse(has_episode_title_for_source(db_path, "youtube", "MyChannel", "another episode"))

    def test_has_episode_title_for_source_accepts_missing_files_when_db_has_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            missing_path = os.path.join(tmpdir, "missing.mp3")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "existing-1",
                    "title": "Episode Forty Two",
                    "file_path": missing_path,
                    "download_status": "downloaded",
                },
            )

            self.assertTrue(has_episode_title_for_source(db_path, "youtube", "MyChannel", "episode forty two"))

    def test_is_downloaded_true_when_downloaded_row_exists_even_if_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            upsert_download(
                db_path,
                {
                    "source_type": "youtube",
                    "source_name": "MyChannel",
                    "item_uid": "existing-1",
                    "title": "Episode Forty Two",
                    "file_path": os.path.join(tmpdir, "missing.mp4"),
                    "download_status": "downloaded",
                },
            )

            self.assertTrue(is_downloaded(db_path, "youtube", "MyChannel", "existing-1"))

if __name__ == "__main__":
    unittest.main()
