import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-dashboard-coverage.log")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import django

django.setup()

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api.services import dashboard_actions as actions
from models.domain import DownloadStatus, SourceType
from models.models import (
    Download,
    Job,
    ProfileConfigValue,
    SourceConfig,
    TranscriptSegment,
)


def ensure_schema():
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in apps.get_models():
            if model._meta.db_table not in existing:
                editor.create_model(model)
                existing.add(model._meta.db_table)


class DashboardActionCoverageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_schema()
        super().setUpClass()

    def setUp(self):
        for model in reversed(apps.get_models()):
            model.objects.all().delete()
        self.factory = RequestFactory()
        self.user = SimpleNamespace(is_authenticated=True, get_username=lambda: "alice")

    def request(self, method="post", path="/", data=None, headers=None):
        request = getattr(self.factory, method)(path, data or {}, **(headers or {}))
        request.user = self.user
        return request

    def test_small_helpers_and_pipeline_branches(self):
        self.assertEqual(tuple(actions.JobStage("one", "Two")), ("one", "Two"))
        upload = actions.ManualUploadResult(SimpleNamespace(id=1), Path("/tmp/a"))
        self.assertEqual(tuple(upload), (upload.download, upload.path))
        self.assertEqual(actions._optional_int("12"), 12)
        self.assertIsNone(actions._optional_int("x"))
        self.assertIsNone(actions._optional_float(""))
        self.assertIsNone(actions._optional_float("bad"))
        self.assertEqual(actions._human_size(0), "—")
        self.assertIn("MB", actions._human_size(1024 * 1024))
        self.assertEqual(actions._human_duration(3660), "1h 1m")
        request = self.request("get")
        request.user = SimpleNamespace(is_authenticated=False, get_username=lambda: "")
        self.assertEqual(actions._profile_id(request), "default")
        self.assertEqual(actions._profile_id(self.request("get")), "alice")
        request = self.request("post", data={"next": "/next"})
        self.assertEqual(actions._redirect_back(request)["Location"], "/next")

        missing = Job(profile_id="alice", job_type="download_single", payload={})
        self.assertTrue(actions._job_still_needs_work(missing))
        missing.payload = {"download_id": 88}
        self.assertTrue(actions._job_still_needs_work(missing))
        missing.job_type = "other"
        self.assertFalse(actions._job_still_needs_work(missing))
        for job_type, expected in (
            ("generate_transcript", "transcript_generation"),
            ("transcode_media", "transcript_generation"),
            ("download_episode", "downloading"),
        ):
            self.assertEqual(actions._job_stage(Job(job_type=job_type)).name, expected)
        self.assertEqual(actions._job_display_title(Job(job_type="my_job", payload={})), "My Job")
        self.assertEqual(actions._job_display_title(Job(job_type="my_job", payload={"title": "Title"})), "Title")

        with patch.dict(os.environ, {"GETOFFLINE_ACTIVE_PIPELINE_STALE_SECONDS": "-1"}):
            self.assertIsNone(actions._active_pipeline_cutoff())
        with patch.dict(os.environ, {"GETOFFLINE_ACTIVE_PIPELINE_STALE_SECONDS": "10"}):
            self.assertTrue(actions._job_is_fresh(Job(created_at=timezone.now())))

    def test_settings_enqueue_status_and_metadata_actions(self):
        with patch("api.services.dashboard_actions.render", return_value=SimpleNamespace(status_code=200)):
            response = actions.settings_page(self.request("get"))
        self.assertEqual(response.status_code, 200)

        with patch("api.services.dashboard_actions.create_job", return_value=SimpleNamespace(id=5, job_type="update_downloads", profile_id="alice", status="queued")), patch(
            "api.services.dashboard_actions.publish_job"
        ) as publish:
            request = self.request("post", data={"job_type": "update_downloads"}, headers={"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"})
            response = actions.enqueue_job(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("status_url", json.loads(response.content))
        publish.assert_called_once()
        self.assertEqual(actions.enqueue_job(self.request("get")).status_code, 400)
        self.assertEqual(actions.enqueue_job(self.request("post", data={"job_type": "bad"})).status_code, 400)
        with patch("api.services.dashboard_actions.create_job", return_value=SimpleNamespace(id=6, job_type="download_single", profile_id="alice", status="queued")), patch(
            "api.services.dashboard_actions.publish_job"
        ):
            response = actions.enqueue_job(self.request("post", data={"job_type": "download_single", "url": "https://example", "next": "/jobs/"}))
        self.assertEqual(response.status_code, 302)

        with patch.object(actions.Job.objects, "filter") as filter_jobs:
            filter_jobs.return_value.first.return_value = None
            self.assertFalse(json.loads(actions.worker_message_status(self.request("get", "/status", {"job_id": "8"})).content)["finished"])
            self.assertEqual(actions.worker_message_status(self.request("get", "/status")).status_code, 400)
            filter_jobs.return_value.order_by.return_value.first.return_value = None
            self.assertFalse(json.loads(actions.worker_message_status(self.request("get", "/status", {"token": "x"})).content)["finished"])

        item = Download.objects.create(
            profile_id="alice", source_type=SourceType.PODCAST, source_name="Feed",
            title="Old", item_uid="item-1", item_id="item-1", item_url="https://item",
            file_path="", file_ext="mp3", download_status=DownloadStatus.DOWNLOADED,
        )
        for action_name, expected in (("mark_played", True), ("mark_unplayed", False), ("favorite", True), ("unfavorite", False)):
            response = getattr(actions, action_name)(self.request("post", data={"next": "/library/"}), item.id)
            self.assertEqual(response.status_code, 302)
            item.refresh_from_db()
            field = "played" if "played" in action_name else "favorite"
            self.assertEqual(getattr(item, field), expected)
        self.assertEqual(actions.edit_metadata(self.request("post", data={"id": "bad"})).status_code, 400)
        self.assertEqual(actions.edit_metadata(self.request("post", data={"id": str(item.id), "title": "", "source_name": ""})).status_code, 400)
        response = actions.edit_metadata(self.request("post", data={"id": str(item.id), "title": "New", "source_name": "New Feed"}))
        self.assertEqual(response.status_code, 200)

    def test_source_settings_and_batch_actions(self):
        source = SourceConfig.objects.create(
            profile_id="alice", source_type=SourceType.YOUTUBE, name="Old",
            url="https://youtube.com/old", media_type="video", enabled=True,
        )
        self.assertEqual(actions.add_source(self.request("post", data={"source_type": "bad"})).status_code, 400)
        valid = {
            "source_type": "youtube", "name": "New", "url": "https://youtube.com/new",
            "media_type": "video", "enabled": "1", "subtitles": "1",
        }
        self.assertEqual(actions.add_source(self.request("post", data=valid)).status_code, 302)
        update = dict(valid)
        update.pop("source_type")
        response = actions.update_source(self.request("post", data=update), source.id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(actions.toggle_source(self.request("post"), source.id).status_code, 302)
        self.assertEqual(actions.delete_source(self.request("post"), source.id).status_code, 302)

        item = Download.objects.create(
            profile_id="alice", source_type=SourceType.PODCAST, source_name="Feed",
            title="Episode", item_uid="batch-1", item_id="batch-1", item_url="https://item",
            file_path="", file_ext="mp3", download_status=DownloadStatus.DOWNLOADED,
        )
        for action_name in ("played", "unplayed", "favorite", "unfavorite"):
            response = actions.batch_update(self.request("post", data={"ids": [str(item.id)], "batch_action": action_name}))
            self.assertEqual(response.status_code, 302)
        with patch("api.services.dashboard_actions.create_job", return_value=SimpleNamespace(id=9, job_type="download_single", profile_id="alice")), patch(
            "api.services.dashboard_actions.publish_job"
        ) as publish:
            response = actions.batch_update(self.request("post", data={"ids": [str(item.id)], "batch_action": "download"}))
        self.assertEqual(response.status_code, 302)
        publish.assert_called_once()
        self.assertEqual(actions.batch_update(self.request("post", data={"ids": [], "batch_action": "purge"})).status_code, 302)
        response = actions.batch_update(
            self.request(
                "post", data={"ids": [str(item.id)], "batch_action": "edit-metadata"}
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_manual_upload_validation_and_search(self):
        with patch("api.services.dashboard_actions._write_manual_upload", side_effect=ValueError("bad file")):
            request = self.request("post")
            request.FILES.setlist("files", [SimpleNamespace(name="bad.txt")])
            self.assertEqual(actions.manual_upload(request).status_code, 400)
        request = self.request("post")
        self.assertEqual(actions.manual_upload(request).status_code, 400)
        with tempfile.TemporaryDirectory() as tmpdir:
            ProfileConfigValue.objects.create(profile_id="alice", key="output_root", value=tmpdir)
            path = Path(tmpdir) / "episode.mp3"
            path.write_bytes(b"x")
            item = Download.objects.create(
                profile_id="alice", source_type=SourceType.PODCAST, source_name="Feed",
                title="Episode", item_uid="search-1", item_id="search-1", file_path=str(path),
                subtitle_path=None, download_status=DownloadStatus.DOWNLOADED,
            )
            TranscriptSegment.objects.create(download=item, start_seconds=2, end_seconds=3, text="search phrase")
            self.assertEqual(json.loads(actions.transcript_search(self.request("get", "/search", {"q": "x"})).content)["results"], [])
            self.assertEqual(len(json.loads(actions.transcript_search(self.request("get", "/search", {"q": "phrase"})).content)["results"]), 1)
            self.assertEqual(actions._normalize_upload_stem("...bad?.mp3"), "bad-.mp3")
            with self.assertRaises(ValueError):
                actions._write_manual_upload("alice", SimpleNamespace(name="bad.txt", chunks=list))

    def test_import_downloads_directory_command_registers_channel_folders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            channel_dir = root / "My Channel"
            channel_dir.mkdir()
            pdf_path = channel_dir / "lease.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%EOF\n")
            mp3_path = channel_dir / "episode.mp3"
            mp3_path.write_bytes(b"audio")

            call_command("import_downloads_directory", str(root), profile_id="alice")

            pdf = Download.objects.get(profile_id="alice", title="lease.pdf")
            audio = Download.objects.get(profile_id="alice", title="episode.mp3")
            self.assertEqual(pdf.source_name, "My Channel")
            self.assertEqual(audio.source_name, "My Channel")
            self.assertEqual(pdf.file_path, str(pdf_path.resolve()))
            self.assertEqual(pdf.file_ext, "pdf")
