import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

try:
    import django  # noqa: E402
    from django.test import TestCase, override_settings  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - dependency may be absent outside project venv
    django = None
    TestCase = unittest.TestCase

    def override_settings(**_kwargs):
        return lambda cls: cls

if django is not None:
    django.setup()

from app.routing import queue_name  # noqa: E402

if django is not None:
    from models.jobs import claim_job, create_job, finish_job  # noqa: E402
    from models.models import Job  # noqa: E402


@override_settings(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
)
class SharedDjangoModelTests(TestCase):
    @unittest.skipIf(django is None, "Django is not installed")
    def test_create_claim_and_finish_job(self):
        job = create_job(
            profile_id="default",
            job_type="sync_media",
            payload={"source": "test"},
            idempotency_key="sync_media:default:test",
        )
        claimed = claim_job(job.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, Job.STATUS_RUNNING)
        finish_job(claimed, status=Job.STATUS_SUCCEEDED)
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, Job.STATUS_SUCCEEDED)
        self.assertEqual(claimed.payload, {"source": "test"})

    @unittest.skipIf(django is None, "Django is not installed")
    def test_idempotency_reuses_queued_job(self):
        first = create_job(profile_id="default", job_type="summarize_missing", idempotency_key="summary:default")
        second = create_job(profile_id="default", job_type="summarize_missing", idempotency_key="summary:default")
        self.assertEqual(first.id, second.id)


class QueueRoutingTests(unittest.TestCase):
    def test_download_jobs_share_single_download_queue(self):
        self.assertEqual(queue_name("update_downloads"), "getoffline.downloads")
        self.assertEqual(queue_name("download_single"), "getoffline.downloads")

    def test_non_download_jobs_get_separate_queues(self):
        self.assertEqual(queue_name("sync_media"), "getoffline.sync_media")
        self.assertEqual(queue_name("summarize_missing"), "getoffline.summarize_missing")


if __name__ == "__main__":
    unittest.main()
