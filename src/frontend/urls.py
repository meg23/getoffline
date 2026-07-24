from django.contrib.auth import views as auth_views
from django.urls import include
from django.urls import path

from . import views

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/", include("api.api.urls")),
    path("", views.library, name="library"),
    path("jobs/", views.jobs, name="jobs"),
    path(
        "jobs/active-status/",
        views.active_pipeline_status,
        name="active_pipeline_status",
    ),
    path("jobs/enqueue/", views.enqueue_job, name="enqueue_job"),
    path(
        "worker-messages/status/",
        views.worker_message_status,
        name="worker_message_status",
    ),
    path("batch-update/", views.batch_update, name="batch_update"),
    path("transcript-search/", views.transcript_search, name="transcript_search"),
    path("manual-upload/", views.manual_upload, name="manual_upload"),
    path("edit-metadata/", views.edit_metadata, name="edit_metadata"),
    path("play/<int:download_id>/", views.player, name="player"),
    path("media/<int:download_id>/", views.media, name="media"),
    path("subtitle/<int:download_id>/", views.subtitle, name="subtitle"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/save/", views.save_config, name="save_config"),
    path("sources/add/", views.add_source, name="add_source"),
    path("sources/<str:source_type>/save/", views.save_sources, name="save_sources"),
    path("sources/<int:source_id>/update/", views.update_source, name="update_source"),
    path("sources/<int:source_id>/toggle/", views.toggle_source, name="toggle_source"),
    path("sources/<int:source_id>/delete/", views.delete_source, name="delete_source"),
    path("downloads/<int:download_id>/played/", views.mark_played, name="mark_played"),
    path(
        "downloads/<int:download_id>/unplayed/",
        views.mark_unplayed,
        name="mark_unplayed",
    ),
    path("downloads/<int:download_id>/favorite/", views.favorite, name="favorite"),
    path(
        "downloads/<int:download_id>/unfavorite/", views.unfavorite, name="unfavorite"
    ),
    path(
        "downloads/<int:download_id>/position/",
        views.save_position,
        name="save_position",
    ),
    path(
        "downloads/<int:download_id>/delete-file/",
        views.delete_file,
        name="delete_file",
    ),
]
