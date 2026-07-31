import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from django.apps import apps
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from models.domain import DownloadStatus, JobStatus
from models.models import Download, Job, ProfileConfigValue


class BackendApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = set(connection.introspection.table_names())
        with connection.schema_editor() as schema_editor:
            for model in apps.get_models():
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)
                    existing_tables.add(model._meta.db_table)

    def setUp(self):
        for model in reversed(apps.get_models()):
            model.objects.all().delete()
        self.user = User.objects.create_user(username="api-user", password="pw")
        self.client = Client()
        self.client.force_login(self.user)

    def test_health_endpoint_is_public_for_container_readiness(self):
        response = Client(enforce_csrf_checks=True).get(reverse("api_health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "service": "api"})

    def test_library_returns_episode_json(self):
        Download.objects.create(
            profile_id="api-user",
            item_uid="ep-1",
            source_type="podcast",
            source_name="Feed",
            title="Episode One",
            description="A test episode",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_library"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["episodes"][0]["title"], "Episode One")
        self.assertIn("stream_url", payload["episodes"][0])

    def test_manual_video_censor_is_profile_scoped_and_snapshots_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "movie.mp4"
            media.write_bytes(b"video")
            ProfileConfigValue.objects.bulk_create(
                [
                    ProfileConfigValue(
                        profile_id="api-user", key="output_root", value=tmpdir
                    ),
                    ProfileConfigValue(
                        profile_id="api-user", key="video_censor_method", value="beep"
                    ),
                    ProfileConfigValue(
                        profile_id="api-user",
                        key="video_censor_keep_original",
                        value="1",
                    ),
                    ProfileConfigValue(
                        profile_id="api-user",
                        key="video_censor_padding_ms",
                        value="225",
                    ),
                ]
            )
            download = Download.objects.create(
                profile_id="api-user",
                item_uid="video-censor",
                source_type="youtube",
                source_name="Channel",
                file_path="",
                file_path_relative=media.name,
                file_ext="mp4",
                download_status=DownloadStatus.DOWNLOADED,
                last_seen_at=timezone.now(),
            )

            with patch("frontend.views.publish_job") as publish:
                response = self.client.post(
                    reverse("api_censor_download", args=[download.id])
                )

            self.assertEqual(response.status_code, 200)
            download.refresh_from_db()
            self.assertEqual(download.download_status, DownloadStatus.CENSORING)
            job = Job.objects.get(pk=response.json()["job_id"])
            self.assertEqual(download.file_path, str(media.resolve()))
            self.assertEqual(download.file_path_relative, media.name)
            self.assertEqual(job.payload["media_path"], str(media.resolve()))
            self.assertEqual(
                job.payload["censor_policy"],
                {
                    "enabled": True,
                    "method": "beep",
                    "keep_original": True,
                    "padding_ms": 225,
                    "redact_transcript": True,
                },
            )
            publish.assert_called_once()
            duplicate = self.client.post(
                reverse("api_censor_download", args=[download.id])
            )
            self.assertEqual(duplicate.status_code, 409)

    def test_download_api_preserves_audio_default_and_snapshots_video_policy(self):
        ProfileConfigValue.objects.create(
            profile_id="api-user", key="video_censor_enabled", value="1"
        )
        with patch("api.views.publish_job"):
            audio_response = self.client.post(
                reverse("api_download"),
                data=json.dumps({"url": "https://example.test/media"}),
                content_type="application/json",
            )
            video_response = self.client.post(
                reverse("api_download"),
                data=json.dumps(
                    {"url": "https://example.test/media", "media_type": "video"}
                ),
                content_type="application/json",
            )

        self.assertEqual(audio_response.status_code, 200)
        self.assertEqual(video_response.status_code, 200)
        audio_job = Job.objects.get(pk=audio_response.json()["download"]["job_id"])
        video_job = Job.objects.get(pk=video_response.json()["download"]["job_id"])
        self.assertEqual(audio_job.payload["media_type"], "audio")
        self.assertFalse(audio_job.payload["censor_policy"]["enabled"])
        self.assertTrue(video_job.payload["censor_policy"]["enabled"])
        self.assertNotEqual(audio_job.id, video_job.id)
        self.assertNotEqual(audio_job.idempotency_key, video_job.idempotency_key)

    def test_download_api_idempotency_is_profile_scoped(self):
        other = Job.objects.create(
            profile_id="other-user",
            job_type="download_single",
            status=JobStatus.QUEUED,
            payload={"url": "https://example.test/other"},
            idempotency_key="client-key",
        )

        with patch("api.views.publish_job"):
            response = self.client.post(
                reverse("api_download"),
                data=json.dumps(
                    {
                        "url": "https://example.test/mine",
                        "idempotency_key": "client-key",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        created = Job.objects.get(pk=response.json()["download"]["job_id"])
        self.assertNotEqual(created.id, other.id)
        self.assertEqual(created.profile_id, "api-user")

    def test_censor_enabled_manual_upload_is_created_hidden(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ProfileConfigValue.objects.bulk_create(
                [
                    ProfileConfigValue(
                        profile_id="api-user", key="output_root", value=tmpdir
                    ),
                    ProfileConfigValue(
                        profile_id="api-user", key="video_censor_enabled", value="1"
                    ),
                ]
            )
            from api.services import dashboard_actions

            with (
                patch(
                    "api.services.dashboard_actions._write_manual_upload",
                    wraps=dashboard_actions._write_manual_upload,
                ) as write_upload,
                patch("frontend.views.publish_job"),
            ):
                response = self.client.post(
                    reverse("api_dashboard_manual_upload"),
                    {
                        "files": SimpleUploadedFile(
                            "hidden.mp4", b"video", content_type="video/mp4"
                        )
                    },
                )

            self.assertEqual(response.status_code, 201)
            self.assertEqual(
                write_upload.call_args.kwargs["initial_status"],
                DownloadStatus.CENSORING,
            )
            download = Download.objects.get(title="hidden.mp4")
            self.assertEqual(download.download_status, DownloadStatus.CENSORING)

    def test_retry_requires_a_failed_supported_stage_with_retained_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "retry.mp4"
            media.write_bytes(b"video")
            ProfileConfigValue.objects.create(
                profile_id="api-user", key="output_root", value=tmpdir
            )
            download = Download.objects.create(
                profile_id="api-user",
                item_uid="retry-video",
                source_type="youtube",
                source_name="Channel",
                file_path=str(media),
                file_ext="mp4",
                download_status=DownloadStatus.CENSORING,
            )
            failed = Job.objects.create(
                profile_id="api-user",
                job_type="generate_transcript",
                status=JobStatus.FAILED,
                payload={"download_id": download.id, "post_download_censor": True},
            )
            with patch("frontend.views.publish_job") as publish:
                response = self.client.post(reverse("api_retry_job", args=[failed.id]))

            self.assertEqual(response.status_code, 200)
            retry = Job.objects.get(pk=response.json()["job_id"])
            self.assertEqual(retry.job_type, failed.job_type)
            self.assertNotEqual(retry.idempotency_key, failed.idempotency_key)
            publish.assert_called_once()

            media.unlink()
            missing = Job.objects.create(
                profile_id="api-user",
                job_type="censor_audio",
                status=JobStatus.FAILED,
                payload={"download_id": download.id, "media_path": str(media)},
            )
            response = self.client.post(reverse("api_retry_job", args=[missing.id]))
            self.assertEqual(response.status_code, 409)

    def test_retry_plain_transcript_does_not_hide_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "plain.mp3"
            media.write_bytes(b"audio")
            ProfileConfigValue.objects.create(
                profile_id="api-user", key="output_root", value=tmpdir
            )
            download = Download.objects.create(
                profile_id="api-user",
                item_uid="plain-retry",
                source_type="podcast",
                source_name="Feed",
                file_path="",
                file_path_relative=media.name,
                file_ext="mp3",
                download_status=DownloadStatus.DOWNLOADED,
            )
            failed = Job.objects.create(
                profile_id="api-user",
                job_type="generate_transcript",
                status=JobStatus.FAILED,
                payload={"download_id": download.id, "subtitles": True},
            )

            with patch("frontend.views.publish_job"):
                response = self.client.post(
                    reverse("api_retry_job", args=[failed.id])
                )

            self.assertEqual(response.status_code, 200)
            download.refresh_from_db()
            self.assertEqual(download.download_status, DownloadStatus.DOWNLOADED)

    def _create_download(self) -> Download:
        return Download.objects.create(
            profile_id="api-user",
            item_uid="ep-2",
            source_type="podcast",
            source_name="Feed",
            title="Episode Two",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

    def test_frontend_library_defaults_to_unplayed_filter_on_server(self):
        played = Download.objects.create(
            profile_id="api-user",
            item_uid="played-ep",
            source_type="podcast",
            source_name="Feed",
            title="Played Episode",
            download_status=DownloadStatus.DOWNLOADED,
            played=True,
            last_seen_at=timezone.now(),
        )
        unplayed = Download.objects.create(
            profile_id="api-user",
            item_uid="unplayed-ep",
            source_type="podcast",
            source_name="Feed",
            title="Unplayed Episode",
            download_status=DownloadStatus.DOWNLOADED,
            played=False,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_frontend_library"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["library_filter_mode"], "unplayed")
        self.assertEqual([item["id"] for item in payload["downloads"]], [unplayed.id])
        self.assertNotIn(played.id, [item["id"] for item in payload["downloads"]])
        self.assertEqual(payload["stats"]["filters"]["all"], 2)
        self.assertEqual(payload["stats"]["filters"]["unplayed"], 1)
        self.assertEqual(payload["stats"]["filters"]["played"], 1)

    def test_frontend_library_honors_filter_modes(self):
        played = Download.objects.create(
            profile_id="api-user",
            item_uid="played-ep",
            source_type="podcast",
            source_name="Feed",
            title="Played Episode",
            download_status=DownloadStatus.DOWNLOADED,
            played=True,
            last_seen_at=timezone.now(),
        )
        favorite = Download.objects.create(
            profile_id="api-user",
            item_uid="favorite-ep",
            source_type="podcast",
            source_name="Feed",
            title="Favorite Episode",
            download_status=DownloadStatus.DOWNLOADED,
            favorite=True,
            last_seen_at=timezone.now(),
        )

        played_response = self.client.get(
            reverse("api_frontend_library"), {"filter": "played"}
        )
        favorite_response = self.client.get(
            reverse("api_frontend_library"), {"filter": "favorites"}
        )
        all_response = self.client.get(
            reverse("api_frontend_library"), {"filter": "all"}
        )

        self.assertEqual(played_response.status_code, 200)
        self.assertEqual(played_response.json()["library_filter_mode"], "played")
        self.assertEqual(
            [item["id"] for item in played_response.json()["downloads"]], [played.id]
        )
        self.assertEqual(favorite_response.status_code, 200)
        self.assertEqual(favorite_response.json()["library_filter_mode"], "favorites")
        self.assertEqual(
            [item["id"] for item in favorite_response.json()["downloads"]],
            [favorite.id],
        )
        self.assertEqual(all_response.status_code, 200)
        self.assertEqual(all_response.json()["library_filter_mode"], "all")
        self.assertCountEqual(
            [item["id"] for item in all_response.json()["downloads"]],
            [played.id, favorite.id],
        )

    def test_frontend_player_falls_back_to_file_path_extension_for_video_kind(self):
        download = Download.objects.create(
            profile_id="api-user",
            item_uid="ep-video",
            source_type="manual",
            source_name="Imports",
            title="Imported Video",
            file_path="/media/imported-video.webm",
            file_ext="",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_frontend_player", args=[download.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["media_kind"], "video")

    def test_frontend_player_classifies_pdf_as_document(self):
        download = Download.objects.create(
            profile_id="api-user",
            item_uid="pdf-1",
            source_type="manual",
            source_name="Manual Uploads",
            title="Notes.pdf",
            file_path="/media/notes.pdf",
            file_ext="pdf",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_frontend_player", args=[download.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["media_kind"], "document")

    def test_playback_progress_updates_episode_state(self):
        download = self._create_download()

        response = self.client.post(
            reverse("api_playback_progress"),
            data={
                "episode_id": download.id,
                "position_seconds": "12.5",
                "reason": "timeupdate",
            },
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)
        self.assertFalse(download.played)

    def test_session_api_post_requires_csrf_when_checks_are_enforced(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
        )

        self.assertEqual(response.status_code, 403)

    def test_session_api_post_accepts_explicit_csrf_token(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        token = client.get(reverse("api_csrf")).json()["csrf_token"]

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)

    def test_basic_auth_api_post_does_not_require_csrf_token(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        credentials = base64.b64encode(b"api-user:pw").decode("ascii")

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)


if __name__ == "__main__":
    unittest.main()
