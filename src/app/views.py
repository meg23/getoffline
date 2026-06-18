from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse

from models.jobs import create_job
from models.models import Download, Job

from .queue import publish_job


ALLOWED_JOB_TYPES = {"update_downloads", "download_single", "sync_media", "summarize_missing"}


def _profile_id(request: HttpRequest) -> str:
    return str(request.GET.get("profile_id") or request.POST.get("profile_id") or request.session.get("profile_id") or "default")


def library(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    downloads = Download.objects.filter(profile_id=profile_id).order_by("-last_seen_at", "-id")[:100]
    recent_jobs = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    return render(request, "app/library.html", {"downloads": downloads, "jobs": recent_jobs, "profile_id": profile_id})


def jobs(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:100]
    return render(request, "app/jobs.html", {"jobs": rows, "profile_id": profile_id})


def enqueue_job(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    profile_id = _profile_id(request)
    job_type = str(request.POST.get("job_type") or "").strip()
    if job_type not in ALLOWED_JOB_TYPES:
        return HttpResponseBadRequest("Unsupported job_type")

    payload = {"source": "django_app"}
    if request.POST.get("url"):
        payload["url"] = str(request.POST["url"]).strip()
    idempotency_key = request.POST.get("idempotency_key") or f"{job_type}:{profile_id}:{payload.get('url', 'manual')}"
    job = create_job(profile_id=profile_id, job_type=job_type, payload=payload, idempotency_key=idempotency_key)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    return HttpResponseRedirect(reverse("jobs") + f"?profile_id={profile_id}")
