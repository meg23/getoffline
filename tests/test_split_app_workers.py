# ruff: noqa: E402
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

import django  # noqa: E402
from django.apps import apps  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import connection
from django.test import Client as DjangoClient  # noqa: E402
from django.test import TestCase
from django.utils import timezone

django.setup()


def _ensure_django_test_schema():
    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        for model in apps.get_models():
            if model._meta.db_table in existing_tables:
                continue
            schema_editor.create_model(model)
            existing_tables.add(model._meta.db_table)


def _clear_django_test_data():
    for model in reversed(apps.get_models()):
        model.objects.all().delete()


from models.domain import DownloadStatus
from models.domain import JobStatus
from models.domain import SourceType
from app.queue import job_priority  # noqa: E402
from app.routing import PODCAST_DOWNLOAD_QUEUE
from app.routing import TRANSCRIPT_QUEUE
from app.routing import YOUTUBE_DOWNLOAD_QUEUE
from app.routing import queue_arguments
from app.routing import queue_name

from app.views import _sync_update_downloads_schedule
from app.views import _write_manual_upload
from models.jobs import claim_job  # noqa: E402
from models.jobs import create_job
from models.jobs import finish_job
from models.models import Download  # noqa: E402
from models.models import Job
from models.models import ProfileConfigValue
from models.models import ScheduledJob
from models.models import SourceConfig
from models.models import TranscriptSegment
from models.scheduler import enqueue_due_scheduled_jobs  # noqa: E402
from workers.handlers import _candidates_for_source
from workers.handlers import _delete_ffmpeg_source_files  # noqa: E402
from workers.handlers import _download_with_yt_dlp
from workers.handlers import _downloaded_media_requires_ffmpeg
from workers.handlers import _ffmpeg_video_args
from workers.handlers import _idempotency_key
from workers.handlers import _is_expected_ytdlp_download_error
from workers.handlers import _yt_dlp_download_outtmpl
from workers.handlers import _youtube_candidates
from workers.handlers import check_for_episodes
from workers.handlers import generate_transcript
from workers.handlers import retention_cleanup
from workers.handlers import transcode_media
from workers import runner
from django.contrib.auth.models import User


class AuthenticatedClient(DjangoClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user, created = User.objects.get_or_create(username="default")
        if created:
            user.set_password("pass")
            user.save(update_fields=["password"])
        self.force_login(user)


Client = AuthenticatedClient


def _logged_in_client(username: str, password: str = "pass") -> DjangoClient:
    user, _ = User.objects.get_or_create(username=username)
    user.set_password(password)
    user.save(update_fields=["password"])
    client = DjangoClient()
    if not client.login(username=username, password=password):
        raise AssertionError(f"Could not log in test user {username!r}")
    return client


class SharedDjangoModelTests(TestCase):
    @classmethod
    def setUpClass(cls):
        _ = cls
        _ensure_django_test_schema()
        super().setUpClass()

    def setUp(self):
        super().setUp()
        _clear_django_test_data()

    def test_expected_ytdlp_unavailable_errors_are_nonfatal(self):
        class DownloadError(Exception):
            pass

        self.assertTrue(
            _is_expected_ytdlp_download_error(
                DownloadError(
                    "ERROR: [youtube] HiQ3UtXDgGE: Video unavailable. "
                    "This video has been removed by the uploader"
                )
            )
        )

    def test_unexpected_ytdlp_errors_are_not_suppressed(self):
        class DownloadError(Exception):
            pass

        self.assertFalse(
            _is_expected_ytdlp_download_error(DownloadError("network fail"))
        )
        self.assertFalse(
            _is_expected_ytdlp_download_error(TypeError("video unavailable"))
        )

    def test_ytdlp_outtmpl_bounds_title_and_id_components(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = _yt_dlp_download_outtmpl(Path(tmpdir))

        self.assertIn("%(title).160B", outtmpl)
        self.assertIn("%(id).48B", outtmpl)
        self.assertNotIn("[%(id)s]", outtmpl)

    def test_deferred_video_screening_runs_in_transcript_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media_path = root / "converted.mp4"
            media_path.write_bytes(b"video")
            subtitle_path = root / "converted.srt"
            subtitle_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nclean text\n",
                encoding="utf-8",
            )
            source_path = root / "source.webm"
            source_path.write_bytes(b"source")
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            job = create_job(
                profile_id="default",
                job_type="generate_transcript",
                payload={
                    "deferred_download_lookup": {
                        "profile_id": "default",
                        "source_type": SourceType.YOUTUBE.value,
                        "source_name": "Test Channel",
                        "item_uid": "video-1",
                    },
                    "deferred_download_defaults": {
                        "title": "Converted video",
                        "file_path": str(media_path),
                        "file_path_relative": "converted.mp4",
                        "file_ext": "mp4",
                        "file_size_bytes": media_path.stat().st_size,
                        "download_status": "downloaded",
                    },
                    "deferred_media_path": str(media_path),
                    "ffmpeg_source_file_paths": [str(source_path)],
                    "delete_explicit_content": True,
                    "subtitles": True,
                },
                idempotency_key="generate_transcript:default:deferred:video-1",
            )

            with (
                patch("workers.handlers.create_subtitles", return_value=subtitle_path),
                patch("workers.handlers.screen_transcript", return_value=None),
            ):
                generate_transcript(job)

            download = Download.objects.get(item_uid="video-1")
            self.assertEqual(download.file_path, str(media_path))
            self.assertEqual(download.subtitle_path, str(subtitle_path))
            self.assertFalse(source_path.exists())
            self.assertEqual(
                TranscriptSegment.objects.filter(download=download).count(), 1
            )

    def test_filtered_video_transcode_queues_transcript_screening(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_path = root / "source.webm"
            source_path.write_bytes(b"source")
            target_path = root / "converted.mp4"
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            job = create_job(
                profile_id="default",
                job_type="transcode_media",
                payload={
                    "source_file_path": str(source_path),
                    "source_file_paths": [str(source_path)],
                    "target_file_path": str(target_path),
                    "output_root": str(root),
                    "media_type": "video",
                    "delete_explicit_content": True,
                    "download_lookup": {
                        "profile_id": "default",
                        "source_type": SourceType.YOUTUBE.value,
                        "source_name": "Test Channel",
                        "item_uid": "video-2",
                    },
                    "download_defaults": {
                        "title": "Converted video",
                        "file_path": str(source_path),
                        "file_ext": "webm",
                        "file_size_bytes": source_path.stat().st_size,
                        "download_status": "downloaded",
                    },
                    "subtitles": True,
                },
                idempotency_key="transcode_media:default:video-2",
            )

            def fake_run(command, check, capture_output, text):
                self.assertIn(str(target_path), command)
                target_path.write_bytes(b"converted")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("workers.handlers.subprocess.run", side_effect=fake_run),
                patch("workers.handlers._publish_created_job"),
                patch(
                    "workers.handlers._screen_deferred_video_before_insert"
                ) as screen_deferred,
            ):
                transcode_media(job)

            screen_deferred.assert_not_called()
            self.assertFalse(Download.objects.filter(item_uid="video-2").exists())
            child = Job.objects.get(
                job_type="generate_transcript",
                idempotency_key=(
                    "generate_transcript:default:deferred:youtube:"
                    "Test Channel:video-2"
                ),
            )
            self.assertEqual(child.payload["deferred_media_path"], str(target_path))
            self.assertEqual(
                child.payload["deferred_download_defaults"]["file_path"],
                str(target_path),
            )
            self.assertEqual(
                child.payload["ffmpeg_source_file_paths"], [str(source_path)]
            )

    def test_library_does_not_expose_removed_transfer_button(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="update-form"')
        self.assertContains(response, 'name="job_type" value="update_downloads"')
        self.assertNotContains(response, 'id="transfer-form"')
        self.assertNotContains(response, 'name="job_type" value="transfer_media"')

    def test_json_enqueue_rejects_removed_transfer_job(self):
        client = Client()

        with patch("app.views.publish_job"):
            response = client.post(
                "/jobs/enqueue/",
                {"job_type": "transfer_media"},
                HTTP_ACCEPT="application/json",
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 400)

    def test_create_claim_and_finish_job(self):
        job = create_job(
            profile_id="default",
            job_type="retention_cleanup",
            payload={"source": "test"},
            idempotency_key="retention_cleanup:default:test",
        )
        claimed = claim_job(job.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, JobStatus.RUNNING)
        finish_job(claimed, status=JobStatus.SUCCEEDED)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, JobStatus.SUCCEEDED)
        self.assertEqual(claimed.payload, {"source": "test"})

    def test_idempotency_reuses_queued_job(self):
        first = create_job(
            profile_id="default",
            job_type="update_downloads",
            idempotency_key="update:default",
        )
        second = create_job(
            profile_id="default",
            job_type="update_downloads",
            idempotency_key="update:default",
        )
        self.assertEqual(first.id, second.id)

    def test_manual_upload_writes_download_metadata_with_original_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=tmpdir
            )
            uploaded = SimpleUploadedFile(
                "Vacation Clip.mp4", b"video-bytes", content_type="video/mp4"
            )

            download, path = _write_manual_upload("default", uploaded)

            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"video-bytes")
            self.assertEqual(path.parent, Path(tmpdir) / "manual")
            self.assertEqual(download.source_type, "manual")
            self.assertEqual(download.source_name, "Manual Uploads")
            self.assertEqual(download.title, "Vacation Clip.mp4")
            self.assertEqual(download.file_ext, "mp4")
            self.assertEqual(download.file_path_relative, "manual/Vacation Clip.mp4")

    def test_manual_upload_endpoint_queues_transcript_pipeline(self):
        client = Client()
        user, _created = User.objects.get_or_create(username="default")
        user.set_password("pass")
        user.save(update_fields=["password"])
        self.assertTrue(client.login(username="default", password="pass"))
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("app.views.publish_job") as publish,
        ):
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=tmpdir
            )

            response = client.post(
                "/manual-upload/",
                {
                    "files": SimpleUploadedFile(
                        "Drop Movie.webm", b"video-bytes", content_type="video/webm"
                    )
                },
            )

            self.assertEqual(response.status_code, 201)
            download = Download.objects.get(title="Drop Movie.webm")
            job = Job.objects.get(
                job_type="generate_transcript", payload__download_id=download.id
            )
            self.assertEqual(job.payload["source_type"], "manual")
            self.assertEqual(job.payload["media_type"], "video")
            publish.assert_called_once_with(
                {
                    "job_id": job.id,
                    "job_type": "generate_transcript",
                    "profile_id": "default",
                    "attempt": 1,
                }
            )

    def test_library_preview_keeps_default_limit(self):
        base_seen_at = timezone.now()
        for index in range(105):
            Download.objects.create(
                profile_id="default",
                source_type="manual",
                source_name="Manual Uploads",
                title=f"Library Item {index:03d}",
                file_ext="mp3",
                download_status="downloaded",
                last_seen_at=base_seen_at - timedelta(seconds=index),
            )

        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Library Item 000")
        self.assertNotContains(response, "Library Item 104")
        self.assertEqual(
            response.content.count(b"<tr\n                data-row-id="), 100
        )

    def test_library_all_filter_renders_every_database_download(self):
        base_seen_at = timezone.now()
        for index in range(105):
            Download.objects.create(
                profile_id="default",
                source_type="manual",
                source_name="Manual Uploads",
                title=f"Library Item {index:03d}",
                file_ext="mp3",
                download_status="downloaded",
                last_seen_at=base_seen_at - timedelta(seconds=index),
            )

        response = Client().get("/?filter=all")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Library Item 000")
        self.assertContains(response, "Library Item 104")
        self.assertEqual(
            response.content.count(b"<tr\n                data-row-id="), 105
        )
        self.assertContains(response, 'data-server-mode="all"')

    def test_scheduler_disables_removed_job_types(self):
        due_at = timezone.now() - timedelta(minutes=1)
        schedule = ScheduledJob.objects.create(
            profile_id="default",
            job_type="removed_feature",
            interval_seconds=3600,
            payload={"source": "test-scheduler"},
            idempotency_key_template="scheduled:${job_type}:${profile_id}:${due_hour}",
            next_run_at=due_at,
        )

        with patch("models.scheduler.publish_job") as publish:
            job_ids = enqueue_due_scheduled_jobs(now=timezone.now())

        self.assertEqual(job_ids, [])
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        publish.assert_not_called()

    def test_auto_update_setting_creates_enabled_update_schedule(self):
        now = timezone.now()

        _sync_update_downloads_schedule("alice", "15", now=now)

        schedule = ScheduledJob.objects.get(
            profile_id="alice", job_type="update_downloads"
        )
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_seconds, 900)
        self.assertEqual(schedule.payload, {"source": "scheduler"})
        self.assertEqual(
            schedule.idempotency_key_template,
            "scheduled:update_downloads:${profile_id}:${due_hour}",
        )
        self.assertGreater(schedule.next_run_at, now)

    def test_auto_update_setting_zero_disables_update_schedule(self):
        schedule = ScheduledJob.objects.create(
            profile_id="alice",
            job_type="update_downloads",
            interval_seconds=900,
            payload={"source": "scheduler"},
            idempotency_key_template="scheduled:update_downloads:${profile_id}:${due_hour}",
            next_run_at=timezone.now(),
        )

        _sync_update_downloads_schedule("alice", "0")

        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)

    def test_save_config_syncs_auto_update_schedule_for_logged_in_user(self):
        client = Client()
        User.objects.create_user(username="alice", password="pass")
        self.assertTrue(client.login(username="alice", password="pass"))

        response = client.post("/settings/save/", {"config__auto_update_minutes": "7"})

        self.assertEqual(response.status_code, 302)
        schedule = ScheduledJob.objects.get(
            profile_id="alice", job_type="update_downloads"
        )
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_seconds, 420)

    def test_add_source_rejects_oversized_name_without_server_error(self):
        client = Client()
        User.objects.create_user(username="alice", password="pass")
        self.assertTrue(client.login(username="alice", password="pass"))

        response = client.post(
            "/sources/add/",
            {
                "source_type": "youtube",
                "name": "x" * 256,
                "url": "https://example.com/feed",
                "media_type": "video",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SourceConfig.objects.filter(profile_id="alice").exists())

    def test_retention_cleanup_deletes_expired_non_favorite_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            expired = Path(tmpdir) / "expired.mp4"
            favorite = Path(tmpdir) / "favorite.mp4"
            expired.write_text("old", encoding="utf-8")
            favorite.write_text("fav", encoding="utf-8")
            old_time = timezone.now() - timedelta(days=10)
            ProfileConfigValue.objects.create(
                profile_id="default", key="auto_delete_content_days", value="7"
            )
            expired_download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                title="Expired",
                file_path=str(expired),
                download_status="downloaded",
                completed_at=old_time,
            )
            favorite_download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                title="Favorite",
                file_path=str(favorite),
                download_status="downloaded",
                completed_at=old_time,
                favorite=True,
            )
            job = Job.objects.create(
                profile_id="default", job_type="retention_cleanup", payload={}
            )

            retention_cleanup(job)

            expired_download.refresh_from_db()
            favorite_download.refresh_from_db()
            self.assertFalse(expired.exists())
            self.assertTrue(favorite.exists())
            self.assertEqual(expired_download.download_status, "retention_deleted")
            self.assertEqual(favorite_download.download_status, "downloaded")

    def test_library_marks_sibling_podcast_subtitles_when_database_path_missing(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            media = root / "episode.mp3"
            media.write_text("audio", encoding="utf-8")
            media.with_suffix(".srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8"
            )
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.PODCAST,
                source_name="Podcast",
                item_uid="episode-1",
                title="Podcast Episode",
                file_path=str(media),
                file_ext="mp3",
                download_status="downloaded",
                subtitle_path=str(media.with_suffix(".srt")),
            )

            with patch("app.views.publish_job"):
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('data-has-subtitles="1"', body)
        self.assertIn(f"/subtitle/{download.id}/", body)

    def test_subtitle_endpoint_converts_srt_to_vtt_for_browser_tracks(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            media = root / "video.mp4"
            subtitle = root / "video.srt"
            media.write_text("video", encoding="utf-8")
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,250\ncaption text\n", encoding="utf-8"
            )
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                item_uid="video-1",
                title="Video",
                file_path=str(media),
                file_ext="mp4",
                download_status="downloaded",
                subtitle_path=str(subtitle),
            )

            response = client.get(f"/subtitle/{download.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/vtt; charset=utf-8")
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.250", body)

    def test_media_endpoint_honors_open_ended_range_to_eof(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            media = root / "video.mp4"
            media.write_bytes(b"a" * (2 * 1024 * 1024))
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                item_uid="range-video-1",
                title="Range Video",
                file_path=str(media),
                file_ext="mp4",
                download_status="downloaded",
            )

            response = client.get(f"/media/{download.id}/", HTTP_RANGE="bytes=0-")
            body = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Length"], str(2 * 1024 * 1024))
        self.assertEqual(
            response["Content-Range"],
            f"bytes 0-{2 * 1024 * 1024 - 1}/{2 * 1024 * 1024}",
        )
        self.assertEqual(len(body), 2 * 1024 * 1024)

    def test_media_endpoint_honors_explicit_range_end(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            media = root / "clip.mp4"
            media.write_bytes(b"0123456789")
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                item_uid="range-video-2",
                title="Range Video",
                file_path=str(media),
                file_ext="mp4",
                download_status="downloaded",
            )

            response = client.get(f"/media/{download.id}/", HTTP_RANGE="bytes=2-5")
            body = b"".join(response.streaming_content)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Length"], "4")
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(body, b"2345")

    def test_video_player_omits_subtitle_track_by_default(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ProfileConfigValue.objects.create(
                profile_id="default", key="output_root", value=str(root)
            )
            media = root / "video.mp4"
            subtitle = root / "video.srt"
            media.write_text("video", encoding="utf-8")
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,250\ncaption text\n", encoding="utf-8"
            )
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                item_uid="video-player-1",
                title="Video Player",
                file_path=str(media),
                file_ext="mp4",
                download_status="downloaded",
                subtitle_path=str(subtitle),
                last_position_seconds=42.5,
            )

            with patch("app.views.publish_job"):
                response = client.get(f"/play/{download.id}/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        player_script = (
            Path(__file__).parents[1] / "src/app/static/app/player.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn('id="subtitle-track"', body)
        self.assertIn(f"/media/{download.id}/#t=42.500", body)
        self.assertIn('/static/app/player.js"', body)
        self.assertIn("const periodicProgressSeconds = 5;", player_script)
        self.assertIn('media?.addEventListener("timeupdate"', player_script)
        self.assertIn('media?.addEventListener("pause"', player_script)
        self.assertIn('media?.addEventListener("seeked"', player_script)
        self.assertIn('media?.addEventListener("ended"', player_script)
        self.assertIn('window.addEventListener("pagehide"', player_script)
        self.assertIn("navigator.sendBeacon(form.action, body)", player_script)

    def test_django_player_position_endpoint_persists_resume_and_completion(self):
        client = Client()
        download = Download.objects.create(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            source_name="Channel",
            item_uid="position-video-1",
            title="Position Video",
            file_path="/tmp/position-video.mp4",
            file_ext="mp4",
            download_status="downloaded",
            last_position_seconds=0.0,
        )

        response = client.post(
            f"/downloads/{download.id}/position/",
            {"position_seconds": "37.250", "reason": "timeupdate"},
        )
        download.refresh_from_db()

        self.assertEqual(response.status_code, 204)
        self.assertAlmostEqual(download.last_position_seconds, 37.25, places=2)
        self.assertFalse(download.played)

        response = client.post(
            f"/downloads/{download.id}/position/",
            {"position_seconds": "0", "reason": "ended"},
        )
        download.refresh_from_db()

        self.assertEqual(response.status_code, 204)
        self.assertAlmostEqual(download.last_position_seconds, 0.0, places=2)
        self.assertTrue(download.played)

    def test_logged_in_user_playback_resume_and_position_are_profile_scoped(self):
        client = _logged_in_client("alice")
        other_client = _logged_in_client("bob")
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "alice-position.mp3"
            media.write_text("audio", encoding="utf-8")
            download = Download.objects.create(
                profile_id="alice",
                source_type=SourceType.PODCAST,
                source_name="Alice Podcast",
                item_uid="alice-position-1",
                title="Alice Resume Episode",
                file_path=str(media),
                file_ext="mp3",
                download_status="downloaded",
                last_position_seconds=12.5,
                total_listened_seconds=4.0,
            )

            response = client.get(f"/play/{download.id}/?t=42.125")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(f"/media/{download.id}/#t=42.125", body)
        self.assertIn("Alice Resume Episode", body)

        response = client.post(
            f"/downloads/{download.id}/position/",
            {"position_seconds": "50.500", "reason": "mini-timeupdate"},
        )
        self.assertEqual(response.status_code, 204)
        download.refresh_from_db()
        self.assertAlmostEqual(download.last_position_seconds, 50.5, places=2)
        self.assertAlmostEqual(download.total_listened_seconds, 42.0, places=2)
        self.assertFalse(download.played)
        self.assertIsNotNone(download.last_position_updated_at)

        response = other_client.get(f"/play/{download.id}/")
        self.assertEqual(response.status_code, 404)
        response = other_client.post(
            f"/downloads/{download.id}/position/",
            {"position_seconds": "1", "reason": "timeupdate"},
        )
        self.assertEqual(response.status_code, 404)
        download.refresh_from_db()
        self.assertAlmostEqual(download.last_position_seconds, 50.5, places=2)

    def test_logged_in_library_exposes_playback_dataset_for_own_profile_only(self):
        client = _logged_in_client("alice")
        Download.objects.create(
            profile_id="bob",
            source_type=SourceType.YOUTUBE,
            source_name="Bob Channel",
            item_uid="bob-hidden-1",
            title="Bob Hidden Video",
            file_path="/tmp/bob-hidden.mp4",
            file_ext="mp4",
            download_status="downloaded",
            last_position_seconds=9.0,
        )
        download = Download.objects.create(
            profile_id="alice",
            source_type=SourceType.YOUTUBE,
            source_name="Alice Channel",
            item_uid="alice-visible-1",
            title="Alice Visible Video",
            file_path="/tmp/alice-visible.mp4",
            file_ext="mp4",
            download_status="downloaded",
            last_position_seconds=33.25,
        )

        with patch("app.views.publish_job"):
            response = client.get("/?filter=all")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("Alice Visible Video", body)
        self.assertIn(f'data-row-id="{download.id}"', body)
        self.assertIn(f'data-media-url="/media/{download.id}/"', body)
        self.assertIn('data-resume-seconds="33.25"', body)
        self.assertIn(f'href="/play/{download.id}/"', body)
        self.assertNotIn("Bob Hidden Video", body)

    def test_anonymous_users_must_login_before_playback_or_position_updates(self):
        client = DjangoClient()
        download = Download.objects.create(
            profile_id="alice",
            source_type=SourceType.YOUTUBE,
            source_name="Alice Channel",
            item_uid="anonymous-position-1",
            title="Login Required Video",
            file_path="/tmp/login-required.mp4",
            file_ext="mp4",
            download_status="downloaded",
            last_position_seconds=7.0,
        )

        response = client.get(f"/play/{download.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/login/?next=/play/{download.id}/", response["Location"])

        response = client.post(
            f"/downloads/{download.id}/position/",
            {"position_seconds": "99", "reason": "timeupdate"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"/login/?next=/downloads/{download.id}/position/", response["Location"]
        )
        download.refresh_from_db()
        self.assertAlmostEqual(download.last_position_seconds, 7.0, places=2)

    def test_enqueue_job_ajax_proxy_preserves_json_headers(self):
        client = Client()

        with patch("app.views.publish_job"):
            response = client.post(
                "/jobs/enqueue/",
                {"job_type": "update_downloads", "next": "/"},
                HTTP_ACCEPT="application/json",
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("status_url", payload)

    def test_quick_add_form_returns_to_library_after_queueing_download(self):
        template = Path("src/app/templates/app/library.html").read_text()

        self.assertIn('name="next" value="{% url \'library\' %}"', template)

    def test_enqueue_job_redirects_to_next_when_present(self):
        client = Client()

        with patch("app.views.publish_job") as publish:
            response = client.post(
                "/jobs/enqueue/",
                {
                    "profile_id": "default",
                    "job_type": "update_downloads",
                    "next": "/?profile_id=default",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/?profile_id=default")
        job = Job.objects.get(job_type="update_downloads")
        publish.assert_called_once_with(
            {
                "job_id": job.id,
                "job_type": "update_downloads",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    def test_batch_update_purge_deletes_media_and_database_record(self):
        client = Client()
        user, _created = User.objects.get_or_create(username="default")
        user.set_password("pass")
        user.save(update_fields=["password"])
        self.assertTrue(client.login(username="default", password="pass"))

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS media_summaries")
            cursor.execute(
                "CREATE TABLE media_summaries ("
                "id integer primary key, "
                "download_id integer not null references downloads(id), "
                "summary text)"
            )
        self.addCleanup(self._drop_media_summaries_test_table)
        with tempfile.TemporaryDirectory() as tmpdir:
            media_path = Path(tmpdir) / "purge-me.mp3"
            media_path.write_text("audio", encoding="utf-8")
            download = Download.objects.create(
                profile_id="default",
                source_type="youtube",
                source_name="Test Channel",
                item_uid="video-purge",
                title="Purge Me",
                file_path=str(media_path),
                file_ext="mp3",
                download_status="downloaded",
            )
            directory_path = Path(tmpdir) / "directory-media"
            directory_path.mkdir()
            directory_download = Download.objects.create(
                profile_id="default",
                source_type="youtube",
                source_name="Test Channel",
                item_uid="directory-purge",
                title="Directory Purge",
                file_path=str(directory_path),
                file_ext="",
                download_status="downloaded",
            )
            TranscriptSegment.objects.create(
                download=download,
                subtitle_path=str(media_path.with_suffix(".srt")),
                start_seconds=0.0,
                end_seconds=1.0,
                text="hello",
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO media_summaries (download_id, summary) VALUES (%s, %s)",
                    [download.id, "legacy summary"],
                )

            response = client.post(
                "/batch-update/",
                {
                    "ids": [str(download.id), str(directory_download.id)],
                    "batch_action": "purge",
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertFalse(media_path.exists())
            self.assertTrue(directory_path.exists())
            self.assertFalse(Download.objects.filter(pk=download.pk).exists())
            self.assertFalse(Download.objects.filter(pk=directory_download.pk).exists())
            self.assertFalse(
                TranscriptSegment.objects.filter(download_id=download.pk).exists()
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM media_summaries WHERE download_id = %s",
                    [download.pk],
                )
                self.assertEqual(cursor.fetchone()[0], 0)

    def _drop_media_summaries_test_table(self):

        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS media_summaries")

    def test_batch_update_transcript_refresh_supersedes_active_job(self):
        client = Client()
        user, _created = User.objects.get_or_create(username="default")
        user.set_password("pass")
        user.save(update_fields=["password"])
        self.assertTrue(client.login(username="default", password="pass"))
        download = Download.objects.create(
            profile_id="default",
            source_type="youtube",
            source_name="Test Channel",
            item_uid="video-refresh-active",
            title="Refresh Active",
            file_path="/tmp/refresh-active.mp4",
            file_ext="mp4",
            download_status="downloaded",
        )
        active_job = Job.objects.create(
            profile_id="default",
            job_type="generate_transcript",
            status=JobStatus.RUNNING,
            payload={"download_id": download.id},
            idempotency_key=f"generate_transcript:default:{download.id}",
        )

        with patch("app.views.publish_job") as publish:
            response = client.post(
                "/batch-update/",
                {"ids": [str(download.id)], "batch_action": "download"},
            )

        self.assertEqual(response.status_code, 302)
        active_job.refresh_from_db()
        self.assertEqual(active_job.status, JobStatus.RUNNING)
        replacement = Job.objects.get(
            job_type="download_single",
            status=JobStatus.QUEUED,
            idempotency_key=f"download_single:default:{download.id}",
        )
        self.assertEqual(replacement.payload["redownload"], True)
        publish.assert_called_once_with(
            {
                "job_id": replacement.id,
                "job_type": "download_single",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    def test_candidates_for_source_dispatches_reloaded_source_type_strings(self):
        youtube_source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="Test Channel",
            url="https://www.youtube.com/@example/videos",
            enabled=True,
        )
        podcast_source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.PODCAST,
            name="Test Podcast",
            url="https://example.com/feed.xml",
            enabled=True,
        )
        youtube_source.refresh_from_db()
        podcast_source.refresh_from_db()

        with (
            patch(
                "workers.handlers._youtube_candidates",
                return_value=[{"item_uid": "yt"}],
            ) as youtube_candidates,
            patch(
                "workers.handlers._podcast_candidates",
                return_value=[{"item_uid": "pod"}],
            ) as podcast_candidates,
        ):
            self.assertEqual(
                list(_candidates_for_source(youtube_source)), [{"item_uid": "yt"}]
            )
            self.assertEqual(
                list(_candidates_for_source(podcast_source)), [{"item_uid": "pod"}]
            )

        youtube_candidates.assert_called_once_with(youtube_source)
        podcast_candidates.assert_called_once_with(podcast_source)

    def test_candidates_for_source_skips_invalid_urls(self):
        source = SourceConfig(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="Invalid source",
            url="default",
            media_type="video",
        )

        with patch("workers.handlers._youtube_entries_from_url") as extract:
            self.assertEqual(list(_candidates_for_source(source)), [])

        extract.assert_not_called()

    def test_episode_checker_honors_source_max_downloads(self):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="Test Channel",
            url="https://www.youtube.com/@example/videos",
            enabled=True,
            max_downloads=1,
        )
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=JobStatus.QUEUED,
        )
        candidates = iter(
            [
                {
                    "item_uid": "video-1",
                    "item_url": "https://youtu.be/1",
                    "media_url": "https://youtu.be/1",
                    "title": "One",
                },
                {
                    "item_uid": "video-2",
                    "item_url": "https://youtu.be/2",
                    "media_url": "https://youtu.be/2",
                    "title": "Two",
                },
                {
                    "item_uid": "video-3",
                    "item_url": "https://youtu.be/3",
                    "media_url": "https://youtu.be/3",
                    "title": "Three",
                },
            ]
        )
        with (
            patch("workers.handlers._candidates_for_source", return_value=candidates),
            patch("workers.handlers._publish_created_job") as publish,
        ):
            check_for_episodes(job)
        jobs = Job.objects.filter(
            job_type="download_episode", payload__source_id=source.id
        )
        self.assertEqual(jobs.count(), 1)
        self.assertEqual(jobs.first().payload["item_uid"], "video-1")
        publish.assert_called_once()

    def test_episode_checker_republishes_existing_queued_download_job(self):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="Test Channel",
            url="https://www.youtube.com/@example/videos",
            enabled=True,
            max_downloads=1,
        )
        existing = create_job(
            profile_id="default",
            job_type="download_episode",
            payload={
                "source_id": source.id,
                "source_type": SourceType.YOUTUBE,
                "item_uid": "video-1",
            },
            idempotency_key=_idempotency_key(
                "download_episode", "default", source.id, "video-1"
            ),
        )
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=JobStatus.QUEUED,
        )
        candidates = iter(
            [
                {
                    "item_uid": "video-1",
                    "item_url": "https://youtu.be/1",
                    "media_url": "https://youtu.be/1",
                    "title": "One",
                },
            ]
        )

        with (
            patch("workers.handlers._candidates_for_source", return_value=candidates),
            patch("workers.handlers._publish_created_job") as publish,
        ):
            check_for_episodes(job)

        self.assertEqual(
            Job.objects.filter(
                job_type="download_episode", payload__source_id=source.id
            ).count(),
            1,
        )
        publish.assert_called_once_with(existing)

    def test_episode_checker_resets_and_republishes_stale_running_download_job(self):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="Test Channel",
            url="https://www.youtube.com/@example/videos",
            enabled=True,
            max_downloads=1,
        )
        existing = create_job(
            profile_id="default",
            job_type="download_episode",
            payload={
                "source_id": source.id,
                "source_type": SourceType.YOUTUBE,
                "item_uid": "video-1",
            },
            idempotency_key=_idempotency_key(
                "download_episode", "default", source.id, "video-1"
            ),
        )
        stale_started_at = timezone.now() - timedelta(hours=7)
        Job.objects.filter(pk=existing.id).update(
            status=JobStatus.RUNNING,
            started_at=stale_started_at,
            updated_at=stale_started_at,
        )
        existing.refresh_from_db()
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=JobStatus.QUEUED,
        )
        candidates = iter(
            [
                {
                    "item_uid": "video-1",
                    "item_url": "https://youtu.be/1",
                    "media_url": "https://youtu.be/1",
                    "title": "One",
                },
            ]
        )

        with (
            patch("workers.handlers._candidates_for_source", return_value=candidates),
            patch("workers.handlers._publish_created_job") as publish,
        ):
            check_for_episodes(job)

        existing.refresh_from_db()
        self.assertEqual(existing.status, JobStatus.QUEUED)
        self.assertIsNone(existing.started_at)
        publish.assert_called_once_with(existing)

    def test_youtube_candidates_drill_into_channel_videos_tab(self):
        source = SourceConfig(
            id=4,
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="gamer",
            url="https://www.youtube.com/@gameranxTV",
            enabled=True,
            max_downloads=1,
        )
        tab_entry = {
            "id": "UCNvzD7Z-g64bPXxGzaQaa4g",
            "title": "gameranx - Videos",
            "url": "https://www.youtube.com/@gameranxTV/videos",
        }
        video_entry = {
            "id": "abcdefghijk",
            "title": "Actual newest upload",
            "url": "abcdefghijk",
        }
        with patch(
            "workers.handlers._youtube_entries_from_url",
            side_effect=[[tab_entry], [video_entry]],
        ):
            candidates = list(_youtube_candidates(source))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["item_uid"], "abcdefghijk")
        self.assertEqual(
            candidates[0]["media_url"], "https://www.youtube.com/watch?v=abcdefghijk"
        )

    def test_youtube_candidates_skip_live_titled_flat_entries_by_default(self):
        source = SourceConfig(
            id=5,
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="streamer",
            url="https://www.youtube.com/playlist?list=uploads",
            enabled=True,
            max_downloads=2,
            include_livestreams=False,
        )
        live_entry = {
            "id": "MbEO1g8_COs",
            "title": "Birthday Hangs & Talkin' VIDEO GAMES... | LIVE GAMING NEWS 🔴",
            "url": "MbEO1g8_COs",
        }
        upload_entry = {
            "id": "abcdefghijk",
            "title": "Actual newest upload",
            "url": "abcdefghijk",
        }
        with patch(
            "workers.handlers._youtube_entries_from_url",
            return_value=[live_entry, upload_entry],
        ):
            candidates = list(_youtube_candidates(source))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["item_uid"], "abcdefghijk")

    def test_youtube_candidates_allow_live_titled_flat_entries_when_enabled(self):
        source = SourceConfig(
            id=6,
            profile_id="default",
            source_type=SourceType.YOUTUBE,
            name="streamer",
            url="https://www.youtube.com/playlist?list=uploads",
            enabled=True,
            max_downloads=1,
            include_livestreams=True,
        )
        live_entry = {
            "id": "MbEO1g8_COs",
            "title": "Birthday Hangs & Talkin' VIDEO GAMES... | LIVE GAMING NEWS 🔴",
            "url": "MbEO1g8_COs",
        }
        with patch(
            "workers.handlers._youtube_entries_from_url", return_value=[live_entry]
        ):
            candidates = list(_youtube_candidates(source))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["item_uid"], "MbEO1g8_COs")

    def test_video_without_explicit_delete_is_inserted_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            media_file = output_root / "Channel" / "Fast Video [fast-video].mp4"

            class FakeYoutubeDLForFastVideo:
                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, _tb):
                    return False

                def extract_info(self, url, download):
                    media_file.parent.mkdir(parents=True, exist_ok=True)
                    media_file.write_text("raw video", encoding="utf-8")
                    for hook in self.opts.get("progress_hooks", []):
                        hook(
                            {
                                "status": "finished",
                                "filename": str(media_file),
                                "info_dict": {"title": "Fast Video"},
                            }
                        )
                    return {
                        "id": "fast-video",
                        "title": "Fast Video",
                        "webpage_url": "https://youtube.com/watch?v=fast-video",
                        "filepath": str(media_file),
                    }

                def prepare_filename(self, info):
                    return str(media_file)

            fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDLForFastVideo)
            job = Job.objects.create(
                profile_id="default",
                job_type="download_episode",
                payload={
                    "source_type": SourceType.YOUTUBE,
                    "source_name": "Channel",
                    "media_type": "video",
                    "item_uid": "fast-video",
                    "item_url": "https://youtube.com/watch?v=fast-video",
                    "media_url": "https://youtube.com/watch?v=fast-video",
                    "delete_explicit_content": False,
                },
            )

            with (
                patch.dict(sys.modules, {"yt_dlp": fake_module}),
                patch(
                    "workers.handlers._download_output_root", return_value=output_root
                ),
            ):
                result = _download_with_yt_dlp(job, job.payload)

            self.assertIsInstance(result, dict)
            self.assertIn("download_id", result)
            self.assertNotIn("download_lookup", result)
            download = Download.objects.get(pk=result["download_id"])
            self.assertEqual(download.download_status, "downloaded")
            self.assertEqual(download.file_path, str(media_file.resolve()))
            self.assertEqual(result["source_file_paths"], [str(media_file.resolve())])
            self.assertFalse(result["delete_explicit_content"])

    def test_video_with_explicit_delete_is_screened_before_database_insert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            media_file = output_root / "Channel" / "Clean Video [clean-video].mp4"
            subtitle_file = media_file.with_suffix(".srt")

            class FakeYoutubeDLForCleanVideo:
                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, _tb):
                    return False

                def extract_info(self, url, download):
                    media_file.parent.mkdir(parents=True, exist_ok=True)
                    media_file.write_text("raw video", encoding="utf-8")
                    return {
                        "id": "clean-video",
                        "title": "Clean Video",
                        "webpage_url": "https://youtube.com/watch?v=clean-video",
                        "filepath": str(media_file),
                    }

                def prepare_filename(self, info):
                    return str(media_file)

            def fake_create_subtitles(*_args, **_kwargs):
                subtitle_file.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nclean words\n",
                    encoding="utf-8",
                )
                return subtitle_file

            fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDLForCleanVideo)
            job = Job.objects.create(
                profile_id="default",
                job_type="download_episode",
                payload={
                    "source_type": SourceType.YOUTUBE,
                    "source_name": "Channel",
                    "media_type": "video",
                    "item_uid": "clean-video",
                    "item_url": "https://youtube.com/watch?v=clean-video",
                    "media_url": "https://youtube.com/watch?v=clean-video",
                    "delete_explicit_content": True,
                    "subtitles": True,
                },
            )

            with (
                patch.dict(sys.modules, {"yt_dlp": fake_module}),
                patch(
                    "workers.handlers._download_output_root", return_value=output_root
                ),
                patch(
                    "workers.handlers._downloaded_media_requires_ffmpeg",
                    return_value=(False, "mp4"),
                ),
                patch(
                    "workers.handlers.create_subtitles",
                    side_effect=fake_create_subtitles,
                ) as subtitles,
                patch(
                    "workers.handlers.screen_transcript", return_value=None
                ) as screen,
            ):
                result = _download_with_yt_dlp(job, job.payload)

            subtitles.assert_called_once()
            screen.assert_called_once_with(subtitle_file)
            self.assertIsInstance(result, Download)
            self.assertEqual(Download.objects.count(), 1)
            download = Download.objects.get()
            self.assertEqual(download.item_uid, "clean-video")
            self.assertEqual(download.download_status, "downloaded")
            self.assertEqual(download.subtitle_path, str(subtitle_file))
            self.assertTrue(
                TranscriptSegment.objects.filter(download=download).exists()
            )

    def test_video_with_explicit_match_is_recorded_as_filtered_tombstone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            media_file = output_root / "Channel" / "Explicit Video [bad-video].mp4"
            subtitle_file = media_file.with_suffix(".srt")

            class FakeYoutubeDLForExplicitVideo:
                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, _tb):
                    return False

                def extract_info(self, url, download):
                    media_file.parent.mkdir(parents=True, exist_ok=True)
                    media_file.write_text("raw video", encoding="utf-8")
                    return {
                        "id": "bad-video",
                        "title": "Explicit Video",
                        "webpage_url": "https://youtube.com/watch?v=bad-video",
                        "filepath": str(media_file),
                    }

                def prepare_filename(self, info):
                    return str(media_file)

            def fake_create_subtitles(*_args, **_kwargs):
                subtitle_file.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nbad words\n",
                    encoding="utf-8",
                )
                return subtitle_file

            match = SimpleNamespace(
                category="profanity", term="profanityfilter", sentence="bad words"
            )
            fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDLForExplicitVideo)
            job = Job.objects.create(
                profile_id="default",
                job_type="download_episode",
                payload={
                    "source_type": SourceType.YOUTUBE,
                    "source_name": "Channel",
                    "media_type": "video",
                    "item_uid": "bad-video",
                    "item_url": "https://youtube.com/watch?v=bad-video",
                    "media_url": "https://youtube.com/watch?v=bad-video",
                    "delete_explicit_content": True,
                    "subtitles": True,
                },
            )

            with (
                patch.dict(sys.modules, {"yt_dlp": fake_module}),
                patch(
                    "workers.handlers._download_output_root", return_value=output_root
                ),
                patch(
                    "workers.handlers._downloaded_media_requires_ffmpeg",
                    return_value=(False, "mp4"),
                ),
                patch(
                    "workers.handlers.create_subtitles",
                    side_effect=fake_create_subtitles,
                ),
                patch("workers.handlers.screen_transcript", return_value=match),
                patch("workers.handlers.log_filtered_deletion") as deletion_log,
            ):
                result = _download_with_yt_dlp(job, job.payload)

            self.assertIsNone(result)
            self.assertEqual(Download.objects.count(), 1)
            download = Download.objects.get()
            self.assertEqual(download.item_uid, "bad-video")
            self.assertEqual(download.item_url, "https://youtube.com/watch?v=bad-video")
            self.assertEqual(download.title, "Explicit Video")
            self.assertEqual(download.download_status, DownloadStatus.FILTERED)
            self.assertIsNone(download.file_path)
            self.assertIsNone(download.subtitle_path)
            self.assertFalse(media_file.exists())
            self.assertFalse(subtitle_file.exists())
            deletion_log.assert_called_once()

            download_job_count = Job.objects.filter(job_type="download_episode").count()
            source = SourceConfig.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                name="Channel",
                url="https://youtube.com/@channel",
                enabled=True,
                media_type="video",
                delete_explicit_content=True,
            )
            with patch(
                "workers.handlers._candidates_for_source",
                return_value=[
                    {
                        "item_uid": "bad-video",
                        "item_url": "https://youtube.com/watch?v=bad-video",
                        "media_url": "https://youtube.com/watch?v=bad-video",
                        "title": "Explicit Video",
                    }
                ],
            ):
                check_for_episodes(
                    Job.objects.create(
                        profile_id="default",
                        job_type="check_for_episodes",
                        payload={"source_id": source.id},
                    )
                )

            self.assertEqual(
                Job.objects.filter(job_type="download_episode").count(),
                download_job_count,
            )

    def test_transcode_media_updates_row_deletes_original_and_queues_transcript(
        self,
    ):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceType.PODCAST,
            name="Podcast",
            url="https://example.com/feed.xml",
            enabled=True,
            media_type="audio",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Path(tmpdir) / "episode.webm"
            original.write_text("downloaded", encoding="utf-8")
            download = Download.objects.create(
                profile_id="default",
                source_type=source.source_type,
                source_name=source.name,
                item_uid="episode-1",
                file_path=str(original),
                file_ext="webm",
                file_size_bytes=original.stat().st_size,
            )
            output = Path(tmpdir) / "episode.converted.mp3"

            def fake_run(command, check, capture_output, text):
                _ = (check, capture_output)
                self.assertIn("-codec:a", command)
                output.write_text("converted", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="ffmpeg done")

            job = Job.objects.create(
                profile_id="default",
                job_type="transcode_media",
                payload={"download_id": download.id},
            )
            with (
                patch("workers.handlers.subprocess.run", side_effect=fake_run) as run,
                patch("workers.handlers._publish_created_job") as publish,
            ):
                transcode_media(job)

            download.refresh_from_db()
            self.assertEqual(download.file_ext, "mp3")
            self.assertFalse(original.exists())
            self.assertEqual(download.file_path, str(output.resolve()))
            self.assertEqual(download.file_size_bytes, len("converted"))
            transcript_job = Job.objects.get(
                job_type="generate_transcript", payload__download_id=download.id
            )
            self.assertNotIn("original_file_path", transcript_job.payload)
            self.assertFalse(transcript_job.payload["delete_explicit_content"])
            run.assert_called_once()
            publish.assert_called_once()

    def test_transcode_media_propagates_explicit_filter_to_transcript_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Path(tmpdir) / "episode.webm"
            original.write_text("downloaded", encoding="utf-8")
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                item_uid="video-1",
                file_path=str(original),
                file_ext="webm",
                file_size_bytes=original.stat().st_size,
            )
            output = Path(tmpdir) / "episode.converted.mp4"

            def fake_run(command, check, capture_output, text):
                _ = (check, capture_output)
                output.write_text("converted", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="ffmpeg done")

            job = Job.objects.create(
                profile_id="default",
                job_type="transcode_media",
                payload={"download_id": download.id, "delete_explicit_content": True},
            )
            with (
                patch("workers.handlers.subprocess.run", side_effect=fake_run),
                patch("workers.handlers._publish_created_job"),
            ):
                transcode_media(job)

            transcript_job = Job.objects.get(
                job_type="generate_transcript", payload__download_id=download.id
            )
            self.assertTrue(transcript_job.payload["delete_explicit_content"])

    def test_generate_transcript_filters_explicit_recent_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "video.mp4"
            media.write_text("media", encoding="utf-8")
            subtitle = media.with_suffix(".srt")
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                title="Explicit video",
                item_uid="video-2",
                file_path=str(media),
                file_ext="mp4",
                file_size_bytes=media.stat().st_size,
                download_status="downloaded",
            )
            job = Job.objects.create(
                profile_id="default",
                job_type="generate_transcript",
                payload={
                    "download_id": download.id,
                    "subtitles": True,
                    "delete_explicit_content": True,
                },
            )
            match = SimpleNamespace(
                category="profanity", term="profanityfilter", sentence="bad words"
            )

            def fake_create_subtitles(*_args, **_kwargs):
                subtitle.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nbad words\n", encoding="utf-8"
                )
                return subtitle

            with (
                patch(
                    "workers.handlers.create_subtitles",
                    side_effect=fake_create_subtitles,
                ),
                patch("workers.handlers.screen_transcript", return_value=match),
                patch("workers.handlers.log_filtered_deletion") as deletion_log,
            ):
                generate_transcript(job)

            download.refresh_from_db()
            self.assertEqual(download.download_status, "filtered")
            self.assertFalse(media.exists())
            self.assertFalse(subtitle.exists())
            self.assertFalse(
                TranscriptSegment.objects.filter(download=download).exists()
            )
            deletion_log.assert_called_once()

    def test_generate_transcript_keeps_media_when_screening_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "video.mp4"
            media.write_text("media", encoding="utf-8")
            subtitle = media.with_suffix(".srt")
            download = Download.objects.create(
                profile_id="default",
                source_type=SourceType.YOUTUBE,
                source_name="Channel",
                title="Unchecked video",
                item_uid="video-3",
                file_path=str(media),
                file_ext="mp4",
                file_size_bytes=media.stat().st_size,
                download_status="downloaded",
            )
            job = Job.objects.create(
                profile_id="default",
                job_type="generate_transcript",
                payload={
                    "download_id": download.id,
                    "subtitles": True,
                    "delete_explicit_content": True,
                },
            )

            def fake_create_subtitles(*_args, **_kwargs):
                subtitle.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nunchecked words\n",
                    encoding="utf-8",
                )
                return subtitle

            with (
                patch(
                    "workers.handlers.create_subtitles",
                    side_effect=fake_create_subtitles,
                ),
                patch(
                    "workers.handlers.screen_transcript",
                    side_effect=RuntimeError("profanityfilter unavailable"),
                ),
                patch("workers.handlers.log_filtered_deletion") as deletion_log,
            ):
                generate_transcript(job)

            download.refresh_from_db()
            self.assertEqual(download.download_status, "downloaded")
            self.assertTrue(media.exists())
            self.assertTrue(subtitle.exists())
            self.assertTrue(
                TranscriptSegment.objects.filter(download=download).exists()
            )
            deletion_log.assert_not_called()


class QueueRoutingTests(unittest.TestCase):
    def test_ffmpeg_source_cleanup_logs_and_deletes_only_original_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = Path(tmpdir) / "episode.webm"
            target = Path(tmpdir) / "episode.converted.mp4"
            missing = Path(tmpdir) / "already-removed.webm"
            original.write_text("original", encoding="utf-8")
            target.write_text("converted", encoding="utf-8")

            with self.assertLogs("getoffline", level="INFO") as logs:
                deleted = _delete_ffmpeg_source_files(
                    [original, target, missing], target
                )

            self.assertEqual(deleted, [original.resolve()])
            self.assertFalse(original.exists())
            self.assertTrue(target.exists())
            log_output = "\n".join(logs.output)
            self.assertIn("FFmpeg source cleanup starting", log_output)
            self.assertIn("reason=matches-target", log_output)
            self.assertIn("reason=missing", log_output)
            self.assertIn("FFmpeg source cleanup finished", log_output)

    def test_h264_video_args_use_smaller_jellyfin_friendly_crf_profile(self):
        def fake_profile_setting(_profile_id, key, default):
            values = {"video_codec": "h264"}
            return values.get(key, default)

        with patch(
            "workers.handlers._profile_setting", side_effect=fake_profile_setting
        ):
            args = _ffmpeg_video_args("default", "mp4")

        self.assertIn("-c:v", args)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-preset") + 1], "fast")
        self.assertEqual(args[args.index("-crf") + 1], "25")
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-b:a") + 1], "128k")
        self.assertEqual(args[args.index("-ac") + 1], "2")
        self.assertEqual(args[args.index("-ar") + 1], "48000")
        self.assertIn("-threads", args)
        self.assertEqual(args[args.index("-threads") + 1], "1")
        self.assertIn("+faststart", args)

    def test_jellyfin_h264_setting_transcodes_single_file_videos(self):
        def fake_profile_setting(_profile_id, key, default):
            values = {"video_format": "mp4", "video_codec": "h264"}
            return values.get(key, default)

        with patch(
            "workers.handlers._profile_setting", side_effect=fake_profile_setting
        ):
            webm_requires, webm_target = _downloaded_media_requires_ffmpeg(
                profile_id="default",
                media_kind="video",
                current_ext="webm",
                input_count=1,
            )
            mp4_requires, mp4_target = _downloaded_media_requires_ffmpeg(
                profile_id="default",
                media_kind="video",
                current_ext="mp4",
                input_count=1,
            )

        self.assertTrue(webm_requires)
        self.assertEqual(webm_target, "mp4")
        self.assertTrue(mp4_requires)
        self.assertEqual(mp4_target, "mp4")

    def test_copy_video_setting_only_remuxes_when_container_differs(self):
        def fake_profile_setting(_profile_id, key, default):
            values = {"video_format": "mp4", "video_codec": "copy"}
            return values.get(key, default)

        with patch(
            "workers.handlers._profile_setting", side_effect=fake_profile_setting
        ):
            webm_requires, webm_target = _downloaded_media_requires_ffmpeg(
                profile_id="default",
                media_kind="video",
                current_ext="webm",
                input_count=1,
            )
            mp4_requires, mp4_target = _downloaded_media_requires_ffmpeg(
                profile_id="default",
                media_kind="video",
                current_ext="mp4",
                input_count=1,
            )

        self.assertTrue(webm_requires)
        self.assertEqual(webm_target, "mp4")
        self.assertFalse(mp4_requires)
        self.assertEqual(mp4_target, "mp4")

    def test_priority_rules_match_user_initiated_and_fresh_work(self):
        self.assertEqual(
            job_priority(
                {"job_type": "download_single", "payload": {"manual_enqueue": True}}
            ),
            10,
        )
        self.assertEqual(
            job_priority(
                {"job_type": "download_episode", "payload": {"redownload": True}}
            ),
            9,
        )
        self.assertEqual(
            job_priority({"job_type": "download_episode", "payload": {}}), 5
        )
        self.assertEqual(
            job_priority(
                {
                    "job_type": "generate_transcript",
                    "payload": {"download_id": 1, "source_type": "podcast"},
                }
            ),
            8,
        )
        self.assertEqual(
            job_priority(
                {
                    "job_type": "generate_transcript",
                    "payload": {"download_id": 1, "source_type": "youtube"},
                }
            ),
            7,
        )
        self.assertEqual(
            job_priority(
                {
                    "job_type": "generate_transcript",
                    "payload": {"download_id": 1, "startup_missing_subtitle": True},
                }
            ),
            2,
        )

    def test_priority_queues_are_declared_with_max_priority(self):
        self.assertEqual(
            queue_arguments(YOUTUBE_DOWNLOAD_QUEUE), {"x-max-priority": 10}
        )
        self.assertEqual(
            queue_arguments(PODCAST_DOWNLOAD_QUEUE), {"x-max-priority": 10}
        )
        self.assertEqual(queue_arguments(TRANSCRIPT_QUEUE), {"x-max-priority": 10})

    def test_download_jobs_route_to_source_specific_download_queues(self):
        self.assertEqual(queue_name("update_downloads"), "getoffline.jobs.updates")
        self.assertEqual(queue_name("check_for_episodes"), "getoffline.jobs.updates")
        self.assertEqual(
            queue_name("download_single"), "getoffline.jobs.downloads.youtube"
        )
        self.assertEqual(
            queue_name("download_single", {"source_type": "youtube"}),
            "getoffline.jobs.downloads.youtube",
        )
        self.assertEqual(
            queue_name("download_episode", {"source_type": "podcast"}),
            "getoffline.jobs.downloads.podcast",
        )
        self.assertEqual(
            queue_name("download_episode", {"source_type": "youtube"}),
            "getoffline.jobs.downloads.youtube",
        )
        self.assertEqual(
            queue_name("transcode_media", {"source_type": "youtube"}),
            "getoffline.jobs.ffmpeg",
        )

    def test_non_download_jobs_get_separate_queues(self):
        self.assertEqual(queue_name("transcode_media"), "getoffline.jobs.ffmpeg")
        self.assertEqual(
            queue_name("generate_transcript"), "getoffline.jobs.transcripts"
        )


class WorkerRabbitMQConnectionTests(unittest.TestCase):
    def test_worker_rabbitmq_parameters_disables_default_heartbeat(self):
        with patch.object(
            runner.settings,
            "RABBITMQ_URL",
            "amqp://guest:guest@rabbitmq:5672/%2F",
        ):
            params = runner.worker_rabbitmq_parameters()

        self.assertEqual(params.heartbeat, 0)

    def test_worker_rabbitmq_parameters_respects_url_heartbeat(self):
        with patch.object(
            runner.settings,
            "RABBITMQ_URL",
            "amqp://guest:guest@rabbitmq:5672/%2F?heartbeat=300",
        ):
            params = runner.worker_rabbitmq_parameters()

        self.assertEqual(params.heartbeat, 300)

    def test_close_connection_if_open_skips_already_closed_connection(self):
        connection = SimpleNamespace(is_closed=True, close=lambda: self.fail("closed"))

        runner.close_connection_if_open(connection)


if __name__ == "__main__":
    unittest.main()
