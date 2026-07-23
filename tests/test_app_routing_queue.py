# ruff: noqa: E402
import os
import sys
import unittest

from unittest.mock import Mock
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

import django

django.setup()

from app.queue import job_priority
from app.queue import publish_job
from app.routing import CLEANUP_QUEUE
from app.routing import FFMPEG_QUEUE
from app.routing import MAX_QUEUE_PRIORITY
from app.routing import PODCAST_DOWNLOAD_QUEUE
from app.routing import SERIAL_EPISODE_CHECK_QUEUE
from app.routing import TRANSCRIPT_QUEUE
from app.routing import YOUTUBE_DOWNLOAD_QUEUE
from app.routing import queue_arguments
from app.routing import queue_name


class AppRoutingTests(unittest.TestCase):
    def test_download_jobs_route_by_source_or_media_type(self):
        self.assertEqual(
            queue_name("download_episode", {"source_type": "podcast"}),
            PODCAST_DOWNLOAD_QUEUE,
        )
        self.assertEqual(
            queue_name("download_single", {"source_type": "youtube"}),
            YOUTUBE_DOWNLOAD_QUEUE,
        )
        self.assertEqual(
            queue_name("download_single", {"media_type": "audio"}),
            PODCAST_DOWNLOAD_QUEUE,
        )
        self.assertEqual(queue_name("download_single", {}), YOUTUBE_DOWNLOAD_QUEUE)

    def test_non_download_jobs_route_to_dedicated_queues(self):
        cases = {
            "check_for_episodes": SERIAL_EPISODE_CHECK_QUEUE,
            "update_downloads": SERIAL_EPISODE_CHECK_QUEUE,
            "transcode_media": FFMPEG_QUEUE,
            "generate_transcript": TRANSCRIPT_QUEUE,
            "retention_cleanup": CLEANUP_QUEUE,
        }
        for job_type, expected_queue in cases.items():
            with self.subTest(job_type=job_type):
                self.assertEqual(queue_name(job_type), expected_queue)

    def test_priority_queues_declare_max_priority(self):
        self.assertEqual(
            queue_arguments(YOUTUBE_DOWNLOAD_QUEUE),
            {"x-max-priority": MAX_QUEUE_PRIORITY},
        )


class JobPriorityTests(unittest.TestCase):
    def test_manual_and_redownload_single_downloads_are_highest_priority(self):
        self.assertEqual(
            job_priority(
                {"job_type": "download_single", "payload": {"manual_enqueue": "yes"}}
            ),
            10,
        )
        self.assertEqual(
            job_priority({"job_type": "download_single", "payload": {"redownload": 1}}),
            10,
        )
        self.assertEqual(job_priority({"job_type": "download_single"}), 9)

    def test_transcript_priority_prefers_audio_and_existing_downloads(self):
        self.assertEqual(
            job_priority(
                {
                    "job_type": "generate_transcript",
                    "payload": {"startup_missing_subtitle": True},
                }
            ),
            2,
        )
        self.assertEqual(
            job_priority(
                {"job_type": "generate_transcript", "payload": {"source_type": "podcast"}}
            ),
            8,
        )
        self.assertEqual(
            job_priority(
                {"job_type": "generate_transcript", "payload": {"media_type": "audio"}}
            ),
            8,
        )
        self.assertEqual(
            job_priority(
                {"job_type": "generate_transcript", "payload": {"download_id": 123}}
            ),
            7,
        )
        self.assertEqual(job_priority({"job_type": "generate_transcript"}), 3)


class PublishJobTests(unittest.TestCase):
    def test_publish_job_clamps_explicit_priority_and_excludes_payload_body(self):
        channel = Mock()
        connection = Mock()
        connection.channel.return_value = channel

        with patch("app.queue.pika.BlockingConnection", return_value=connection):
            publish_job(
                {
                    "job_id": 7,
                    "job_type": "download_single",
                    "payload": {"source_type": "youtube"},
                    "priority": 999,
                }
            )

        channel.queue_declare.assert_called_once_with(
            queue=YOUTUBE_DOWNLOAD_QUEUE,
            durable=True,
            arguments={"x-max-priority": MAX_QUEUE_PRIORITY},
        )
        published = channel.basic_publish.call_args.kwargs
        self.assertEqual(published["routing_key"], YOUTUBE_DOWNLOAD_QUEUE)
        self.assertEqual(published["properties"].priority, MAX_QUEUE_PRIORITY)
        self.assertEqual(published["body"], b'{"job_id": 7, "job_type": "download_single", "priority": 999}')
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
