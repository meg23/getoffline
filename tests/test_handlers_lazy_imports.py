import builtins
import importlib
import sys
import unittest


class HandlerLazyImportTests(unittest.TestCase):
    def test_handlers_import_without_downloader_only_dependencies(self):
        """Split worker images can import handlers before selecting a job type."""
        original_import = builtins.__import__
        original_import_module = importlib.import_module
        previous_handlers = sys.modules.pop("workers.handlers", None)

        def guarded_import(name, *args, **kwargs):
            if name in {"feedparser", "yt_dlp"}:
                raise ModuleNotFoundError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        def guarded_import_module(name, *args, **kwargs):
            if name in {"feedparser", "yt_dlp"}:
                raise ModuleNotFoundError(f"No module named '{name}'")
            return original_import_module(name, *args, **kwargs)

        builtins.__import__ = guarded_import
        importlib.import_module = guarded_import_module
        try:
            importlib.import_module("workers.handlers")
        finally:
            builtins.__import__ = original_import
            importlib.import_module = original_import_module
            sys.modules.pop("workers.handlers", None)
            if previous_handlers is not None:
                sys.modules["workers.handlers"] = previous_handlers


if __name__ == "__main__":
    unittest.main()
