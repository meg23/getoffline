from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "client-app" / "getoffline_console.py"
spec = importlib.util.spec_from_file_location("getoffline_console", MODULE_PATH)
assert spec is not None and spec.loader is not None
console = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = console
spec.loader.exec_module(console)


class BridgeHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, Any]]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server hook name
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append((self.path, payload))
        body = json.dumps({"session_id": "session-123"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class ConsolePlaybackTests(unittest.TestCase):
    def test_default_bridge_stop_url_uses_sibling_stop_endpoint(self):
        self.assertEqual(
            console.default_bridge_stop_url("http://bridge.local/play"),
            "http://bridge.local/stop",
        )
        self.assertEqual(
            console.default_bridge_stop_url("http://bridge.local/api/audio"),
            "http://bridge.local/api/audio/stop",
        )

    def test_load_credentials_accepts_legacy_credentials_file(self):
        original = console.CONFIG_FILE
        try:
            temp_file = Path(self.id().replace(".", "_") + ".json")
            temp_file.write_text(
                json.dumps(
                    {
                        "base_url": "http://localhost:8080",
                        "username": "alice",
                        "password": "secret",
                    }
                ),
                encoding="utf-8",
            )
            console.CONFIG_FILE = temp_file
            credentials = console.load_credentials()
        finally:
            console.CONFIG_FILE = original
            temp_file.unlink(missing_ok=True)
        assert credentials is not None
        self.assertEqual(credentials.playback_backend, "local")
        self.assertEqual(credentials.bridge_url, "")
        self.assertEqual(credentials.bridge_stop_url, "")

    def test_audio_bridge_posts_standard_play_and_stop_payloads(self):
        BridgeHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            backend = console.AudioBridgePlaybackBackend(f"{base}/play")
            session = backend.start(
                item={"id": 7, "title": "Example", "media_kind": "audio"},
                stream_url="http://getoffline.local/api/stream/7",
                auth_header="Basic abc123",
                seek=42.5,
            )
            backend.stop(session)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(session.session_id, "session-123")
        self.assertEqual(BridgeHandler.requests[0][0], "/play")
        self.assertEqual(
            BridgeHandler.requests[0][1],
            {
                "url": "http://getoffline.local/api/stream/7",
                "headers": {"Authorization": "Basic abc123"},
                "seek_seconds": 42.5,
                "title": "Example",
                "media_kind": "audio",
                "episode_id": 7,
            },
        )
        self.assertEqual(BridgeHandler.requests[1], ("/stop", {"session_id": "session-123"}))


if __name__ == "__main__":
    unittest.main()
