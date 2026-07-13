from django.urls import path

from . import views

urlpatterns = [
    path("health", views.health, name="api_health"),
    path("frontend/library", views.frontend_library, name="api_frontend_library"),
    path(
        "dashboard/active-pipeline-status",
        views.dashboard_active_pipeline_status,
        name="api_dashboard_active_pipeline_status",
    ),
    path(
        "dashboard/enqueue-job",
        views.dashboard_enqueue_job,
        name="api_dashboard_enqueue_job",
    ),
    path(
        "dashboard/worker-message-status",
        views.dashboard_worker_message_status,
        name="api_dashboard_worker_message_status",
    ),
    path(
        "dashboard/batch-update",
        views.dashboard_batch_update,
        name="api_dashboard_batch_update",
    ),
    path(
        "dashboard/transcript-search",
        views.dashboard_transcript_search,
        name="api_dashboard_transcript_search",
    ),
    path(
        "dashboard/manual-upload",
        views.dashboard_manual_upload,
        name="api_dashboard_manual_upload",
    ),
    path(
        "dashboard/edit-metadata",
        views.dashboard_edit_metadata,
        name="api_dashboard_edit_metadata",
    ),
    path(
        "dashboard/downloads/<int:download_id>/played",
        views.dashboard_mark_played,
        name="api_dashboard_mark_played",
    ),
    path(
        "dashboard/downloads/<int:download_id>/unplayed",
        views.dashboard_mark_unplayed,
        name="api_dashboard_mark_unplayed",
    ),
    path(
        "dashboard/downloads/<int:download_id>/favorite",
        views.dashboard_favorite,
        name="api_dashboard_favorite",
    ),
    path(
        "dashboard/downloads/<int:download_id>/unfavorite",
        views.dashboard_unfavorite,
        name="api_dashboard_unfavorite",
    ),
    path(
        "dashboard/downloads/<int:download_id>/position",
        views.dashboard_save_position,
        name="api_dashboard_save_position",
    ),
    path(
        "dashboard/downloads/<int:download_id>/delete-file",
        views.dashboard_delete_file,
        name="api_dashboard_delete_file",
    ),
    path("frontend/jobs", views.frontend_jobs, name="api_frontend_jobs"),
    path(
        "frontend/player/<int:episode_id>",
        views.frontend_player,
        name="api_frontend_player",
    ),
    path("search", views.search, name="api_search"),
    path("podcasts", views.podcasts, name="api_podcasts"),
    path("episodes/<int:episode_id>", views.episode_detail, name="api_episode_detail"),
    path("library", views.library, name="api_library"),
    path("playback/start", views.playback_start, name="api_playback_start"),
    path("playback/progress", views.playback_progress, name="api_playback_progress"),
    path("playback/complete", views.playback_complete, name="api_playback_complete"),
    path("history", views.history, name="api_history"),
    path("download", views.download, name="api_download"),
    path("downloads", views.downloads, name="api_downloads"),
    path("user", views.user, name="api_user"),
    path("csrf", views.csrf, name="api_csrf"),
    path("stream/<int:episode_id>", views.stream, name="api_stream"),
    path("subtitle/<int:episode_id>", views.subtitle, name="api_subtitle"),
]
