from django.db import models
from django.utils import timezone


class Download(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    source_type = models.CharField(max_length=32)
    source_name = models.CharField(max_length=255)
    title = models.TextField(blank=True, null=True)
    file_path = models.TextField(blank=True, null=True)
    download_status = models.CharField(max_length=32, default="downloaded", db_index=True)
    played = models.BooleanField(default=False)
    favorite = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "downloads"
        indexes = [
            models.Index(fields=["profile_id", "download_status", "-last_seen_at"]),
        ]

    def __str__(self) -> str:
        return self.title or f"Download {self.pk}"


class Job(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"

    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    job_type = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=32, default=STATUS_QUEUED, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    error_message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "jobs"
        indexes = [
            models.Index(fields=["status", "job_type", "created_at"]),
            models.Index(fields=["profile_id", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.job_type} #{self.pk} ({self.status})"
