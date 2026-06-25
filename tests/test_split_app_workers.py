import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

try:
    import django  # noqa: E402
    from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
    from django.test import Client, TestCase, override_settings  # noqa: E402
except (
    ModuleNotFoundError
):  # pragma: no cover - dependency may be absent outside project venv
    django = None
    TestCase = unittest.TestCase

    def override_settings(**_kwargs):
        return lambda cls: cls


if django is not None:
    django.setup()

from app.queue import job_priority  # noqa: E402
from app.routing import (
    CLEANUP_QUEUE,
    PODCAST_DOWNLOAD_QUEUE,
    TRANSCRIPT_QUEUE,
    YOUTUBE_DOWNLOAD_QUEUE,
    queue_arguments,
    queue_name,
)  # noqa: E402

if django is not None:
    from models.jobs import claim_job, create_job, finish_job  # noqa: E402
    from models.models import (
        Download,
        Job,
        MediaSummary,
        ScheduledJob,
        SourceConfig,
        ProfileConfigValue,
        TranscriptSegment,
    )  # noqa: E402
    from app.views import (
        _queue_counts,
        _queue_missing_summary_batch,
        _sync_update_downloads_schedule,
        _write_manual_upload,
    )  # noqa: E402
    from models.scheduler import enqueue_due_scheduled_jobs  # noqa: E402
    from workers.handlers import (
        check_for_episodes,
        retention_cleanup,
        transcode_media,
        _idempotency_key,
        _is_expected_ytdlp_download_error,
        _youtube_candidates,
    )  # noqa: E402
    from workers.runner import enqueue_missing_summary_jobs  # noqa: E402


@override_settings(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
)
class SharedDjangoModelTests(TestCase):

    @unittest.skipIf(django is None, "Django is not installed")
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_unexpected_ytdlp_errors_are_not_suppressed(self):
        class DownloadError(Exception):
            pass

        self.assertFalse(
            _is_expected_ytdlp_download_error(DownloadError("network fail"))
        )
        self.assertFalse(
            _is_expected_ytdlp_download_error(TypeError("video unavailable"))
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_create_claim_and_finish_job(self):
        job = create_job(
            profile_id="default",
            job_type="transfer_media",
            payload={"source": "test"},
            idempotency_key="transfer_media:default:test",
        )
        claimed = claim_job(job.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, Job.STATUS_RUNNING)
        finish_job(claimed, status=Job.STATUS_SUCCEEDED)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, Job.STATUS_SUCCEEDED)
        self.assertEqual(claimed.payload, {"source": "test"})

    @unittest.skipIf(django is None, "Django is not installed")
    def test_idempotency_reuses_queued_job(self):
        first = create_job(
            profile_id="default",
            job_type="summarize_missing",
            idempotency_key="summary:default",
        )
        second = create_job(
            profile_id="default",
            job_type="summarize_missing",
            idempotency_key="summary:default",
        )
        self.assertEqual(first.id, second.id)

    @unittest.skipIf(django is None, "Django is not installed")
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_manual_upload_endpoint_queues_transcript_pipeline(self):
        client = Client()
        from django.contrib.auth.models import User

        User.objects.create_user(username="default", password="pass")
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_scheduler_enqueues_due_database_configured_job(self):
        from datetime import timedelta
        from django.utils import timezone

        due_at = timezone.now() - timedelta(minutes=1)
        schedule = ScheduledJob.objects.create(
            profile_id="default",
            job_type="transfer_media",
            interval_seconds=3600,
            payload={"source": "test-scheduler"},
            idempotency_key_template="scheduled:${job_type}:${profile_id}:${due_hour}",
            next_run_at=due_at,
        )

        with patch("models.scheduler.publish_job") as publish:
            job_ids = enqueue_due_scheduled_jobs(now=timezone.now())

        self.assertEqual(len(job_ids), 1)
        job = Job.objects.get(id=job_ids[0])
        self.assertEqual(job.job_type, "transfer_media")
        self.assertEqual(job.payload["source"], "test-scheduler")
        self.assertEqual(job.payload["scheduled_job_id"], schedule.id)
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, due_at)
        publish.assert_called_once_with(
            {
                "job_id": job.id,
                "job_type": "transfer_media",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_auto_update_setting_creates_enabled_update_schedule(self):
        from django.utils import timezone

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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_auto_update_setting_zero_disables_update_schedule(self):
        from django.utils import timezone

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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_save_config_syncs_auto_update_schedule_for_logged_in_user(self):
        client = Client()
        from django.contrib.auth.models import User

        User.objects.create_user(username="alice", password="pass")
        self.assertTrue(client.login(username="alice", password="pass"))

        response = client.post("/settings/save/", {"config__auto_update_minutes": "7"})

        self.assertEqual(response.status_code, 302)
        schedule = ScheduledJob.objects.get(
            profile_id="alice", job_type="update_downloads"
        )
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.interval_seconds, 420)

    @unittest.skipIf(django is None, "Django is not installed")
    def test_retention_cleanup_deletes_expired_non_favorite_content(self):
        from datetime import timedelta
        from django.utils import timezone

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
                source_type=SourceConfig.SOURCE_YOUTUBE,
                source_name="Channel",
                title="Expired",
                file_path=str(expired),
                download_status="downloaded",
                completed_at=old_time,
            )
            favorite_download = Download.objects.create(
                profile_id="default",
                source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_missing_summary_batch_is_queued_once_for_downloaded_subtitle_rows(self):
        Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="missing-summary",
            title="Missing Summary",
            file_path="/tmp/media.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="/tmp/media.srt",
        )

        with patch("app.views.publish_job") as publish:
            queued = _queue_missing_summary_batch(
                "default", reason="test_missing_summary"
            )
            queued_again = _queue_missing_summary_batch(
                "default", reason="test_missing_summary"
            )

        self.assertTrue(queued)
        self.assertFalse(queued_again)
        job = Job.objects.get(
            job_type="summarize_missing",
            idempotency_key="summarize_missing:default:auto",
        )
        self.assertEqual(job.payload["reason"], "test_missing_summary")
        self.assertTrue(job.payload["auto_enqueue"])
        publish.assert_called_once_with(
            {
                "job_id": job.id,
                "job_type": "summarize_missing",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_startup_missing_summary_scan_enqueues_transcript_backed_missing_and_blank_summaries(
        self,
    ):
        missing = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="startup-missing-summary",
            title="Startup Missing Summary",
            file_path="/tmp/startup-missing.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="/tmp/startup-missing.srt",
        )
        blank = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="startup-blank-summary",
            title="Startup Blank Summary",
            file_path="/tmp/startup-blank.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="",
        )
        TranscriptSegment.objects.create(
            download=blank,
            subtitle_path="/tmp/startup-blank.srt",
            start_seconds=0.0,
            text="Transcript text",
        )
        MediaSummary.objects.create(download=blank, summary_text="", model_name="test")
        no_transcript = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="startup-no-transcript",
            title="Startup No Transcript",
            file_path="/tmp/startup-no-transcript.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="",
        )
        with_summary = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="startup-with-summary",
            title="Startup With Summary",
            file_path="/tmp/startup-with-summary.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="/tmp/startup-with-summary.srt",
        )
        MediaSummary.objects.create(
            download=with_summary, summary_text="Already summarized", model_name="test"
        )

        with patch("workers.runner.publish_job") as publish:
            enqueued = enqueue_missing_summary_jobs()

        self.assertEqual(enqueued, 2)
        self.assertTrue(
            Job.objects.filter(
                job_type="generate_summary", payload__download_id=missing.id
            ).exists()
        )
        self.assertTrue(
            Job.objects.filter(
                job_type="generate_summary", payload__download_id=blank.id
            ).exists()
        )
        self.assertFalse(
            Job.objects.filter(
                job_type="generate_summary", payload__download_id=no_transcript.id
            ).exists()
        )
        self.assertFalse(
            Job.objects.filter(
                job_type="generate_summary", payload__download_id=with_summary.id
            ).exists()
        )
        self.assertEqual(publish.call_count, 2)

    @unittest.skipIf(django is None, "Django is not installed")
    def test_missing_summary_batch_skips_rows_without_subtitles_or_with_summary(self):
        Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="no-subtitles",
            title="No Subtitles",
            file_path="/tmp/no-subtitles.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="",
        )
        with_summary = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            source_name="Channel",
            item_uid="with-summary",
            title="With Summary",
            file_path="/tmp/with-summary.mp4",
            file_ext="mp4",
            download_status="downloaded",
            subtitle_path="/tmp/with-summary.srt",
        )
        MediaSummary.objects.create(
            download=with_summary, summary_text="Already summarized", model_name="test"
        )

        with patch("app.views.publish_job") as publish:
            queued = _queue_missing_summary_batch(
                "default", reason="test_no_candidates"
            )

        self.assertFalse(queued)
        self.assertFalse(Job.objects.filter(job_type="summarize_missing").exists())
        publish.assert_not_called()

    @unittest.skipIf(django is None, "Django is not installed")
    def test_queue_counts_groups_active_jobs_by_worker_queue(self):
        Job.objects.create(
            profile_id="default", job_type="update_downloads", status=Job.STATUS_QUEUED
        )
        Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=Job.STATUS_RUNNING,
        )
        Job.objects.create(
            profile_id="default", job_type="download_episode", status=Job.STATUS_QUEUED
        )
        Job.objects.create(
            profile_id="default",
            job_type="generate_summary",
            status=Job.STATUS_SUCCEEDED,
        )
        Job.objects.create(
            profile_id="other", job_type="download_episode", status=Job.STATUS_QUEUED
        )

        counts = {row["label"]: row for row in _queue_counts("default")}

        self.assertEqual(counts["Updates"]["queued"], 1)
        self.assertEqual(counts["Updates"]["running"], 1)
        self.assertEqual(counts["Updates"]["total"], 2)
        self.assertEqual(counts["Downloads"]["queued"], 1)
        self.assertEqual(counts["Downloads"]["running"], 0)
        self.assertEqual(counts["Summaries"]["total"], 0)
        self.assertEqual(queue_name("retention_cleanup"), CLEANUP_QUEUE)

    @unittest.skipIf(django is None, "Django is not installed")
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
                source_type=SourceConfig.SOURCE_PODCAST,
                source_name="Podcast",
                item_uid="episode-1",
                title="Podcast Episode",
                file_path=str(media),
                file_ext="mp3",
                download_status="downloaded",
                subtitle_path="",
            )

            with patch("app.views.publish_job"):
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn('data-has-subtitles="1"', body)
        self.assertIn(f"/subtitle/{download.id}/", body)

    @unittest.skipIf(django is None, "Django is not installed")
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
                source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
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
                source_type=SourceConfig.SOURCE_YOUTUBE,
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
                response = client.get(f"/player/{download.id}/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn('id="subtitle-track"', body)
        self.assertIn(f"/media/{download.id}/#t=42.500", body)
        self.assertIn("const periodicProgressSeconds = 5;", body)
        self.assertIn("media?.addEventListener('timeupdate'", body)
        self.assertIn("media?.addEventListener('pause'", body)
        self.assertIn("media?.addEventListener('seeked'", body)
        self.assertIn("media?.addEventListener('ended'", body)
        self.assertIn("window.addEventListener('pagehide'", body)
        self.assertIn("navigator.sendBeacon(form.action, body)", body)

    @unittest.skipIf(django is None, "Django is not installed")
    def test_django_player_position_endpoint_persists_resume_and_completion(self):
        client = Client()
        download = Download.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_batch_update_queues_transcript_summary_refresh(self):
        client = Client()
        from django.contrib.auth.models import User

        User.objects.create_user(username="default", password="pass")
        self.assertTrue(client.login(username="default", password="pass"))
        download = Download.objects.create(
            profile_id="default",
            source_type="youtube",
            source_name="Test Channel",
            item_uid="video-refresh",
            title="Refresh Me",
            file_path="/tmp/refresh-me.mp4",
            file_ext="mp4",
            download_status="downloaded",
        )
        MediaSummary.objects.create(download=download, summary_text="old summary")

        with patch("app.views.publish_job") as publish:
            response = client.post(
                "/batch-update/",
                {"ids": [str(download.id)], "batch_action": "transcript-summary"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(MediaSummary.objects.filter(download=download).exists())
        job = Job.objects.get(
            job_type="generate_transcript", payload__download_id=download.id
        )
        self.assertTrue(job.payload["replace_existing"])
        self.assertEqual(job.payload["media_type"], "video")
        publish.assert_called_once_with(
            {
                "job_id": job.id,
                "job_type": "generate_transcript",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_batch_update_transcript_refresh_supersedes_active_job(self):
        client = Client()
        from django.contrib.auth.models import User

        User.objects.create_user(username="default", password="pass")
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
            status=Job.STATUS_RUNNING,
            payload={"download_id": download.id},
            idempotency_key=f"generate_transcript:default:{download.id}",
        )

        with patch("app.views.publish_job") as publish:
            response = client.post(
                "/batch-update/",
                {"ids": [str(download.id)], "batch_action": "transcript-summary"},
            )

        self.assertEqual(response.status_code, 302)
        active_job.refresh_from_db()
        self.assertEqual(active_job.status, Job.STATUS_FAILED)
        self.assertIn("Superseded", active_job.error_message)
        replacement = Job.objects.get(
            job_type="generate_transcript",
            status=Job.STATUS_QUEUED,
            payload__download_id=download.id,
        )
        self.assertNotEqual(replacement.id, active_job.id)
        publish.assert_called_once_with(
            {
                "job_id": replacement.id,
                "job_type": "generate_transcript",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_batch_update_queues_summary_refresh(self):
        client = Client()
        from django.contrib.auth.models import User

        User.objects.create_user(username="default", password="pass")
        self.assertTrue(client.login(username="default", password="pass"))
        download = Download.objects.create(
            profile_id="default",
            source_type="podcast",
            source_name="Test Feed",
            item_uid="episode-refresh",
            title="Refresh Summary",
            file_path="/tmp/refresh-summary.mp3",
            file_ext="mp3",
            download_status="downloaded",
        )

        with patch("app.views.publish_job") as publish:
            response = client.post(
                "/batch-update/",
                {"ids": [str(download.id)], "batch_action": "summary"},
            )

        self.assertEqual(response.status_code, 302)
        job = Job.objects.get(
            job_type="generate_summary", payload__download_id=download.id
        )
        self.assertTrue(job.payload["replace_existing"])
        publish.assert_called_once_with(
            {
                "job_id": job.id,
                "job_type": "generate_summary",
                "profile_id": "default",
                "attempt": 1,
            }
        )

    @unittest.skipIf(django is None, "Django is not installed")
    def test_episode_checker_honors_source_max_downloads(self):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
            name="Test Channel",
            url="https://www.youtube.com/@example/videos",
            enabled=True,
            max_downloads=1,
        )
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=Job.STATUS_QUEUED,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_episode_checker_republishes_existing_queued_download_job(self):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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
                "source_type": SourceConfig.SOURCE_YOUTUBE,
                "item_uid": "video-1",
            },
            idempotency_key=_idempotency_key(
                "download_episode", "default", source.id, "video-1"
            ),
        )
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=Job.STATUS_QUEUED,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_episode_checker_resets_and_republishes_stale_running_download_job(self):
        from datetime import timedelta
        from django.utils import timezone

        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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
                "source_type": SourceConfig.SOURCE_YOUTUBE,
                "item_uid": "video-1",
            },
            idempotency_key=_idempotency_key(
                "download_episode", "default", source.id, "video-1"
            ),
        )
        stale_started_at = timezone.now() - timedelta(hours=7)
        Job.objects.filter(pk=existing.id).update(
            status=Job.STATUS_RUNNING,
            started_at=stale_started_at,
            updated_at=stale_started_at,
        )
        existing.refresh_from_db()
        job = Job.objects.create(
            profile_id="default",
            job_type="check_for_episodes",
            status=Job.STATUS_QUEUED,
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
        self.assertEqual(existing.status, Job.STATUS_QUEUED)
        self.assertIsNone(existing.started_at)
        publish.assert_called_once_with(existing)

    @unittest.skipIf(django is None, "Django is not installed")
    def test_youtube_candidates_drill_into_channel_videos_tab(self):
        source = SourceConfig(
            id=4,
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_youtube_candidates_skip_live_titled_flat_entries_by_default(self):
        source = SourceConfig(
            id=5,
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_youtube_candidates_allow_live_titled_flat_entries_when_enabled(self):
        source = SourceConfig(
            id=6,
            profile_id="default",
            source_type=SourceConfig.SOURCE_YOUTUBE,
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

    @unittest.skipIf(django is None, "Django is not installed")
    def test_transcode_media_updates_row_defers_original_deletion_and_queues_transcript(
        self,
    ):
        source = SourceConfig.objects.create(
            profile_id="default",
            source_type=SourceConfig.SOURCE_PODCAST,
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
        self.assertTrue(original.exists())
        self.assertEqual(download.file_size_bytes, len("converted"))
        transcript_job = Job.objects.get(
            job_type="generate_transcript", payload__download_id=download.id
        )
        self.assertEqual(
            transcript_job.payload["original_file_path"], str(original.resolve())
        )
        run.assert_called_once()
        publish.assert_called_once()


class QueueRoutingTests(unittest.TestCase):
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
            YOUTUBE_DOWNLOAD_QUEUE,
        )

    def test_non_download_jobs_get_separate_queues(self):
        self.assertEqual(queue_name("transfer_media"), "getoffline.jobs.transfer")
        self.assertEqual(
            queue_name("generate_transcript"), "getoffline.jobs.transcripts"
        )
        self.assertEqual(queue_name("summarize_missing"), "getoffline.jobs.summaries")
        self.assertEqual(queue_name("generate_summary"), "getoffline.jobs.summaries")


@unittest.skipIf(django is None, "Django is not installed")
class WorkerRabbitMQConnectionTests(unittest.TestCase):
    def test_worker_rabbitmq_parameters_disables_default_heartbeat(self):
        from workers import runner

        with patch.object(
            runner.settings,
            "RABBITMQ_URL",
            "amqp://guest:guest@rabbitmq:5672/%2F",
        ):
            params = runner.worker_rabbitmq_parameters()

        self.assertEqual(params.heartbeat, 0)

    def test_worker_rabbitmq_parameters_respects_url_heartbeat(self):
        from workers import runner

        with patch.object(
            runner.settings,
            "RABBITMQ_URL",
            "amqp://guest:guest@rabbitmq:5672/%2F?heartbeat=300",
        ):
            params = runner.worker_rabbitmq_parameters()

        self.assertEqual(params.heartbeat, 300)

    def test_close_connection_if_open_skips_already_closed_connection(self):
        from workers import runner

        connection = SimpleNamespace(is_closed=True, close=lambda: self.fail("closed"))

        runner.close_connection_if_open(connection)


if __name__ == "__main__":
    unittest.main()
