from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "client-app" / "app.py"
spec = importlib.util.spec_from_file_location("client_app", MODULE_PATH)
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

    def test_default_bridge_volume_url_uses_sibling_volume_endpoint(self):
        self.assertEqual(
            console.default_bridge_volume_url("http://bridge.local/play"),
            "http://bridge.local/volume",
        )
        self.assertEqual(
            console.default_bridge_volume_url("http://bridge.local/api/audio"),
            "http://bridge.local/api/audio/volume",
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
                volume=0.75,
            )
            backend.set_volume(session, 0.5)
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
                "volume": 0.75,
            },
        )
        self.assertEqual(BridgeHandler.requests[1], ("/volume", {"session_id": "session-123", "volume": 0.5}))
        self.assertEqual(BridgeHandler.requests[2], ("/stop", {"session_id": "session-123"}))

    def test_adjust_volume_clamps_and_posts_for_active_session(self):
        app = console.GetOfflineConsole.__new__(console.GetOfflineConsole)
        app.volume = 0.95
        app.message = ""
        app.playback_session = console.PlaybackSession(session_id="session-123")

        class VolumeBackend:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float]] = []

            def set_volume(self, session: console.PlaybackSession, volume: float) -> None:
                self.calls.append((session.session_id, volume))

        app.playback = VolumeBackend()

        app.adjust_volume(0.1)

        self.assertEqual(app.volume, 1.0)
        self.assertEqual(app.playback.calls, [("session-123", 1.0)])
        self.assertEqual(app.message, "Volume: 100%")


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

    def __init__(self) -> None:
        self.stopped_sessions: list[console.PlaybackSession] = []

    def stop(self, session: console.PlaybackSession) -> None:
        self.stopped_sessions.append(session)
        session.active = False

    def is_running(self, session: console.PlaybackSession) -> bool:
        return False


class RunningPlaybackBackend:
    available = True
    unavailable_message = ""

    def __init__(self) -> None:
        self.stopped_sessions: list[console.PlaybackSession] = []

    def stop(self, session: console.PlaybackSession) -> None:
        self.stopped_sessions.append(session)
        session.active = False

    def is_running(self, session: console.PlaybackSession) -> bool:
        return session.active


class QuitWindow:
    def nodelay(self, value: bool) -> None:
        self.nodelay_value = value

    def timeout(self, value: int) -> None:
        self.timeout_value = value

    def getmaxyx(self) -> tuple[int, int]:
        return (24, 80)

    def erase(self) -> None:
        return

    def refresh(self) -> None:
        return

    def addnstr(self, *_args: Any) -> None:
        return

    def getch(self) -> int:
        return ord("q")


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

    def test_local_player_detection_only_uses_ffplay(self):
        self.assertEqual(console.PLAYER_CANDIDATES, ("ffplay",))

    def test_ffplay_command_gets_headers_seek_and_initial_volume(self):
        command = console.player_command(
            "ffplay",
            "http://getoffline.local/api/stream/7",
            "Basic abc123",
            42.5,
            0.75,
        )

        self.assertEqual(command[0], "ffplay")
        self.assertIn("-nodisp", command)
        self.assertIn("-autoexit", command)
        self.assertIn("-ss", command)
        self.assertIn("42.500", command)
        self.assertIn("-volume", command)
        self.assertIn("75", command)
        self.assertIn("-headers", command)
        self.assertIn("Authorization: Basic abc123\r\n", command)
        self.assertEqual(command[-1], "http://getoffline.local/api/stream/7")

    def test_shutdown_stops_active_playback_and_saves_quit_progress_once(self):
        app = console.GetOfflineConsole.__new__(console.GetOfflineConsole)
        app.client = FakeClient()
        app.playback = RunningPlaybackBackend()
        session = console.PlaybackSession(process=None)
        app.playback_session = session
        app.playing_id = 42
        app.play_start_position = 120.0
        app.play_started_at = console.time.monotonic()
        app._shut_down = False

        app.shutdown()
        app.shutdown()

        self.assertEqual(app.playback.stopped_sessions, [session])
        self.assertIsNone(app.playback_session)
        self.assertIsNone(app.playing_id)
        self.assertEqual(len(app.client.progress_calls), 1)
        self.assertEqual(app.client.progress_calls[0][0], 42)
        self.assertEqual(app.client.progress_calls[0][2], "quit")

    def test_run_stops_active_playback_when_quitting(self):
        app = console.GetOfflineConsole.__new__(console.GetOfflineConsole)
        app.credentials = console.Credentials("http://example.test", "alice", "secret")
        app.client = FakeClient()
        app.playback = RunningPlaybackBackend()
        session = console.PlaybackSession(process=None)
        app.playback_session = session
        app.playing_id = 42
        app.play_start_position = 120.0
        app.play_started_at = console.time.monotonic()
        app.last_progress_at = console.time.monotonic()
        app.filter_mode = "unplayed"
        app.episodes = []
        app.jobs = []
        app.selected = 0
        app.offset = 0
        app.message = "Playing"
        app._shut_down = False
        app.volume = 1.0

        original_curs_set = console.curses.curs_set
        try:
            console.curses.curs_set = lambda _value: None
            app.run(QuitWindow())
        finally:
            console.curses.curs_set = original_curs_set

        self.assertEqual(app.playback.stopped_sessions, [session])
        self.assertIsNone(app.playback_session)
        self.assertIsNone(app.playing_id)
        self.assertEqual(app.client.progress_calls[0][2], "quit")


if __name__ == "__main__":
    unittest.main()
