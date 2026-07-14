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
    def test_episode_row_uses_stable_columns_and_truncates_long_titles(self):
        row = console.format_episode_row(
            played="new",
            source="Very Long Source Name",
            title="An Extremely Long Episode Title That Should Not Push Columns Around",
            width=48,
        )

        self.assertEqual(len(row), 48)
        self.assertTrue(row.startswith(" new     "))
        self.assertIn("Very Long S…", row)
        self.assertTrue(row.endswith("…"))

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


class FakeClient:
    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, float, str]] = []
        self.refreshed = False

    def playback_progress(self, episode_id: int, position_seconds: float, *, reason: str = "timeupdate") -> dict[str, Any]:
        self.progress_calls.append((episode_id, position_seconds, reason))
        return {"ok": True}

    def frontend_library(self, *, filter_mode: str = "") -> dict[str, Any]:
        self.refreshed = True
        return {"downloads": [], "jobs": []}


class StoppedPlaybackBackend:
    available = True
    unavailable_message = ""

    def is_running(self, session: console.PlaybackSession) -> bool:
        return False


class ConsoleProgressTests(unittest.TestCase):
    def test_reaping_closed_player_preserves_resume_position(self):
        app = console.GetOfflineConsole.__new__(console.GetOfflineConsole)
        app.client = FakeClient()
        app.playback = StoppedPlaybackBackend()
        app.playback_session = console.PlaybackSession(process=None)
        app.playing_id = 42
        app.play_start_position = 120.0
        app.play_started_at = console.time.monotonic() - 30.0
        app.filter_mode = "unplayed"
        app.episodes = []
        app.jobs = []
        app.selected = 0
        app.message = "Playing"

        app._reap_player()

        self.assertIsNone(app.playback_session)
        self.assertIsNone(app.playing_id)
        self.assertEqual(app.client.progress_calls[0][0], 42)
        self.assertGreaterEqual(app.client.progress_calls[0][1], 149.0)
        self.assertEqual(app.client.progress_calls[0][2], "stopped")
        self.assertEqual(app.message, "Loaded 0 unplayed item(s)")

    def test_local_player_detection_prefers_vlc_before_ffplay(self):
        self.assertGreater(
            console.PLAYER_CANDIDATES.index("ffplay"),
            console.PLAYER_CANDIDATES.index("vlc"),
        )


if __name__ == "__main__":
    unittest.main()
