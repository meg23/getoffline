import gc
import warnings
import unittest

from workers.download_store import close_cached_descriptors

warnings.simplefilter("ignore", ResourceWarning)


class DatabaseCleanupTestCase(unittest.TestCase):
    """TestCase base that keeps Django test database state isolated between tests."""

    def tearDown(self):
        close_cached_descriptors()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
