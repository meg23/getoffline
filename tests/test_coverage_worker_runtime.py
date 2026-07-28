import json
import os
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-runtime-coverage.log")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import django

django.setup()

from models.domain import HeavyJobKind, JobType, MediaType, SourceType
from workers import runner
from workers.runner import QueuedJobMessage, WorkerConfig
from workers.scheduler import (
    DatabaseSlotBackend,
    GlobalSlotScheduler,
    InMemorySlotBackend,
    scheduler_from_settings,
)


class RunnerCoverageTests(unittest.TestCase):
    def test_message_and_worker_configuration_helpers(self):
        message = QueuedJobMessage.from_body(
            json.dumps(
                {"job_id": "7", "job_type": "download_single", "attempt": 2}
            ).encode()
        )
        self.assertEqual(message.job_id, 7)
        self.assertEqual(message.job_type, "download_single")
        self.assertEqual(runner.safe_prefetch_count("updates", 12), 1)
        self.assertEqual(runner.safe_prefetch_count("ffmpeg", 0), 4)
        with patch.dict(os.environ, {"GETOFFLINE_WORKER_MAX_MESSAGES": "4"}):
            self.assertEqual(runner.safe_max_messages(None), 4)
        config = runner.build_worker_config("transcripts", prefetch_count=2, max_messages=3)
        self.assertEqual(config, WorkerConfig("transcripts", runner.TRANSCRIPT_QUEUE, 2, 3))

    def test_signal_scheduler_and_connection_shutdown_helpers(self):
        runner._STOP = False
        runner._handle_signal(15, None)
        self.assertTrue(runner.worker_should_stop())
        runner._STOP = False
        runner._SCHEDULER = None
        with patch("workers.runner.scheduler_from_settings", return_value="scheduler") as factory:
            self.assertEqual(runner._scheduler(), "scheduler")
            self.assertIs(runner._scheduler(), "scheduler")
            factory.assert_called_once_with()

        closed = SimpleNamespace(is_closed=False, close=MagicMock())
        runner.close_connection_if_open(closed)
        closed.close.assert_called_once_with()
        already_closed = SimpleNamespace(is_closed=True, close=MagicMock())
        runner.close_connection_if_open(already_closed)
        already_closed.close.assert_not_called()

    def test_process_message_claim_skip_success_and_failure(self):
        job = SimpleNamespace(id=9, job_type=JobType.DOWNLOAD_SINGLE.value)
        message = QueuedJobMessage(9, JobType.DOWNLOAD_SINGLE.value)
        with (
            patch.object(runner, "close_old_connections"),
            patch.object(runner, "claim_queued_job", return_value=None),
            patch.object(runner, "run_claimed_job") as run_job,
        ):
            runner.process_queued_job_message(message)
            run_job.assert_not_called()

        with (
            patch.object(runner, "close_old_connections"),
            patch.object(runner, "claim_queued_job", return_value=job),
            patch.object(runner, "run_claimed_job") as run_job,
            patch.object(runner, "mark_job_succeeded") as succeeded,
        ):
            runner.process_message({"job_id": 9, "job_type": "download_single"})
            run_job.assert_called_once_with(job)
            succeeded.assert_called_once_with(job)

        error = RuntimeError("failed")
        with (
            patch.object(runner, "close_old_connections"),
            patch.object(runner, "claim_queued_job", return_value=job),
            patch.object(runner, "run_claimed_job", side_effect=error),
            patch.object(runner, "mark_job_failed") as failed,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                runner.process_queued_job_message(message)
            failed.assert_called_once_with(job, error)

    def test_run_claimed_job_and_completion_message_branches(self):
        job = SimpleNamespace(
            id=11,
            job_type=JobType.TRANSCODE_MEDIA.value,
            profile_id="p1",
            payload={"completion_token": " token "},
        )
        handler = MagicMock()
        scheduler = MagicMock()
        with (
            patch.dict(runner.HANDLERS, {job.job_type: handler}),
            patch.object(runner, "_scheduler", return_value=scheduler),
        ):
            runner.run_claimed_job(job)
        scheduler.run.assert_called_once_with(HeavyJobKind.FFMPEG, handler, job)
        handler.assert_not_called()

        direct = SimpleNamespace(
            id=12, job_type=JobType.DOWNLOAD_SINGLE.value, profile_id="p1", payload={}
        )
        with patch.dict(runner.HANDLERS, {direct.job_type: handler}):
            runner.run_claimed_job(direct)
        handler.assert_called_once_with(direct)

        update = SimpleNamespace(
            id=13,
            job_type=JobType.UPDATE_DOWNLOADS.value,
            profile_id="p1",
            payload={"completion_token": "abc"},
        )
        with patch.object(runner, "create_update_finished_message_once") as create:
            runner.emit_update_finished_message(update, status="failed", error_message="x")
            create.assert_called_once_with(update, "abc", "failed", "x")
        no_token = SimpleNamespace(
            id=14, job_type=JobType.UPDATE_DOWNLOADS.value, profile_id="p1", payload={}
        )
        with patch.object(runner, "create_update_finished_message_once") as create:
            runner.emit_update_finished_message(no_token, status="succeeded")
            create.assert_not_called()
        with patch.object(runner, "finish_job"), patch.object(
            runner, "emit_update_finished_message"
        ):
            runner.mark_job_succeeded(update)
            runner.mark_job_failed(update, RuntimeError("boom"))

    def test_completion_message_is_idempotent(self):
        job = SimpleNamespace(id=15, profile_id="p1", payload={})
        manager = MagicMock()
        with patch.object(runner.Job, "objects", manager):
            manager.filter.return_value.exists.return_value = True
            runner.create_update_finished_message_once(job, "token", "succeeded", "")
            manager.create.assert_not_called()
            manager.filter.return_value.exists.return_value = False
            runner.create_update_finished_message_once(job, "token", "failed", "error")
            manager.create.assert_called_once()
            self.assertEqual(manager.create.call_args.kwargs["payload"]["source_status"], "failed")

    def test_requeue_and_worker_routing_helpers(self):
        youtube = SimpleNamespace(
            id=1, job_type=JobType.DOWNLOAD_SINGLE.value, profile_id="p", payload={"source_type": "youtube"}
        )
        podcast = SimpleNamespace(
            id=2, job_type=JobType.DOWNLOAD_SINGLE.value, profile_id="p", payload={"source_type": "podcast"}
        )
        channel = MagicMock()
        with patch.object(runner, "publish_requeued_job") as publish:
            self.assertEqual(runner.publish_requeued_jobs(channel, "downloader-youtube", [youtube, podcast]), 1)
            publish.assert_called_once_with(channel, youtube, runner.YOUTUBE_DOWNLOAD_QUEUE)
        self.assertTrue(runner.worker_type_is_wrong_downloader("downloader-youtube", runner.PODCAST_DOWNLOAD_QUEUE))
        self.assertFalse(runner.worker_type_is_wrong_downloader("ffmpeg", runner.FFMPEG_QUEUE))
        runner.publish_requeued_job(channel, youtube, runner.YOUTUBE_DOWNLOAD_QUEUE)
        kwargs = channel.basic_publish.call_args.kwargs
        self.assertEqual(json.loads(kwargs["body"])["job_id"], 1)
        with patch.object(runner, "queued_jobs_for_worker", return_value=[youtube]), patch.object(
            runner, "publish_requeued_jobs", return_value=1
        ) as publish:
            self.assertEqual(runner.requeue_existing_jobs(channel, "downloader-youtube"), 1)
            publish.assert_called_once()
        for value in ("1", "true", "yes", "on"):
            with patch.dict(os.environ, {"GETOFFLINE_REQUEUE_EXISTING_JOBS": value}):
                self.assertTrue(runner.requeue_existing_jobs_enabled())
        with patch.dict(os.environ, {"GETOFFLINE_REQUEUE_EXISTING_JOBS": "0"}):
            self.assertFalse(runner.requeue_existing_jobs_enabled())

    def test_worker_channel_consumption_and_delivery(self):
        config = WorkerConfig("ffmpeg", runner.FFMPEG_QUEUE, 2, 1)
        connection = MagicMock()
        channel = MagicMock()
        connection.channel.return_value = channel
        opened = runner.open_worker_channel(connection, config)
        self.assertIs(opened, channel)
        channel.exchange_declare.assert_called_once()
        channel.queue_declare.assert_called_once()
        channel.queue_bind.assert_called_once()
        channel.basic_qos.assert_called_once_with(prefetch_count=2)

        method = SimpleNamespace(delivery_tag=22)
        body = json.dumps({"job_id": 3}).encode()
        with patch.object(runner, "process_queued_job_message"):
            self.assertEqual(runner.process_delivery(channel, config, method, QueuedJobMessage(3)), 1)
            channel.basic_ack.assert_called_once_with(22)
        with patch.object(runner, "process_queued_job_message", side_effect=RuntimeError("x")):
            self.assertEqual(runner.process_delivery(channel, config, method, QueuedJobMessage(3)), 1)
            channel.basic_nack.assert_called_once_with(22, requeue=False)
        channel.reset_mock()
        with patch.object(runner, "worker_should_stop", side_effect=[False, False]), patch.object(
            runner, "handle_delivery", return_value=1
        ) as handle:
            channel.consume.return_value = [(None, None, b""), (method, None, body)]
            runner.consume_worker_messages(channel, config)
            handle.assert_called_once()

        with patch.object(runner, "worker_should_requeue_message", return_value=True), patch.object(
            runner, "requeue_message_for_matching_worker"
        ) as requeue:
            self.assertEqual(runner.handle_delivery(channel, config, method, body), 0)
            requeue.assert_called_once()

    def test_downloader_acceptance_and_signal_cli_helpers(self):
        self.assertTrue(runner.worker_accepts_job("ffmpeg", 1))
        with patch.object(runner.Job.objects, "filter") as filter_jobs:
            filter_jobs.return_value.only.return_value.first.return_value = None
            self.assertTrue(runner.worker_accepts_job("downloader-youtube", 1))
            job = SimpleNamespace(payload={"source_type": "podcast"})
            filter_jobs.return_value.only.return_value.first.return_value = job
            self.assertFalse(runner.worker_accepts_job("downloader-youtube", 1))
            self.assertTrue(runner.worker_accepts_job("downloader-podcast", 1))
        self.assertEqual(runner.source_type_for_downloader_job(SimpleNamespace(payload={"source_type": "youtube"})), SourceType.YOUTUBE)
        self.assertEqual(runner.source_type_for_downloader_job(SimpleNamespace(payload={"media_type": MediaType.AUDIO.value})), SourceType.PODCAST)
        self.assertEqual(runner.source_type_for_downloader_job(SimpleNamespace(payload={})), SourceType.YOUTUBE)
        with patch.object(sys, "argv", ["runner", "ffmpeg", "--prefetch", "3", "--max-messages", "2"]):
            args = runner.parse_args()
        self.assertEqual((args.worker_type, args.prefetch, args.max_messages), ("ffmpeg", 3, 2))
        with patch.object(signal, "signal") as signal_call:
            runner.install_signal_handlers()
            self.assertEqual(signal_call.call_count, 2)


class SchedulerCoverageTests(unittest.TestCase):
    def test_in_memory_backend_branches_and_expiry(self):
        now = [100.0]
        backend = InMemorySlotBackend(capacity=1, clock=lambda: now[0])
        self.assertTrue(backend.acquire("transcript", "occupied", 10)[0])
        self.assertEqual(backend.wait_started("ffmpeg", "wait", 10)["waiting_ffmpeg"], 1)
        self.assertTrue(backend.wait_heartbeat("wait", 10)["renewed"])
        acquired, stats = backend.acquire("transcript", "run", 10)
        self.assertFalse(acquired)
        self.assertEqual(stats["reason"], "capacity-full")
        self.assertEqual(backend.wait_finished("wait")["waiting_transcript"], 0)
        self.assertEqual(backend.wait_finished("wait")["waiting_ffmpeg"], 0)
        self.assertFalse(backend.wait_heartbeat("run", 10)["renewed"])
        self.assertTrue(backend.heartbeat("occupied", 10)["renewed"])
        self.assertEqual(backend.release("occupied")["reason"], "released")
        self.assertEqual(backend.release("occupied")["reason"], "missing")
        backend.wait_started("ffmpeg", "ff", 1)
        backend.wait_heartbeat("missing", 1)
        now[0] = 102
        self.assertEqual(backend.snapshot()["waiting_ffmpeg"], 0)

    def test_scheduler_run_waits_releases_and_cleans_waiter_on_error(self):
        backend = InMemorySlotBackend(capacity=1)
        scheduler = GlobalSlotScheduler(backend, heartbeat_seconds=0.01, poll_seconds=0)
        self.assertEqual(scheduler.run("ffmpeg", lambda value: value + 1, 4), 5)
        self.assertEqual(backend.snapshot()["in_use"], 0)

        class FailingBackend(InMemorySlotBackend):
            def acquire(self, *args, **kwargs):
                raise RuntimeError("scheduler failure")

        failing = FailingBackend()
        with self.assertRaisesRegex(RuntimeError, "scheduler failure"), patch.object(failing, "wait_finished", wraps=failing.wait_finished) as finished:
            GlobalSlotScheduler(failing, poll_seconds=0).acquire_slot("ffmpeg")
        finished.assert_called_once()

    def test_scheduler_heartbeat_loop_and_settings(self):
        backend = MagicMock()
        backend.heartbeat.return_value = {
            "renewed": True, "in_use": 1, "waiting_ffmpeg": 0, "waiting_transcript": 0
        }
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        scheduler = GlobalSlotScheduler(backend, lease_seconds=9, heartbeat_seconds=2)
        scheduler._heartbeat_loop("ffmpeg", "lease", stop)
        backend.heartbeat.assert_called_once_with("lease", 9)
        with patch.dict(os.environ, {
            "GETOFFLINE_CPU_SCHEDULER_SLOTS": "2",
            "GETOFFLINE_CPU_SCHEDULER_LEASE_SECONDS": "8",
            "GETOFFLINE_CPU_SCHEDULER_HEARTBEAT_SECONDS": "3",
            "GETOFFLINE_CPU_SCHEDULER_POLL_SECONDS": "0.5",
        }):
            configured = scheduler_from_settings()
        self.assertEqual((configured.lease_seconds, configured.heartbeat_seconds, configured.poll_seconds), (8.0, 3.0, 0.5))


class DatabaseSlotBackendCoverageTests(unittest.TestCase):
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

    def setUp(self):
        from models.models import AppConfigValue, CpuSlotRequest

        CpuSlotRequest.objects.all().delete()
        AppConfigValue.objects.filter(key="cpu_slot_scheduler_lock").delete()

    def test_database_backend_lifecycle_and_capacity(self):
        backend = DatabaseSlotBackend(capacity=1)
        self.assertTrue(backend.acquire("transcript", "occupied", 20)[0])
        self.assertEqual(backend.wait_started("ffmpeg", "wait", 20)["waiting_ffmpeg"], 1)
        self.assertTrue(backend.wait_heartbeat("wait", 20)["renewed"])
        ok, stats = backend.acquire("transcript", "run", 20)
        self.assertFalse(ok)
        self.assertEqual(stats["reason"], "capacity-full")
        backend.wait_finished("wait")
        self.assertTrue(backend.wait_heartbeat("wait", 20)["renewed"] is False)
        self.assertTrue(backend.heartbeat("occupied", 20)["renewed"])
        self.assertFalse(backend.heartbeat("missing", 20)["renewed"])
        self.assertEqual(backend.release("occupied")["reason"], "released")
        self.assertEqual(backend.release("missing")["reason"], "missing")
