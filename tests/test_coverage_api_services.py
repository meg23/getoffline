from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-api-coverage.log")

import django
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.utils import timezone

django.setup()

from api.services import dashboard_actions, profiles, settings
from models.domain import DownloadStatus, JobStatus, SourceType
from models.models import (
    AppConfigValue,
    Download,
    Job,
    ProfileConfigValue,
    ProfileDownloadSettings,
    ScheduledJob,
    SourceConfig,
    TranscriptSegment,
)


def ensure_test_schema() -> None:
    from django.db import connection

    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        for model in apps.get_models():
            if model._meta.db_table not in existing_tables:
                schema_editor.create_model(model)
                existing_tables.add(model._meta.db_table)


class ApiCoverageDatabaseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_test_schema()
        super().setUpClass()

    def setUp(self):
        super().setUp()
        for model in reversed(apps.get_models()):
            model.objects.all().delete()
        self.factory = RequestFactory()
        self.user = SimpleNamespace(
            is_authenticated=True,
            get_username=lambda: "alice",
        )

    def request(self, method="get", path="/", data=None):
        request = getattr(self.factory, method)(path, data or {})
        request.user = self.user
        return request

    def test_profile_library_and_path_helpers_cover_status_branches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_bytes(b"audio")
            subtitle = root / "episode.srt"
            subtitle.write_text("captions", encoding="utf-8")
            ProfileConfigValue.objects.create(
                profile_id="alice", key="output_root", value=str(root.resolve())
            )
            AppConfigValue.objects.create(key="audio_format", value="opus")
            ProfileDownloadSettings.objects.create(
                profile_id="alice", youtube_cookie_text="cookies"
            )

            values = settings.profile_settings("alice")
            self.assertEqual(values["audio_format"], "opus")
            self.assertEqual(values["output_root"], str(root.resolve()))
            self.assertEqual(settings.profile_output_root("alice"), root.resolve())
            self.assertEqual(profiles.profile_id_for_request(self.request()), "alice")
            anonymous = self.request()
            anonymous.user = SimpleNamespace(is_authenticated=False)
            self.assertEqual(profiles.profile_id_for_request(anonymous), "default")

            item = Download(
                id=1,
                profile_id="alice",
                source_type=SourceType.PODCAST,
                source_name="Feed",
                title="Episode",
                file_path=str(media),
                file_ext="mp3",
                file_size_bytes=1024,
                subtitle_path=str(subtitle),
                download_status=DownloadStatus.DOWNLOADED,
                last_position_seconds=10,
            )
            dashboard_actions._decorate_download(item)
            self.assertEqual(item.status_label, "STARTED")
            self.assertEqual(item.display_kind, "audio")
            self.assertTrue(item.has_subtitles)
            item.played = True
            dashboard_actions._decorate_download(item)
            self.assertEqual(item.status_label, "PLAYED")
            item.download_status = DownloadStatus.MISSING
            dashboard_actions._decorate_download(item)
            self.assertEqual(item.status_label, "MISSING")
            item.download_status = DownloadStatus.RETENTION_DELETED
            dashboard_actions._decorate_download(item)
            self.assertEqual(item.status_label, "REMOVED")

            self.assertEqual(dashboard_actions._resolve_media_path(item), media.resolve())
            self.assertEqual(
                dashboard_actions._resolve_subtitle_path(item), subtitle.resolve()
            )
            with self.assertRaises(Http404):
                dashboard_actions._safe_path(str(root / "missing.mp3"))
            self.assertIn("WEBVTT", dashboard_actions._srt_to_vtt("\ufeff1\n00:00:00,000 --> 00:00:01,000\ntext"))

    def test_library_page_and_active_pipeline_helpers(self):
        now = timezone.now()
        Download.objects.create(
            profile_id="alice",
            source_type=SourceType.PODCAST,
            source_name="Feed",
            title="Played",
            file_ext="mp3",
            download_status=DownloadStatus.DOWNLOADED,
            played=True,
            favorite=True,
            last_seen_at=now,
            total_listened_seconds=120,
        )
        current = Download.objects.create(
            profile_id="alice",
            source_type=SourceType.YOUTUBE,
            source_name="Channel",
            title="Current video",
            file_ext="mp4",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=now - timedelta(seconds=1),
        )
        transcript_job = Job.objects.create(
            profile_id="alice",
            job_type="generate_transcript",
            status=JobStatus.RUNNING,
            payload={"download_id": current.id, "active_stage": "transcript_generation"},
            updated_at=now,
        )
        request = self.request("get", "/?filter=all")
        page = dashboard_actions._library_page_data(request)
        context = dashboard_actions._library_context(page)
        self.assertEqual(len(page.downloads), 2)
        self.assertEqual(context["library_filter_mode"], "all")
        self.assertEqual(context["stats"]["favorites"], 1)
        self.assertEqual(dashboard_actions._job_display_title(transcript_job), "Current video")
        self.assertEqual(dashboard_actions._job_stage(transcript_job).name, "transcript_generation")
        self.assertTrue(dashboard_actions._job_still_needs_work(transcript_job))
        self.assertEqual(len(dashboard_actions._active_pipeline_items("alice")), 1)

        current.subtitle_path = "episode.srt"
        current.save(update_fields=["subtitle_path"])
        self.assertFalse(dashboard_actions._job_still_needs_work(transcript_job))
        self.assertEqual(dashboard_actions._job_stage(Job(job_type="download_single")).name, "downloading")
        self.assertEqual(dashboard_actions._job_stage(Job(job_type="other_job")).name, "queued")
        with patch.dict(os.environ, {"GETOFFLINE_ACTIVE_PIPELINE_STALE_SECONDS": "0"}):
            self.assertIsNone(dashboard_actions._active_pipeline_cutoff())
            self.assertTrue(dashboard_actions._job_is_fresh(transcript_job))
        with patch.dict(os.environ, {"GETOFFLINE_ACTIVE_PIPELINE_STALE_SECONDS": "bad"}):
            self.assertIsNone(dashboard_actions._active_pipeline_cutoff())

    def test_source_forms_validation_and_schedule_transitions(self):
        valid_request = self.request(
            "post",
            "/sources/add/",
            {
                "name": "Channel",
                "url": "https://youtube.com/channel",
                "media_type": "video",
                "enabled": "1",
                "subtitles": "1",
                "subtitle_offset_seconds": "1.25",
                "max_downloads": "3",
                "include_shorts": "1",
            },
        )
        form = dashboard_actions._source_form_data(valid_request, SourceType.YOUTUBE)
        self.assertEqual(dashboard_actions._source_form_errors(valid_request, form, SourceType.YOUTUBE), [])
        self.assertTrue(form.include_shorts)
        invalid_request = self.request(
            "post",
            "/sources/add/",
            {
                "name": "",
                "url": "not-a-url",
                "media_type": "invalid",
                "subtitle_offset_seconds": "nan",
                "max_downloads": "0",
            },
        )
        invalid_form = dashboard_actions._source_form_data(invalid_request, SourceType.YOUTUBE)
        errors = dashboard_actions._source_form_errors(invalid_request, invalid_form, SourceType.YOUTUBE)
        self.assertGreaterEqual(len(errors), 5)
        self.assertIn("media_type is invalid", errors)
        self.assertEqual(dashboard_actions._invalid_config_keys(invalid_request), [])
        invalid_config = self.request(
                "post", "/settings/save/", {"config__max_downloads": "0", "config__audio_format": "wav"}
        )
        self.assertEqual(
            dashboard_actions._invalid_config_keys(invalid_config),
            ["max_downloads", "audio_format"],
        )

        source = SourceConfig.objects.create(
            profile_id="alice",
            source_type=SourceType.YOUTUBE,
            name="Old",
            url="https://example.com/old",
            media_type="audio",
        )
        dashboard_actions._apply_source_form_data(
            source, form, now=timezone.now(), include_enabled=False
        )
        self.assertEqual(source.name, "Channel")
        self.assertNotIn("enabled", dashboard_actions._source_update_fields(include_enabled=False))
        self.assertTrue(dashboard_actions._checked({"flag": "yes"}, "flag"))
        self.assertTrue(dashboard_actions._posted_bool(valid_request, "enabled"))

        dashboard_actions._sync_update_downloads_schedule("alice", "5")
        schedule = ScheduledJob.objects.get(profile_id="alice")
        self.assertEqual(schedule.interval_seconds, 300)
        dashboard_actions._sync_update_downloads_schedule("alice", "10")
        schedule.refresh_from_db()
        self.assertEqual(schedule.interval_seconds, 600)
        dashboard_actions._sync_update_downloads_schedule("alice", "0")
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        dashboard_actions._sync_update_downloads_schedule("alice", "invalid")

    def test_manual_upload_and_worker_status_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ProfileConfigValue.objects.create(
                profile_id="alice", key="output_root", value=tmpdir
            )
            uploaded = SimpleUploadedFile("clip..mp4", b"video", content_type="video/mp4")
            result = dashboard_actions._write_manual_upload("alice", uploaded)
            self.assertTrue(result.path.exists())
            self.assertEqual(result.download.source_type, "manual")
            with self.assertRaises(ValueError):
                dashboard_actions._write_manual_upload(
                    "alice", SimpleUploadedFile("bad.txt", b"bad")
                )
            with self.assertRaises(ValueError):
                dashboard_actions._write_manual_upload(
                    "alice", SimpleUploadedFile("empty.mp3", b"")
                )

        with patch("api.services.dashboard_actions.publish_job") as publish:
            request = self.request("post", "/jobs/enqueue/", {"job_type": "update_downloads"})
            request.headers = {"accept": "application/json"}
            response = dashboard_actions.enqueue_job(request)
        self.assertEqual(response.status_code, 200)
        publish.assert_called_once()
        job = Job.objects.get(job_type="update_downloads")
        status_request = self.request("get", f"/jobs/status/?job_id={job.id}")
        status_request.GET = {"job_id": str(job.id)}
        pending = dashboard_actions.worker_message_status(status_request)
        self.assertFalse(json.loads(pending.content)["finished"])
        job.status = JobStatus.SUCCEEDED
        job.save(update_fields=["status"])
        finished = dashboard_actions.worker_message_status(status_request)
        self.assertTrue(json.loads(finished.content)["finished"])
        missing_token = self.request("get", "/jobs/status/")
        missing_token.GET = {}
        self.assertEqual(dashboard_actions.worker_message_status(missing_token).status_code, 400)

    def test_playback_transcript_metadata_and_batch_actions(self):
        item = Download.objects.create(
            profile_id="alice",
            source_type=SourceType.PODCAST,
            source_name="Feed",
            title="Old title",
            file_path="/tmp/does-not-exist.mp3",
            file_ext="mp3",
            download_status=DownloadStatus.DOWNLOADED,
        )
        update = dashboard_actions._playback_update_from_request(
            self.request("post", "/position/", {"position_seconds": "20", "reason": "ended"}),
            item,
        )
        self.assertIsNotNone(update)
        fields = dashboard_actions._apply_playback_update(item, update, now=timezone.now())
        self.assertIn("played", fields)
        self.assertIsNone(
            dashboard_actions._playback_update_from_request(
                self.request("post", "/position/", {"position_seconds": "bad"}), item
            )
        )
        dashboard_actions._delete_download_media_file(item)
        self.assertIn("last_seen_at", dashboard_actions._apply_playback_update(item, update, now=timezone.now()))

        TranscriptSegment.objects.create(
            download=item, subtitle_path="x.srt", start_seconds=12.5, text="Important phrase"
        )
        search_request = self.request("get", "/search?q=important")
        search_request.GET = {"q": "important"}
        results = json.loads(dashboard_actions.transcript_search(search_request).content)["results"]
        self.assertEqual(results[0]["start_seconds"], 12.5)
        short_request = self.request("get", "/search?q=x")
        short_request.GET = {"q": "x"}
        self.assertEqual(
            json.loads(dashboard_actions.transcript_search(short_request).content)["results"],
            [],
        )

        edit_request = self.request(
            "post", "/edit/", {"id": str(item.id), "title": "New title", "source_name": "New feed"}
        )
        self.assertTrue(json.loads(dashboard_actions.edit_metadata(edit_request).content)["ok"])
        item.refresh_from_db()
        self.assertEqual(item.title, "New title")
        bad_edit = self.request("post", "/edit/", {"id": "bad"})
        self.assertEqual(dashboard_actions.edit_metadata(bad_edit).status_code, 400)


if __name__ == "__main__":
    unittest.main()
