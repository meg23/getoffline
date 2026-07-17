#!/usr/bin/env python3
# ruff: noqa: E402
"""console music-player-style ncurses client for GetOffline.

The app intentionally keeps dependencies to the Python standard library plus the
GetOffline SDK. Playback is delegated to a console-friendly media player or a generic HTTP
audio bridge so terminal rendering remains responsive while the API state is
updated through the SDK.
"""

from __future__ import annotations

import argparse
import base64
import curses
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

REPO_SRC = Path(__file__).resolve().parents[1]
SDK_SRC = REPO_SRC / "packages"
for import_path in (str(SDK_SRC), str(REPO_SRC)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from getoffline_sdk import GetOfflineClient, HttpTransport, Response

APP_NAME = "getoffline-console"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "credentials.json"
PLAYER_CANDIDATES = ("ffplay",)
PROGRESS_INTERVAL_SECONDS = 5.0
DEFAULT_PLAYBACK_BACKEND = "local"
BRIDGE_TIMEOUT_SECONDS = 10.0


@dataclass
class Credentials:
    base_url: str
    username: str
    password: str
    playback_backend: str = DEFAULT_PLAYBACK_BACKEND
    bridge_url: str = ""
    bridge_stop_url: str = ""
    bridge_volume_url: str = ""
    volume: float = 1.0

    @property
    def api_url(self) -> str:
        return self.base_url.rstrip("/") + "/api"

    @property
    def auth_header(self) -> str:
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


class AuthenticatedTransport:
    """HTTP transport that attaches Basic auth to every SDK request."""

    def __init__(self, credentials: Credentials, *, timeout_seconds: float = 30.0) -> None:
        self.transport = HttpTransport(credentials.api_url, timeout_seconds=timeout_seconds)
        self.credentials = credentials

    def request(
        self,
        method: str,
        target: str,
        args: tuple[object, ...] = (),
        *,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        merged = {"Authorization": self.credentials.auth_header, **(headers or {})}
        return self.transport.request(method, target, args, query=query, data=data, headers=merged)


class GetOfflineConsole:
    def __init__(
        self,
        credentials: Credentials,
        player: str | None = None,
        playback_backend: str | None = None,
        bridge_url: str | None = None,
        bridge_stop_url: str | None = None,
        bridge_volume_url: str | None = None,
        volume: float | None = None,
    ) -> None:
        self.credentials = credentials
        self.client = GetOfflineClient(AuthenticatedTransport(credentials))
        self.playback = build_playback_backend(
            playback_backend or credentials.playback_backend,
            player=player,
            bridge_url=bridge_url or credentials.bridge_url,
            bridge_stop_url=bridge_stop_url or credentials.bridge_stop_url,
            bridge_volume_url=bridge_volume_url or credentials.bridge_volume_url,
        )
        self.volume = clamp_volume(credentials.volume if volume is None else volume)
        self.filter_mode = "unplayed"
        self.episodes: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.selected = 0
        self.offset = 0
        self.message = "Ready"
        self.playing_id: int | None = None
        self.play_started_at = 0.0
        self.play_start_position = 0.0
        self.playback_session: PlaybackSession | None = None
        self.last_progress_at = 0.0
        self._shut_down = False

    def refresh(self) -> None:
        payload = self.client.frontend_library(filter_mode=self.filter_mode)
        self.episodes = list(payload.get("downloads") or [])
        self.jobs = list(payload.get("jobs") or [])
        self.selected = min(self.selected, max(len(self.episodes) - 1, 0))
        self.message = f"Loaded {len(self.episodes)} {self.filter_mode} item(s)"

    def run(self, stdscr: Any) -> None:
        try:
            curses.curs_set(0)
            stdscr.nodelay(True)
            stdscr.timeout(200)
            self.refresh()
            while True:
                self._reap_player()
                self._send_periodic_progress()
                self._draw(stdscr)
                key = stdscr.getch()
                if key == -1:
                    continue
                if key in (ord("q"), 27):
                    return
                self._handle_key(stdscr, key)
        finally:
            self.shutdown()

    def _handle_key(self, stdscr: Any, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(max(len(self.episodes) - 1, 0), self.selected + 1)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.selected = min(max(len(self.episodes) - 1, 0), self.selected + 10)
        elif key == curses.KEY_PPAGE:
            self.selected = max(0, self.selected - 10)
        elif key in (ord("\n"), curses.KEY_ENTER, ord("p")):
            self.play_selected()
        elif key == ord("s"):
            self.stop(reason="stop")
        elif key in (ord("-"), ord("_")):
            self.adjust_volume(-0.1)
        elif key in (ord("+"), ord("=")):
            self.adjust_volume(0.1)
        elif key == ord("r"):
            self.refresh()
        elif key == ord("/"):
            self.search(stdscr)
        elif key == ord("a"):
            self.add_download(stdscr)
        elif key == ord("m"):
            self.mark_selected(True)
        elif key == ord("u"):
            self.mark_selected(False)
        elif key == ord("f"):
            self.toggle_favorite()
        elif key == ord("1"):
            self.set_filter("unplayed")
        elif key == ord("2"):
            self.set_filter("played")
        elif key == ord("3"):
            self.set_filter("favorites")
        elif key == ord("4"):
            self.set_filter("all")

    def current_episode(self) -> dict[str, Any] | None:
        if not self.episodes:
            return None
        return self.episodes[self.selected]

    def play_selected(self) -> None:
        episode = self.current_episode()
        if not episode:
            self.message = "No episode selected"
            return
        if not self.playback.available:
            self.message = self.playback.unavailable_message
            return
        self.stop(reason="switch")
        episode_id = int(episode["id"])
        player_payload = self.client.frontend_player(episode_id)
        item = dict(player_payload.get("item") or episode)
        seek = float(player_payload.get("seek_seconds") or item.get("last_position_seconds") or 0.0)
        self.client.playback_start(episode_id)
        url = self.stream_url(episode_id)
        try:
            self.playback_session = self.playback.start(
                item=item,
                stream_url=url,
                auth_header=self.credentials.auth_header,
                seek=seek,
                volume=self.volume,
            )
        except PlaybackError as exc:
            self.message = f"Playback failed: {exc}"
            return
        self.playing_id = episode_id
        self.play_start_position = seek
        self.play_started_at = time.monotonic()
        self.last_progress_at = 0.0
        self.message = f"Playing: {item.get('title') or episode.get('title') or episode_id}"

    def adjust_volume(self, delta: float) -> None:
        previous = self.volume
        self.volume = clamp_volume(self.volume + delta)
        if self.playback_session is not None:
            try:
                self.playback.set_volume(self.playback_session, self.volume)
            except PlaybackError as exc:
                self.volume = previous
                self.message = f"Volume failed: {exc}"
                return
        self.message = f"Volume: {format_volume(self.volume)}"

    def stop(self, *, reason: str) -> None:
        if self.playback_session is not None:
            self.playback.stop(self.playback_session)
        self._save_progress(reason)
        self.playback_session = None
        self.playing_id = None

    def shutdown(self) -> None:
        """Stop any active playback before the console app exits."""

        if self._shut_down:
            return
        self._shut_down = True
        self.stop(reason="quit")

    def mark_selected(self, played: bool) -> None:
        episode = self.current_episode()
        if not episode:
            return
        target = "/dashboard/downloads/{}/{}".format(episode["id"], "played" if played else "unplayed")
        response = self.client.raw_request("POST", target)
        self.message = "Updated playback status" if response.ok else f"Update failed ({response.status_code})"
        self.refresh()

    def toggle_favorite(self) -> None:
        episode = self.current_episode()
        if not episode:
            return
        action = "unfavorite" if episode.get("favorite") else "favorite"
        response = self.client.raw_request("POST", f"/dashboard/downloads/{episode['id']}/{action}")
        self.message = "Updated favorite" if response.ok else f"Favorite failed ({response.status_code})"
        self.refresh()

    def add_download(self, stdscr: Any) -> None:
        url = prompt(stdscr, "YouTube/media URL")
        if not url:
            return
        payload = self.client.download(url)
        self.message = "Queued download" if payload.get("ok") else f"Queue failed: {payload.get('error', 'unknown')}"
        self.refresh()

    def search(self, stdscr: Any) -> None:
        query = prompt(stdscr, "Search transcripts/library")
        if not query:
            return
        payload = self.client.search(query)
        self.episodes = list(payload.get("results") or [])
        self.selected = 0
        self.filter_mode = "search"
        self.message = f"Search returned {len(self.episodes)} item(s)"

    def set_filter(self, value: str) -> None:
        self.filter_mode = value
        self.refresh()

    def stream_url(self, episode_id: int) -> str:
        return f"{self.credentials.api_url}/stream/{urllib.parse.quote(str(episode_id))}"

    def estimated_position(self) -> float:
        if self.playing_id is None:
            return 0.0
        return self.play_start_position + max(time.monotonic() - self.play_started_at, 0.0)

    def _save_progress(self, reason: str) -> None:
        if self.playing_id is None:
            return
        self.client.playback_progress(self.playing_id, self.estimated_position(), reason=reason)

    def _send_periodic_progress(self) -> None:
        if (
            self.playing_id is None
            or self.playback_session is None
            or not self.playback.is_running(self.playback_session)
        ):
            return
        now = time.monotonic()
        if now - self.last_progress_at >= PROGRESS_INTERVAL_SECONDS:
            self.last_progress_at = now
            self._save_progress("timeupdate")

    def _reap_player(self) -> None:
        if self.playback_session is not None and not self.playback.is_running(self.playback_session):
            # A local player process can disappear because the user closed the
            # player window/terminal controls, not only because media reached
            # EOF.  Saving this as ``ended`` clears the resume position on the
            # server, so treat unexpected process exits as a stopped session and
            # preserve the estimated resume point.
            self._save_progress("stopped")
            self.playback.stop(self.playback_session)
            self.message = "Playback stopped"
            self.playback_session = None
            self.playing_id = None
            self.refresh()

    def _draw(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        header = f" getoffline | {self.credentials.username}@{self.credentials.base_url} | filter: {self.filter_mode} "
        safe_addnstr(stdscr, 0, 0, header, curses.A_REVERSE)
        controls = " Move: j/k  Play: Enter/p  Stop: s  Vol: -/+  Search: /  Add: a  Favorite: f  Quit: q "
        filters = " Mark: m played, u unplayed  Filters: 1 unplayed, 2 played, 3 favorites, 4 all  Refresh: r "
        safe_addnstr(stdscr, 1, 0, controls, curses.A_BOLD)
        safe_addnstr(stdscr, 2, 0, filters, curses.A_DIM)
        list_top = 4
        list_height = max(height - 6, 1)
        if self.selected < self.offset:
            self.offset = self.selected
        if self.selected >= self.offset + list_height:
            self.offset = self.selected - list_height + 1
        visible = self.episodes[self.offset : self.offset + list_height]
        for idx, episode in enumerate(visible):
            row = list_top + idx
            absolute = self.offset + idx
            title = str(episode.get("title") or "Untitled")
            source = str(episode.get("source_name") or episode.get("source_type") or "")
            played = "played" if episode.get("played") else "new"
            line = format_episode_row(
                played=played,
                source=source,
                title=title,
                width=width,
            )
            attr = curses.A_REVERSE if absolute == self.selected else curses.A_NORMAL
            safe_addnstr(stdscr, row, 0, line, attr)
        footer = f" {self.message} | volume {format_volume(self.volume)} "
        if self.playing_id:
            footer += f"| position ~{int(self.estimated_position())}s "
        safe_addnstr(stdscr, height - 1, 0, footer, curses.A_REVERSE)
        stdscr.refresh()


def safe_addnstr(window: Any, y: int, x: int, text: str, attr: int = curses.A_NORMAL) -> None:
    """Draw text without failing on curses implementations that reject bottom-right writes."""

    height, width = window.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = max(width - x - 1, 0)
    if available <= 0:
        return
    try:
        window.addnstr(y, x, text.ljust(available), available, attr)
    except curses.error:
        # Some terminals return ERR when drawing near the lower-right cell even
        # when truncating. Leave that cell blank rather than crashing the UI.
        return


def format_episode_row(
    *,
    played: str,
    source: str,
    title: str,
    width: int,
) -> str:
    """Format one library row with stable columns that fit the terminal."""

    chrome_width = 1 + 8 + 1 + 2  # left inset, played column, source/title gap.
    source_width = min(24, max(10, width // 4))
    title_width = max(width - chrome_width - source_width, 10)
    clean_source = " ".join(source.split())
    clean_title = " ".join(title.split())
    return (
        f" {played[:8]:8} "
        f"{truncate(clean_source, source_width):{source_width}}  "
        f"{truncate(clean_title, title_width)}"
    )


def truncate(value: str, max_width: int) -> str:
    if len(value) <= max_width:
        return value
    if max_width <= 1:
        return value[:max_width]
    return value[: max_width - 1] + "…"


def detect_player() -> str | None:
    return next((name for name in PLAYER_CANDIDATES if shutil.which(name)), None)


def player_name(player: str) -> str:
    return Path(player).name.lower()


def clamp_volume(volume: float) -> float:
    return max(0.0, min(float(volume), 1.0))


def format_volume(volume: float) -> str:
    return f"{int(round(clamp_volume(volume) * 100))}%"


class PlaybackError(RuntimeError):
    """Raised when a playback backend cannot start or stop playback."""


@dataclass
class PlaybackSession:
    process: subprocess.Popen[bytes] | None = None
    session_id: str = ""
    active: bool = True
    volume: float = 1.0


class LocalProcessPlaybackBackend:
    def __init__(self, player: str | None = None) -> None:
        self.player = player or detect_player()

    @property
    def available(self) -> bool:
        return bool(self.player)

    @property
    def unavailable_message(self) -> str:
        return "No player found: install ffplay"

    def start(
        self,
        *,
        item: Mapping[str, Any],
        stream_url: str,
        auth_header: str,
        seek: float,
        volume: float = 1.0,
    ) -> PlaybackSession:
        del item
        if not self.player:
            raise PlaybackError(self.unavailable_message)
        command = player_command(self.player, stream_url, auth_header, seek, volume)
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "posix":
            # Place the player in its own process group so stopping the console
            # also stops any helper processes spawned by the player.
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            raise PlaybackError(f"unable to launch player {self.player!r}: {exc}") from exc
        return PlaybackSession(process=process, volume=clamp_volume(volume))

    def set_volume(self, session: PlaybackSession, volume: float) -> None:
        session.volume = clamp_volume(volume)

    def stop(self, session: PlaybackSession) -> None:
        if session.process and session.process.poll() is None:
            self._terminate_process(session.process)
            try:
                session.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill_process(session.process)
                session.process.wait(timeout=3)
        session.active = False

    def is_running(self, session: PlaybackSession) -> bool:
        return bool(session.active and session.process is not None and session.process.poll() is None)

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        process.terminate()

    def _kill_process(self, process: subprocess.Popen[bytes]) -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        process.kill()


class AudioBridgePlaybackBackend:
    """Generic HTTP audio bridge backend.

    The bridge receives the authenticated GetOffline stream URL and decides how
    to present playback to its downstream device. The console client does not
    know what type of device is behind the bridge.
    """

    def __init__(
        self,
        bridge_url: str,
        *,
        bridge_stop_url: str = "",
        bridge_volume_url: str = "",
        timeout_seconds: float = BRIDGE_TIMEOUT_SECONDS,
    ) -> None:
        self.bridge_url = bridge_url.strip()
        self.bridge_stop_url = bridge_stop_url.strip()
        self.bridge_volume_url = bridge_volume_url.strip()
        self.timeout_seconds = timeout_seconds

    @property
    def available(self) -> bool:
        return bool(self.bridge_url)

    @property
    def unavailable_message(self) -> str:
        return "Audio bridge is not configured: pass --bridge-url"

    def start(
        self,
        *,
        item: Mapping[str, Any],
        stream_url: str,
        auth_header: str,
        seek: float,
        volume: float = 1.0,
    ) -> PlaybackSession:
        if not self.bridge_url:
            raise PlaybackError(self.unavailable_message)
        payload = {
            "url": stream_url,
            "headers": {"Authorization": auth_header},
            "seek_seconds": seek,
            "title": str(item.get("title") or ""),
            "media_kind": str(item.get("media_kind") or item.get("display_kind") or "audio"),
            "episode_id": item.get("id"),
            "volume": clamp_volume(volume),
        }
        response = self._post_json(self.bridge_url, payload)
        session_id = str(response.get("session_id") or "")
        return PlaybackSession(session_id=session_id, volume=clamp_volume(volume))

    def set_volume(self, session: PlaybackSession, volume: float) -> None:
        session.volume = clamp_volume(volume)
        volume_url = self.bridge_volume_url or default_bridge_volume_url(self.bridge_url)
        self._post_json(volume_url, {"session_id": session.session_id, "volume": session.volume})

    def stop(self, session: PlaybackSession) -> None:
        if not session.active:
            return
        stop_url = self.bridge_stop_url or default_bridge_stop_url(self.bridge_url)
        try:
            self._post_json(stop_url, {"session_id": session.session_id})
        except PlaybackError:
            # The bridge may intentionally be fire-and-forget. Local progress is
            # still saved even if the bridge does not expose a working stop URL.
            pass
        session.active = False

    def is_running(self, session: PlaybackSession) -> bool:
        return session.active

    def _post_json(self, url: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(  # nosec B310 - bridge URL is user configured.
                request, timeout=self.timeout_seconds
            )
            content = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise PlaybackError(f"bridge returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PlaybackError(f"bridge request failed: {exc.reason}") from exc
        if not content:
            return {}
        decoded = json.loads(content.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}


def default_bridge_stop_url(bridge_url: str) -> str:
    parsed = urllib.parse.urlsplit(bridge_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/play"):
        path = path[: -len("/play")] + "/stop"
    else:
        path = f"{path}/stop"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path or "/stop", parsed.query, parsed.fragment))


def default_bridge_volume_url(bridge_url: str) -> str:
    parsed = urllib.parse.urlsplit(bridge_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/play"):
        path = path[: -len("/play")] + "/volume"
    else:
        path = f"{path}/volume"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path or "/volume", parsed.query, parsed.fragment))


def build_playback_backend(
    playback_backend: str,
    *,
    player: str | None = None,
    bridge_url: str = "",
    bridge_stop_url: str = "",
    bridge_volume_url: str = "",
) -> LocalProcessPlaybackBackend | AudioBridgePlaybackBackend:
    normalized = (playback_backend or DEFAULT_PLAYBACK_BACKEND).strip().lower()
    if normalized == "bridge":
        return AudioBridgePlaybackBackend(bridge_url, bridge_stop_url=bridge_stop_url, bridge_volume_url=bridge_volume_url)
    if normalized == "local":
        return LocalProcessPlaybackBackend(player)
    raise SystemExit(f"Unsupported playback backend: {playback_backend!r}. Expected 'local' or 'bridge'.")


def player_command(player: str, url: str, auth_header: str, seek: float, volume: float = 1.0) -> list[str]:
    if player_name(player) != "ffplay":
        return [player, url]
    return [
        player,
        "-nodisp",
        "-autoexit",
        "-ss",
        f"{seek:.3f}",
        "-volume",
        str(int(round(clamp_volume(volume) * 100))),
        "-headers",
        f"Authorization: {auth_header}\r\n",
        url,
    ]


def format_jobs(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "none"
    return ", ".join(f"{job.get('job_type')}:{job.get('status')}" for job in jobs[:3])


def prompt(stdscr: Any, label: str) -> str:
    curses.echo()
    height, width = stdscr.getmaxyx()
    safe_addnstr(stdscr, height - 1, 0, label + ": ", curses.A_REVERSE)
    stdscr.refresh()
    value = stdscr.getstr(height - 1, len(label) + 2, max(width - len(label) - 3, 1))
    curses.noecho()
    return cast(str, value.decode("utf-8")).strip()


def load_credentials() -> Credentials | None:
    if not CONFIG_FILE.exists():
        return None
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return Credentials(
        base_url=str(data["base_url"]),
        username=str(data["username"]),
        password=str(data["password"]),
        playback_backend=str(data.get("playback_backend") or DEFAULT_PLAYBACK_BACKEND),
        bridge_url=str(data.get("bridge_url") or ""),
        bridge_stop_url=str(data.get("bridge_stop_url") or ""),
        bridge_volume_url=str(data.get("bridge_volume_url") or ""),
        volume=clamp_volume(float(data.get("volume", 1.0))),
    )


def save_credentials(credentials: Credentials) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(credentials.__dict__, indent=2), encoding="utf-8")
    CONFIG_FILE.chmod(0o600)


def login(base_url: str | None = None, username: str | None = None) -> Credentials:
    print("GetOffline login")
    resolved_base = base_url or input("Base URL (for example http://localhost:8000): ").strip()
    resolved_user = username or input("Username: ").strip()
    password = getpass.getpass("Password: ")
    credentials = Credentials(resolved_base, resolved_user, password)
    client = GetOfflineClient(AuthenticatedTransport(credentials))
    payload = client.user()
    if not payload.get("user"):
        raise SystemExit("Login failed: API did not accept the supplied credentials")
    save_credentials(credentials)
    print(f"Saved credentials to {CONFIG_FILE}")
    return credentials


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="console music-player-style ncurses client for GetOffline")
    parser.add_argument("--login", action="store_true", help="prompt for credentials and store them locally")
    parser.add_argument("--base-url", help="GetOffline web base URL, without /api")
    parser.add_argument("--username", help="GetOffline username")
    parser.add_argument("--player", help="media player command to use (default: auto-detect ffplay)")
    parser.add_argument(
        "--playback-backend",
        choices=("local", "bridge"),
        help="playback backend to use (default: stored config or local)",
    )
    parser.add_argument("--bridge-url", help="generic audio bridge play endpoint URL")
    parser.add_argument("--bridge-stop-url", help="generic audio bridge stop endpoint URL")
    parser.add_argument("--bridge-volume-url", help="generic audio bridge volume endpoint URL")
    parser.add_argument("--volume", type=float, help="initial playback volume from 0.0 to 1.0")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = parse_args()
    credentials = login(args.base_url, args.username) if args.login else load_credentials()
    if credentials is None:
        credentials = login(args.base_url, args.username)
    app = GetOfflineConsole(
        credentials,
        player=args.player,
        playback_backend=args.playback_backend,
        bridge_url=args.bridge_url,
        bridge_stop_url=args.bridge_stop_url,
        bridge_volume_url=args.bridge_volume_url,
        volume=args.volume,
    )
    try:
        curses.wrapper(app.run)
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
