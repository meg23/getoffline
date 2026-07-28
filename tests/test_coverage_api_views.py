import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-api-view-coverage.log")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import django

django.setup()

from django.http import Http404, HttpResponse, JsonResponse
from django.test import RequestFactory, TestCase

from api.api import views
from api.services import dashboard_actions


class ApiViewCoverageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from django.apps import apps
        from django.db import connection

        existing = set(connection.introspection.table_names())
        with connection.schema_editor() as editor:
            for model in apps.get_models():
                if model._meta.db_table not in existing:
                    editor.create_model(model)
                    existing.add(model._meta.db_table)
        super().setUpClass()

    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(
            is_authenticated=True,
            get_username=lambda: "alice",
        )

    def request(self, method="get", path="/", data=None, content_type=None):
        if content_type is None:
            request = getattr(self.factory, method)(path, data=data or {})
        else:
            request = getattr(self.factory, method)(
                path, data=data or {}, content_type=content_type
            )
        request.user = self.user
        request._dont_enforce_csrf_checks = True
        return request

    def test_login_redirect_json_and_health_helpers(self):
        request = self.request("post", "/login", {"next": "https://evil.example"})
        self.assertEqual(views._safe_login_redirect(request), "/")
        request = self.request("post", "/login", {"next": "/library/"})
        self.assertEqual(views._safe_login_redirect(request), "/library/")
        request = self.request("post", "/", content_type="application/json")
        request._body = b""
        self.assertEqual(views._json_body(request), {})
        request._body = b"not-json"
        self.assertEqual(views._json_body(request), {})
        request._body = json.dumps([1, 2]).encode()
        self.assertEqual(views._json_body(request), {})
        request._body = json.dumps({"ok": True}).encode()
        self.assertEqual(views._json_body(request), {"ok": True})
        self.assertEqual(views.health(self.request()).status_code, 200)

        with patch("api.api.views.authenticate", return_value=None):
            response = views.login(self.request("post", "/login", {"username": "x"}))
        self.assertEqual(response.status_code, 401)
        user = SimpleNamespace(is_active=True)
        request = self.request("post", "/login", {"next": "/ok"})
        with (
            patch("api.api.views.authenticate", return_value=user),
            patch("api.api.views.auth_login"),
            patch("api.api.views.get_token"),
        ):
            response = views.login(request)
        self.assertEqual(response.status_code, 302)
        with patch("api.api.views.auth_logout"):
            self.assertEqual(views.logout(self.request("post", "/logout")).status_code, 302)

    def test_frontend_endpoints_cover_fallbacks_and_serialization(self):
        item = SimpleNamespace(
            id=4, played=False, favorite=True, title="Episode", last_position_seconds=0,
            file_ext="mp4", file_path="episode.mp4", subtitle_path=None,
        )
        summary = {"id": 4, "title": "Episode"}
        with (
            patch("api.api.views.profile_id_for_request", return_value="alice"),
            patch("api.api.views.normalize_library_filter", return_value="all"),
            patch("api.api.views.list_downloads", return_value=[item]),
            patch("api.api.views.library_filter_counts", return_value={"all": 1}),
            patch("api.api.views.recent_jobs", return_value=[]),
            patch("api.api.views.episode_to_summary", return_value=summary),
            patch("api.api.views.listened_seconds", return_value=61),
        ):
            response = views.frontend_library(self.request("get", "/library"))
        self.assertEqual(json.loads(response.content)["stats"]["visible"], 1)

        job = SimpleNamespace(
            id=8, job_type="download_single", status="queued", error_message=None,
            created_at=None, updated_at=None,
        )
        manager = MagicMock()
        manager.filter.return_value.order_by.return_value.__getitem__.return_value = [job]
        with patch.object(views.Job, "objects", manager):
            response = views.frontend_jobs(self.request())
        self.assertEqual(json.loads(response.content)["jobs"][0]["id"], 8)

        player_request = self.request("get", "/player", {"t": "bad"})
        with (
            patch("api.api.views.get_object_or_404", return_value=item),
            patch("api.api.views.episode_to_summary", return_value=summary),
            patch("api.api.views.resolve_subtitle_path", side_effect=Http404),
        ):
            response = views.frontend_player(player_request, 4)
        payload = json.loads(response.content)
        self.assertFalse(payload["item"]["has_subtitles"])
        self.assertEqual(payload["media_kind"], "video")

        with (
            patch("api.api.views.get_object_or_404", return_value=item),
            patch("api.api.views.resolve_subtitle_path", return_value=None),
            self.assertRaises(Http404),
        ):
            views.subtitle(self.request(), 4)

    def test_dashboard_wrappers_delegate_to_named_service_actions(self):
        wrappers = [
            ("dashboard_active_pipeline_status", "active_pipeline_status", ()),
            ("dashboard_enqueue_job", "enqueue_job", ()),
            ("dashboard_worker_message_status", "worker_message_status", ()),
            ("dashboard_batch_update", "batch_update", ()),
            ("dashboard_transcript_search", "transcript_search", ()),
            ("dashboard_manual_upload", "manual_upload", ()),
            ("dashboard_edit_metadata", "edit_metadata", ()),
            ("dashboard_mark_played", "mark_played", (4,)),
            ("dashboard_mark_unplayed", "mark_unplayed", (4,)),
            ("dashboard_favorite", "favorite", (4,)),
            ("dashboard_unfavorite", "unfavorite", (4,)),
            ("dashboard_save_position", "save_position", (4,)),
            ("dashboard_delete_file", "delete_file", (4,)),
            ("frontend_settings", "settings_page", ()),
            ("settings_save_config", "save_config", ()),
            ("settings_add_source", "add_source", ()),
            ("settings_save_sources", "save_sources", ("youtube",)),
            ("settings_update_source", "update_source", (4,)),
            ("settings_toggle_source", "toggle_source", (4,)),
            ("settings_delete_source", "delete_source", (4,)),
        ]
        request = self.request("post", "/dashboard")
        for wrapper_name, action_name, args in wrappers:
            action = MagicMock(return_value=HttpResponse(status=204))
            with patch.object(dashboard_actions, action_name, action):
                response = getattr(views, wrapper_name)(request, *args)
            self.assertEqual(response.status_code, 204)
            action.assert_called_once_with(request, *args)

    def test_search_library_playback_download_and_account_endpoints(self):
        item = SimpleNamespace(
            id=4, title="Alpha", description="Description", played=True,
            last_position_seconds=12,
        )
        with patch("api.api.views.list_downloads", return_value=[item]), patch(
            "api.api.views.episode_to_summary", return_value={"id": 4}
        ), patch("api.api.views.profile_id_for_request", return_value="alice"):
            self.assertEqual(json.loads(views.search(self.request("get", "/search", {"q": "a"})).content)["results"], [])
            results = json.loads(views.search(self.request("get", "/search", {"q": "alp"})).content)
            self.assertEqual(len(results["results"]), 1)
            history = json.loads(views.history(self.request()).content)
            self.assertEqual(len(history["episodes"]), 1)
            library = json.loads(views.library(self.request()).content)
            self.assertEqual(library["episodes"], [{"id": 4}])
        with patch("api.api.views.SourceConfig.objects.filter") as filter_sources:
            filter_sources.return_value.order_by.return_value = [
                SimpleNamespace(id=1, name="Feed", url="https://feed", enabled=True)
            ]
            self.assertEqual(len(json.loads(views.podcasts(self.request()).content)["podcasts"]), 1)

        item = SimpleNamespace(id=4, last_position_seconds=2, played=False, total_listened_seconds=3, save=MagicMock())
        with patch("api.api.views.get_object_or_404", return_value=item), patch(
            "api.api.views.start", return_value=SimpleNamespace(to_dict=lambda: {"id": 4})
        ):
            self.assertEqual(views.playback_start(self.request("post", "/start", {"episode_id": 4})).status_code, 200)
        with patch("api.api.views.get_object_or_404", return_value=item), patch(
            "api.api.views.build_update", return_value=None
        ):
            self.assertEqual(views.playback_progress(self.request("post", "/progress", {"episode_id": 4, "position_seconds": "bad"})).status_code, 400)
            self.assertEqual(views.playback_complete(self.request("post", "/complete", {"episode_id": 4, "position_seconds": "bad"})).status_code, 400)
        update = SimpleNamespace(to_dict=lambda: {"id": 4})
        with patch("api.api.views.get_object_or_404", return_value=item), patch(
            "api.api.views.build_update", return_value=update
        ), patch("api.api.views.apply_update", return_value=update):
            self.assertEqual(views.playback_progress(self.request("post", "/progress", {"episode_id": 4, "position_seconds": 4})).status_code, 200)
            self.assertEqual(views.playback_complete(self.request("post", "/complete", {"episode_id": 4, "position_seconds": 4})).status_code, 200)

        with patch("api.api.views.create_job", return_value=SimpleNamespace(id=10, job_type="download_single", profile_id="alice", status="queued")), patch(
            "api.api.views.publish_job"
        ) as publish:
            response = views.download(self.request("post", "/download", {"url": "https://example"}))
            self.assertEqual(response.status_code, 200)
            publish.assert_called_once()
        self.assertEqual(views.download(self.request("post", "/download", {})).status_code, 400)
        with patch.object(views, "library", return_value=JsonResponse({"ok": True})):
            self.assertEqual(views.downloads(self.request()).status_code, 200)
        self.assertEqual(json.loads(views.user(self.request()).content)["user"]["username"], "alice")
        self.assertIn("csrf_token", json.loads(views.csrf(self.request()).content))

    def test_stream_and_episode_detail(self):
        item = SimpleNamespace(id=4)
        with patch("api.api.views.get_object_or_404", return_value=item), patch(
            "api.api.views.episode_to_summary", return_value={"id": 4}
        ):
            self.assertEqual(json.loads(views.episode_detail(self.request(), 4).content), {"episode": {"id": 4}})
        with patch("api.api.views.get_object_or_404", return_value=item), patch(
            "api.api.views.resolve_media_path", return_value="/tmp/episode.mp4"
        ), patch("api.api.views.media_response", return_value=HttpResponse(status=206)) as media:
            response = views.stream(self.request("get", "/stream", content_type="application/json"), 4)
            self.assertEqual(response.status_code, 206)
            media.assert_called_once()
