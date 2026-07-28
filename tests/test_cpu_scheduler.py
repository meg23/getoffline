import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.scheduler import GlobalSlotScheduler, InMemorySlotBackend


class CpuSlotSchedulerTests(unittest.TestCase):
    def test_maximum_concurrency_is_three(self):
        backend = InMemorySlotBackend(capacity=3)

        acquired = [
            backend.acquire("ffmpeg", f"lease-{idx}", 60)[0] for idx in range(4)
        ]

        self.assertEqual(acquired, [True, True, True, False])
        self.assertEqual(backend.snapshot()["in_use"], 3)

    def test_ffmpeg_priority_blocks_transcript_when_ffmpeg_is_waiting(self):
        backend = InMemorySlotBackend(capacity=3)
        self.assertTrue(backend.acquire("transcript", "running-1", 60)[0])
        self.assertTrue(backend.acquire("transcript", "running-2", 60)[0])
        backend.wait_started("ffmpeg", "waiting-ffmpeg", 60)

        transcript_ok, transcript_stats = backend.acquire(
            "transcript", "transcript-new", 60
        )
        ffmpeg_ok, ffmpeg_stats = backend.acquire("ffmpeg", "ffmpeg-new", 60)

        self.assertFalse(transcript_ok)
        self.assertEqual(transcript_stats["reason"], "ffmpeg-waiting")
        self.assertTrue(ffmpeg_ok)
        self.assertEqual(ffmpeg_stats["in_use"], 3)

    def test_transcripts_use_all_slots_when_ffmpeg_is_idle(self):
        backend = InMemorySlotBackend(capacity=3)

        acquired = [
            backend.acquire("transcript", f"lease-{idx}", 60)[0] for idx in range(3)
        ]

        self.assertEqual(acquired, [True, True, True])
        self.assertEqual(backend.snapshot()["in_use"], 3)

    def test_expired_lease_recovers_slot_after_worker_failure(self):
        now = [100.0]
        backend = InMemorySlotBackend(capacity=1, clock=lambda: now[0])
        self.assertTrue(backend.acquire("ffmpeg", "crashed-worker", 10)[0])
        self.assertFalse(backend.acquire("ffmpeg", "blocked-worker", 10)[0])

        now[0] = 111.0
        recovered, stats = backend.acquire("transcript", "replacement-worker", 10)

        self.assertTrue(recovered)
        self.assertEqual(stats["in_use"], 1)

    def test_context_manager_releases_slot_on_exception(self):
        backend = InMemorySlotBackend(capacity=1)
        scheduler = GlobalSlotScheduler(
            backend, heartbeat_seconds=3600, poll_seconds=0.001
        )

        with self.assertRaises(RuntimeError):
            with scheduler.acquire("ffmpeg"):
                self.assertEqual(backend.snapshot()["in_use"], 1)
                raise RuntimeError("boom")

        self.assertEqual(backend.snapshot()["in_use"], 0)


if __name__ == "__main__":
    unittest.main()
