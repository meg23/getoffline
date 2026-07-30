"""Global CPU slot scheduler for heavy worker jobs."""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from models.domain import CpuSlotStatus, HeavyJobKind, JobType, parse_str_enum
from workers.logger import get_logger

log = get_logger("workers.scheduler")

HEAVY_JOB_TYPES = {
    JobType.TRANSCODE_MEDIA: HeavyJobKind.FFMPEG,
    JobType.GENERATE_TRANSCRIPT: HeavyJobKind.TRANSCRIPT,
    JobType.GENERATE_OCR: HeavyJobKind.TRANSCRIPT,
}
DEFAULT_CAPACITY = 3
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 1.0
LOCK_KEY = "cpu_slot_scheduler_lock"


class SlotBackend(Protocol):
    def wait_started(
        self, job_type: str, lease_id: str, lease_seconds: float
    ) -> dict: ...
    def wait_heartbeat(self, lease_id: str, lease_seconds: float) -> dict: ...
    def wait_finished(self, lease_id: str) -> dict: ...
    def acquire(
        self, job_type: str, lease_id: str, lease_seconds: float
    ) -> tuple[bool, dict]: ...
    def release(self, lease_id: str) -> dict: ...
    def heartbeat(self, lease_id: str, lease_seconds: float) -> dict: ...
    def snapshot(self) -> dict: ...


class DatabaseSlotBackend:
    """Django database-backed slot accounting.

    This backend intentionally uses the application's existing database instead of
    adding another infrastructure dependency. A singleton row in ``app_config`` is
    locked with ``SELECT ... FOR UPDATE`` while each scheduling decision updates
    ``cpu_slot_requests``. The transaction is short and contains the whole
    decision, so separate worker processes/containers cannot over-admit slots.
    """

    def __init__(self, *, capacity: int):
        self.capacity = capacity

    def wait_started(self, job_type: str, lease_id: str, lease_seconds: float) -> dict:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            CpuSlotRequest.objects.update_or_create(
                lease_id=lease_id,
                defaults={
                    "job_type": job_type,
                    "status": CpuSlotStatus.WAITING,
                    "updated_at": timezone.now(),
                    "expires_at": self._expires_at(lease_seconds),
                },
            )
            return self._stats("wait-started")

    def wait_heartbeat(self, lease_id: str, lease_seconds: float) -> dict:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            updated = CpuSlotRequest.objects.filter(
                lease_id=lease_id, status=CpuSlotStatus.WAITING
            ).update(
                updated_at=timezone.now(), expires_at=self._expires_at(lease_seconds)
            )
            stats = self._stats("wait-heartbeat")
            stats["renewed"] = bool(updated)
            return stats

    def wait_finished(self, lease_id: str) -> dict:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            CpuSlotRequest.objects.filter(
                lease_id=lease_id, status=CpuSlotStatus.WAITING
            ).delete()
            return self._stats("wait-finished")

    def acquire(
        self, job_type: str, lease_id: str, lease_seconds: float
    ) -> tuple[bool, dict]:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            in_use = CpuSlotRequest.objects.filter(status=CpuSlotStatus.RUNNING).count()
            waiting_ffmpeg = (
                CpuSlotRequest.objects.filter(
                    status=CpuSlotStatus.WAITING, job_type=HeavyJobKind.FFMPEG
                )
                .exclude(lease_id=lease_id)
                .exists()
            )
            allowed = False
            reason = "capacity-full"
            if in_use < self.capacity:
                if (
                    parse_str_enum(HeavyJobKind, job_type) is HeavyJobKind.FFMPEG
                    or not waiting_ffmpeg
                ):
                    updated = CpuSlotRequest.objects.filter(lease_id=lease_id).update(
                        job_type=job_type,
                        status=CpuSlotStatus.RUNNING,
                        updated_at=timezone.now(),
                        expires_at=self._expires_at(lease_seconds),
                    )
                    if not updated:
                        CpuSlotRequest.objects.create(
                            lease_id=lease_id,
                            job_type=job_type,
                            status=CpuSlotStatus.RUNNING,
                            expires_at=self._expires_at(lease_seconds),
                        )
                    allowed = True
                    reason = "acquired"
                else:
                    reason = "ffmpeg-waiting"
            return allowed, self._stats(reason)

    def release(self, lease_id: str) -> dict:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            deleted, _ = CpuSlotRequest.objects.filter(lease_id=lease_id).delete()
            return self._stats("released" if deleted else "missing")

    def heartbeat(self, lease_id: str, lease_seconds: float) -> dict:
        with self._locked():
            self._purge_expired()
            CpuSlotRequest = self._request_model()
            updated = CpuSlotRequest.objects.filter(
                lease_id=lease_id, status=CpuSlotStatus.RUNNING
            ).update(
                updated_at=timezone.now(), expires_at=self._expires_at(lease_seconds)
            )
            stats = self._stats("heartbeat")
            stats["renewed"] = bool(updated)
            return stats

    def snapshot(self) -> dict:
        with self._locked():
            self._purge_expired()
            return self._stats("snapshot")

    @contextmanager
    def _locked(self):
        self._ensure_lock_row()
        with transaction.atomic():
            AppConfigValue = self._config_model()
            AppConfigValue.objects.select_for_update().get(key=LOCK_KEY)
            yield

    @staticmethod
    def _ensure_lock_row() -> None:
        from models.models import AppConfigValue

        try:
            AppConfigValue.objects.get_or_create(
                key=LOCK_KEY, defaults={"value": "1", "updated_at": timezone.now()}
            )
        except IntegrityError:
            log.debug("CPU slot scheduler lock row already exists")

    @staticmethod
    def _expires_at(lease_seconds: float):
        return timezone.now() + timezone.timedelta(seconds=float(lease_seconds))

    @staticmethod
    def _purge_expired() -> None:
        from models.models import CpuSlotRequest

        CpuSlotRequest.objects.filter(expires_at__lte=timezone.now()).delete()

    @staticmethod
    def _request_model():
        from models.models import CpuSlotRequest

        return CpuSlotRequest

    @staticmethod
    def _config_model():
        from models.models import AppConfigValue

        return AppConfigValue

    def _stats(self, reason: str) -> dict:
        CpuSlotRequest = self._request_model()
        return {
            "reason": reason,
            "in_use": CpuSlotRequest.objects.filter(
                status=CpuSlotStatus.RUNNING
            ).count(),
            "waiting_ffmpeg": CpuSlotRequest.objects.filter(
                status=CpuSlotStatus.WAITING, job_type=HeavyJobKind.FFMPEG
            ).count(),
            "waiting_transcript": CpuSlotRequest.objects.filter(
                status=CpuSlotStatus.WAITING, job_type=HeavyJobKind.TRANSCRIPT
            ).count(),
        }


class InMemorySlotBackend:
    """Thread-safe scheduler backend used by unit tests."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] = time.time,
    ):
        self.capacity = capacity
        self.clock = clock
        self.requests = {}
        self.lock = threading.Lock()

    def _purge(self):
        now = self.clock()
        for lease_id, request in list(self.requests.items()):
            if request["expires_at"] <= now:
                del self.requests[lease_id]

    def wait_started(self, job_type: str, lease_id: str, lease_seconds: float) -> dict:
        with self.lock:
            self._purge()
            self.requests[lease_id] = {
                "job_type": job_type,
                "status": CpuSlotStatus.WAITING,
                "expires_at": self.clock() + lease_seconds,
            }
            return self._stats("wait-started")

    def wait_heartbeat(self, lease_id: str, lease_seconds: float) -> dict:
        with self.lock:
            self._purge()
            renewed = (
                lease_id in self.requests
                and self.requests[lease_id]["status"] is CpuSlotStatus.WAITING
            )
            if renewed:
                self.requests[lease_id]["expires_at"] = self.clock() + lease_seconds
            stats = self._stats("wait-heartbeat")
            stats["renewed"] = renewed
            return stats

    def wait_finished(self, lease_id: str) -> dict:
        with self.lock:
            self._purge()
            if self.requests.get(lease_id, {}).get("status") is CpuSlotStatus.WAITING:
                self.requests.pop(lease_id, None)
            return self._stats("wait-finished")

    def acquire(
        self, job_type: str, lease_id: str, lease_seconds: float
    ) -> tuple[bool, dict]:
        with self.lock:
            self._purge()
            in_use = sum(
                1
                for request in self.requests.values()
                if request["status"] is CpuSlotStatus.RUNNING
            )
            ffmpeg_waiting = any(
                other_id != lease_id
                and request["status"] is CpuSlotStatus.WAITING
                and parse_str_enum(HeavyJobKind, request["job_type"])
                is HeavyJobKind.FFMPEG
                for other_id, request in self.requests.items()
            )
            reason = "capacity-full"
            ok = False
            if in_use < self.capacity:
                if (
                    parse_str_enum(HeavyJobKind, job_type) is HeavyJobKind.FFMPEG
                    or not ffmpeg_waiting
                ):
                    self.requests[lease_id] = {
                        "job_type": job_type,
                        "status": CpuSlotStatus.RUNNING,
                        "expires_at": self.clock() + lease_seconds,
                    }
                    ok = True
                    reason = "acquired"
                else:
                    reason = "ffmpeg-waiting"
            return ok, self._stats(reason)

    def release(self, lease_id: str) -> dict:
        with self.lock:
            self._purge()
            reason = "released" if self.requests.pop(lease_id, None) else "missing"
            return self._stats(reason)

    def heartbeat(self, lease_id: str, lease_seconds: float) -> dict:
        with self.lock:
            self._purge()
            renewed = (
                lease_id in self.requests
                and self.requests[lease_id]["status"] == CpuSlotStatus.RUNNING
            )
            if renewed:
                self.requests[lease_id]["expires_at"] = self.clock() + lease_seconds
            stats = self._stats("heartbeat")
            stats["renewed"] = renewed
            return stats

    def snapshot(self) -> dict:
        with self.lock:
            self._purge()
            return self._stats("snapshot")

    def _stats(self, reason: str) -> dict:
        return {
            "reason": reason,
            "in_use": sum(
                1
                for request in self.requests.values()
                if request["status"] is CpuSlotStatus.RUNNING
            ),
            "waiting_ffmpeg": sum(
                1
                for request in self.requests.values()
                if request["status"] is CpuSlotStatus.WAITING
                and parse_str_enum(HeavyJobKind, request["job_type"])
                is HeavyJobKind.FFMPEG
            ),
            "waiting_transcript": sum(
                1
                for request in self.requests.values()
                if request["status"] is CpuSlotStatus.WAITING
                and request["job_type"] == "transcript"
            ),
        }


@dataclass
class SlotLease:
    scheduler: GlobalSlotScheduler
    job_type: str
    lease_id: str
    _stop: threading.Event
    _thread: threading.Thread | None

    def release(self) -> None:
        self._stop.set()
        stats = self.scheduler.backend.release(self.lease_id)
        log.info(
            "CPU slot released job_type=%s lease_id=%s in_use=%s waiting_ffmpeg=%s waiting_transcript=%s reason=%s",
            self.job_type,
            self.lease_id,
            stats["in_use"],
            stats["waiting_ffmpeg"],
            stats["waiting_transcript"],
            stats["reason"],
        )


class GlobalSlotScheduler:
    def __init__(
        self,
        backend: SlotBackend,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ):
        self.backend = backend
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds

    @contextmanager
    def acquire(self, job_type: str) -> Iterator[SlotLease]:
        lease = self.acquire_slot(job_type)
        try:
            yield lease
        finally:
            lease.release()

    def run(self, job_type: str, fn: Callable, *args, **kwargs):
        with self.acquire(job_type):
            return fn(*args, **kwargs)

    def acquire_slot(self, job_type: str) -> SlotLease:
        lease_id = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        wait_stats = self.backend.wait_started(job_type, lease_id, self.lease_seconds)
        log.info(
            "CPU slot wait started job_type=%s lease_id=%s in_use=%s waiting_ffmpeg=%s waiting_transcript=%s",
            job_type,
            lease_id,
            wait_stats["in_use"],
            wait_stats["waiting_ffmpeg"],
            wait_stats["waiting_transcript"],
        )
        try:
            while True:
                self.backend.wait_heartbeat(lease_id, self.lease_seconds)
                ok, stats = self.backend.acquire(job_type, lease_id, self.lease_seconds)
                log.info(
                    "CPU slot acquire decision job_type=%s lease_id=%s acquired=%s reason=%s in_use=%s waiting_ffmpeg=%s waiting_transcript=%s",
                    job_type,
                    lease_id,
                    ok,
                    stats["reason"],
                    stats["in_use"],
                    stats["waiting_ffmpeg"],
                    stats["waiting_transcript"],
                )
                if ok:
                    break
                time.sleep(self.poll_seconds)
        except Exception:
            self.backend.wait_finished(lease_id)
            raise
        stop = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_loop, args=(job_type, lease_id, stop), daemon=True
        )
        thread.start()
        return SlotLease(self, job_type, lease_id, stop, thread)

    def _heartbeat_loop(
        self, job_type: str, lease_id: str, stop: threading.Event
    ) -> None:
        while not stop.wait(self.heartbeat_seconds):
            stats = self.backend.heartbeat(lease_id, self.lease_seconds)
            log.info(
                "CPU slot heartbeat job_type=%s lease_id=%s renewed=%s in_use=%s waiting_ffmpeg=%s waiting_transcript=%s",
                job_type,
                lease_id,
                stats.get("renewed"),
                stats["in_use"],
                stats["waiting_ffmpeg"],
                stats["waiting_transcript"],
            )


def scheduler_from_settings() -> GlobalSlotScheduler:
    capacity = int(
        os.getenv(
            "GETOFFLINE_CPU_SCHEDULER_SLOTS",
            getattr(settings, "CPU_SCHEDULER_SLOTS", DEFAULT_CAPACITY),
        )
    )
    return GlobalSlotScheduler(
        DatabaseSlotBackend(capacity=capacity),
        lease_seconds=float(
            os.getenv("GETOFFLINE_CPU_SCHEDULER_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        ),
        heartbeat_seconds=float(
            os.getenv(
                "GETOFFLINE_CPU_SCHEDULER_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS
            )
        ),
        poll_seconds=float(
            os.getenv("GETOFFLINE_CPU_SCHEDULER_POLL_SECONDS", DEFAULT_POLL_SECONDS)
        ),
    )
