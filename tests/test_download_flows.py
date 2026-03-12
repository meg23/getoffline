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


def _fake_visualizer_generator(media_path, subtitle_path):
    _ = subtitle_path
    media_path = Path(media_path)
    viz_path = media_path.with_suffix(".visualizer.mp4")
    viz_path.write_text("fake video", encoding="utf-8")
    return viz_path


def _build_sample_config_from_repo_config(output_root):
    with open("config.yaml", encoding="utf-8") as f:
        source = yaml.safe_load(f)

    youtube_entry = next(
        item
        for item in source.get("youtube", [])
        if item.get("type", "audio").lower() == "audio"
        and item.get("subtitles")
        and item.get("visualize")
    )
    podcast_entry = next(
        item
        for item in source.get("podcasts", [])
        if item.get("subtitles") and item.get("visualize")
    )

    return {
        "defaults": {
            "cookie_path": os.path.join(output_root, "cookies.txt"),
            "playlist_end": 1,
            "max_downloads": 1,
            "output_root": output_root,
            "audio_format": "mp3",
            "audio_quality": 0,
            "processing_workers": 1,
            "ad_scrubber": {"enabled": False},
        },
        "youtube": [{
            "name": youtube_entry["name"],
            "url": youtube_entry["url"],
            "type": "audio",
            "scrub": False,
            "subtitles": True,
            "visualize": True,
        }],
        "podcasts": [{
            "name": podcast_entry["name"],
            "url": podcast_entry["url"],
            "scrub": False,
            "subtitles": True,
            "visualize": True,
        }],
    }


class DownloadFlowTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.instances = []

    def test_sample_config_single_youtube_and_podcast_with_subtitles_and_visualizer(self):
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
            ), patch("youtube.generate_whisper_subtitles", side_effect=_fake_subtitle_generator), patch(
                "podcasts.generate_whisper_subtitles", side_effect=_fake_subtitle_generator
            ), patch("youtube.create_audio_visualizer_video", side_effect=_fake_visualizer_generator), patch(
                "podcasts.create_audio_visualizer_video", side_effect=_fake_visualizer_generator
            ):
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
            self.assertTrue(youtube_mp3.with_suffix(".visualizer.mp4").exists())
            self.assertTrue(podcast_mp3.with_suffix(".srt").exists())
            self.assertTrue(podcast_mp3.with_suffix(".visualizer.mp4").exists())

            self.assertTrue(any(item.startswith("YouTube: ") for item in downloaded_items))
            self.assertTrue(any(item.startswith("Podcast: ") for item in downloaded_items))
            self.assertTrue(any(item.startswith("Subtitles: Podcast") for item in downloaded_items))


if __name__ == "__main__":
    unittest.main()
