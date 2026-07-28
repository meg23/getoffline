from django.db import models
from django.utils import timezone

from .domain import DownloadStatus
from .domain import JobStatus
from .domain import ProfanityStatus


class AppConfigValue(models.Model):
    key = models.CharField(max_length=191, primary_key=True)
    value = models.TextField()
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_config"


class ProfileConfigValue(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    key = models.CharField(max_length=191)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "profile_config"
        constraints = [
            models.UniqueConstraint(
                fields=["profile_id", "key"], name="uniq_profile_config_key"
            )
        ]
        indexes = [models.Index(fields=["profile_id", "key"])]


class ProfileDownloadSettings(models.Model):
    profile_id = models.CharField(
        max_length=191, unique=True, default="default", db_index=True
    )
    youtube_cookie_text = models.TextField(blank=True, null=True)
    cookie_updated_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "profile_download_settings"


class DownloadSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    youtube_cookie_text = models.TextField(blank=True, null=True)
    cookie_updated_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "download_settings"


class SourceConfig(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    source_type = models.CharField(max_length=32, db_index=True)
    position = models.IntegerField(default=0)
    name = models.CharField(max_length=255)
    url = models.TextField()
    media_type = models.CharField(max_length=32, blank=True, null=True)
    enabled = models.BooleanField(default=True)
    subtitles = models.BooleanField(default=True)
    subtitle_offset_seconds = models.FloatField(blank=True, null=True)
    max_downloads = models.IntegerField(blank=True, null=True)
    delete_explicit_content = models.BooleanField(default=False)
    include_shorts = models.BooleanField(default=False)
    include_livestreams = models.BooleanField(default=False)
    title_exclude = models.TextField(blank=True, null=True)
    manual_upload_censor_profanity = models.BooleanField(default=False)
    manual_upload_censor_method = models.CharField(
        max_length=32,
        choices=[("duck", "Mute audio"), ("beep", "Beep tone")],
        default="duck",
    )
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "source_configs"
        ordering = ["source_type", "position", "id"]
        indexes = [models.Index(fields=["profile_id", "source_type", "position"])]

    def __str__(self) -> str:
        return f"{self.source_type}: {self.name}"


class Download(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    source_type = models.CharField(max_length=32)
    source_name = models.CharField(max_length=255)
    source_url = models.TextField(blank=True, null=True)
    item_uid = models.CharField(max_length=255, blank=True, null=True)
    item_id = models.CharField(max_length=255, blank=True, null=True)
    item_url = models.TextField(blank=True, null=True)
    media_url = models.TextField(blank=True, null=True)
    title = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    uploader = models.CharField(max_length=255, blank=True, null=True)
    channel = models.CharField(max_length=255, blank=True, null=True)
    upload_date = models.CharField(max_length=32, blank=True, null=True)
    duration_seconds = models.IntegerField(blank=True, null=True)
    file_path = models.TextField(blank=True, null=True)
    file_path_relative = models.TextField(blank=True, null=True)
    file_ext = models.CharField(max_length=32, blank=True, null=True)
    file_size_bytes = models.BigIntegerField(blank=True, null=True)
    subtitle_path = models.TextField(blank=True, null=True)
    subtitle_path_relative = models.TextField(blank=True, null=True)
    download_status = models.CharField(
        max_length=32, default=DownloadStatus.DOWNLOADED, db_index=True
    )
    raw_metadata_json = models.TextField(blank=True, null=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    played = models.BooleanField(default=False)
    favorite = models.BooleanField(default=False)
    played_at = models.DateTimeField(blank=True, null=True)
    last_position_seconds = models.FloatField(default=0.0)
    total_listened_seconds = models.FloatField(default=0.0)
    last_position_updated_at = models.DateTimeField(blank=True, null=True)
    profanity_status = models.CharField(
        max_length=32,
        default=ProfanityStatus.CLEAN,
        db_index=True,
        choices=[(choice, choice.upper()) for choice in ProfanityStatus],
    )

    class Meta:
        db_table = "downloads"
        indexes = [
            models.Index(fields=["profile_id", "download_status", "-last_seen_at"]),
            models.Index(fields=["source_type", "source_name"]),
        ]

    def __str__(self) -> str:
        return self.title or f"Download {self.pk}"


class TranscriptSegment(models.Model):
    download = models.ForeignKey(
        Download,
        db_column="download_id",
        on_delete=models.CASCADE,
        related_name="transcript_segments",
    )
    subtitle_path = models.TextField()
    start_seconds = models.FloatField()
    end_seconds = models.FloatField(blank=True, null=True)
    text = models.TextField()

    class Meta:
        db_table = "transcript_segments"
        indexes = [models.Index(fields=["download", "start_seconds"])]


class CpuSlotRequest(models.Model):
    lease_id = models.CharField(max_length=191, primary_key=True)
    job_type = models.CharField(max_length=32, db_index=True)
    status = models.CharField(max_length=32, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "cpu_slot_requests"
        indexes = [
            models.Index(fields=["status", "job_type", "expires_at"]),
            models.Index(fields=["status", "expires_at"]),
        ]


class ScheduledJob(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    job_type = models.CharField(max_length=64, db_index=True)
    enabled = models.BooleanField(default=True, db_index=True)
    interval_seconds = models.PositiveIntegerField(default=3600)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key_template = models.CharField(max_length=255, blank=True, default="")
    next_run_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_run_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "scheduled_jobs"
        indexes = [
            models.Index(fields=["enabled", "next_run_at"]),
            models.Index(fields=["profile_id", "job_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.job_type} every {self.interval_seconds}s ({'enabled' if self.enabled else 'disabled'})"


class Job(models.Model):
    profile_id = models.CharField(max_length=191, default="default", db_index=True)
    job_type = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=32, default=JobStatus.QUEUED, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(
        max_length=255, blank=True, null=True, db_index=True
    )
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
