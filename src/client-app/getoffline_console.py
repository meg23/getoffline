#!/usr/bin/env python3
# ruff: noqa: E402
"""console music-player-style ncurses client for GetOffline.

The app intentionally keeps dependencies to the Python standard library plus the
GetOffline SDK. Playback is delegated to a console-friendly media player such as
mpv, ffplay, or vlc so terminal rendering remains responsive while the API state
is updated through the SDK.
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
PLAYER_CANDIDATES = ("mpv", "ffplay", "cvlc", "vlc")
PROGRESS_INTERVAL_SECONDS = 5.0


@dataclass
class Credentials:
    base_url: str
    username: str
    password: str

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
    def __init__(self, credentials: Credentials, player: str | None = None) -> None:
        self.credentials = credentials
        self.client = GetOfflineClient(AuthenticatedTransport(credentials))
        self.player = player or detect_player()
        self.filter_mode = "unplayed"
        self.episodes: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.selected = 0
        self.offset = 0
        self.message = "Ready"
        self.playing_id: int | None = None
        self.play_started_at = 0.0
        self.play_start_position = 0.0
        self.process: subprocess.Popen[bytes] | None = None
        self.last_progress_at = 0.0

    def refresh(self) -> None:
        payload = self.client.frontend_library(filter_mode=self.filter_mode)
        self.episodes = list(payload.get("downloads") or [])
        self.jobs = list(payload.get("jobs") or [])
        self.selected = min(self.selected, max(len(self.episodes) - 1, 0))
        self.message = f"Loaded {len(self.episodes)} {self.filter_mode} item(s)"

    def run(self, stdscr: Any) -> None:
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
                self.stop(reason="quit")
                return
            self._handle_key(stdscr, key)

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
        if not self.player:
            self.message = "No player found: install mpv, ffplay, or vlc"
            return
        self.stop(reason="switch")
        episode_id = int(episode["id"])
        player_payload = self.client.frontend_player(episode_id)
        item = dict(player_payload.get("item") or episode)
        seek = float(player_payload.get("seek_seconds") or item.get("last_position_seconds") or 0.0)
        self.client.playback_start(episode_id)
        url = self.stream_url(episode_id)
        command = player_command(self.player, url, self.credentials.auth_header, seek)
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.playing_id = episode_id
        self.play_start_position = seek
        self.play_started_at = time.monotonic()
        self.last_progress_at = 0.0
        self.message = f"Playing: {item.get('title') or episode.get('title') or episode_id}"

    def stop(self, *, reason: str) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._save_progress(reason)
        self.process = None
        self.playing_id = None

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
        if self.playing_id is None or self.process is None or self.process.poll() is not None:
            return
        now = time.monotonic()
        if now - self.last_progress_at >= PROGRESS_INTERVAL_SECONDS:
            self.last_progress_at = now
            self._save_progress("timeupdate")

    def _reap_player(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self._save_progress("ended")
            self.message = "Playback ended"
            self.process = None
            self.playing_id = None
            self.refresh()

    def _draw(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        header = f" GetOffline Console | {self.credentials.username}@{self.credentials.base_url} | filter: {self.filter_mode} "
        safe_addnstr(stdscr, 0, 0, header, curses.A_REVERSE)
        help_text = "↑/↓ j/k move  Enter/p play  s stop  / search  a add  m/u played  f fav  1-4 filters  r refresh  q quit"
        safe_addnstr(stdscr, 1, 0, help_text, curses.A_DIM)
        list_top = 3
        list_height = max(height - 7, 1)
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
            status = "♥" if episode.get("favorite") else " "
            played = "P" if episode.get("played") else "N"
            marker = "▶" if self.playing_id == episode.get("id") else " "
            line = f"{marker}{status} {played} {source[:24]:24} {title}"
            attr = curses.A_REVERSE if absolute == self.selected else curses.A_NORMAL
            safe_addnstr(stdscr, row, 0, line, attr)
        footer = f" {self.message} "
        if self.playing_id:
            footer += f"| position ~{int(self.estimated_position())}s "
        safe_addnstr(stdscr, height - 2, 0, footer, curses.A_REVERSE)
        safe_addnstr(stdscr, height - 1, 0, f" Jobs: {format_jobs(self.jobs)}", curses.A_DIM)
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


def detect_player() -> str | None:
    return next((name for name in PLAYER_CANDIDATES if shutil.which(name)), None)


def player_command(player: str, url: str, auth_header: str, seek: float) -> list[str]:
    if player == "mpv":
        return ["mpv", "--no-video", f"--start={seek:.3f}", f"--http-header-fields=Authorization: {auth_header}", url]
    if player == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-ss", f"{seek:.3f}", "-headers", f"Authorization: {auth_header}\r\n", url]
    if player in {"vlc", "cvlc"}:
        return [player, "--intf", "ncurses", f"--start-time={int(seek)}", f"--http-header=Authorization: {auth_header}", url]
    return [player, url]


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
    return Credentials(base_url=str(data["base_url"]), username=str(data["username"]), password=str(data["password"]))


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
    parser.add_argument("--player", help="media player command to use (default: auto-detect mpv/ffplay/vlc)")
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = parse_args()
    credentials = login(args.base_url, args.username) if args.login else load_credentials()
    if credentials is None:
        credentials = login(args.base_url, args.username)
    app = GetOfflineConsole(credentials, player=args.player)
    curses.wrapper(app.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
