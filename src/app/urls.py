from django.urls import path

from . import views

urlpatterns = [
    path("", views.library, name="library"),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/enqueue/", views.enqueue_job, name="enqueue_job"),
]
