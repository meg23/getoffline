import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.summarization import (
    DEFAULT_OLLAMA_URL,
    LLAMA_CPP_FILENAME_ENV,
    OLLAMA_URL_ENV,
    SUMMARY_BACKEND_ENV,
    _internal_llama_summary,
    _llama_cpp_model_ref,
    _ollama_summary,
    _ollama_url,
    _summary_backend,
    summarize_segments,
)


class SummarizationTests(unittest.TestCase):
    def test_summary_backend_defaults_to_internal(self):
        with patch.dict(os.environ, {SUMMARY_BACKEND_ENV: ""}, clear=False):
            self.assertEqual(_summary_backend(), "internal")

    def test_summary_backend_accepts_ollama_override(self):
        with patch.dict(os.environ, {SUMMARY_BACKEND_ENV: "ollama"}, clear=False):
            self.assertEqual(_summary_backend(), "ollama")

    def test_llama_cpp_model_ref_allows_filename_override(self):
        with patch.dict(os.environ, {LLAMA_CPP_FILENAME_ENV: "custom.gguf"}, clear=False):
            repo_id, filename = _llama_cpp_model_ref("qwen2.5:0.5b")
        self.assertEqual(repo_id, "Qwen/Qwen2.5-0.5B-Instruct-GGUF")
        self.assertEqual(filename, "custom.gguf")

    def test_internal_summary_uses_loaded_llama_model(self):
        fake_llm = Mock()
        fake_llm.create_chat_completion.return_value = {
            "choices": [{"message": {"content": json.dumps({"summary": "Internal summary."})}}]
        }
        with patch("workers.summarization._load_llama_cpp_model", return_value=fake_llm):
            summary = _internal_llama_summary("Some transcript text.", model_name="qwen2.5:0.5b")
        self.assertEqual(summary, "Internal summary.")
        fake_llm.create_chat_completion.assert_called_once()

    def test_summarize_segments_uses_internal_backend_by_default(self):
        with patch.dict(os.environ, {SUMMARY_BACKEND_ENV: ""}, clear=False):
            with patch("workers.summarization._internal_llama_summary", return_value="Internal summary.") as internal_summary:
                result = summarize_segments(["Some transcript text."], model_name="qwen2.5:0.5b")
        self.assertEqual(result["summary_text"], "Internal summary.")
        self.assertEqual(result["model_name"], "internal:qwen2.5:0.5b")
        internal_summary.assert_called_once()

    def test_ollama_url_defaults_to_loopback(self):
        with patch.dict(os.environ, {OLLAMA_URL_ENV: ""}, clear=False):
            self.assertEqual(_ollama_url(), DEFAULT_OLLAMA_URL)

    def test_ollama_summary_posts_to_environment_url_when_backend_is_ollama(self):
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"response": json.dumps({"summary": "Short summary."})}).encode("utf-8")

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return FakeResponse()

        with patch.dict(os.environ, {OLLAMA_URL_ENV: "http://ollama:11434/api/generate"}, clear=False):
            with patch("workers.summarization.request.urlopen", side_effect=fake_urlopen):
                summary = _ollama_summary("Some transcript text.", model_name="qwen2.5:0.5b", timeout_seconds=12)

        self.assertEqual(summary, "Short summary.")
        self.assertEqual(seen["url"], "http://ollama:11434/api/generate")
        self.assertEqual(seen["timeout"], 12)


if __name__ == "__main__":
    unittest.main()
