from django.urls import path

from . import views

urlpatterns = [
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
]
