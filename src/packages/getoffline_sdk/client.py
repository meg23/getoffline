"""High-level client for GetOffline API operations."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from packages.getoffline_sdk.transports import Response, Transport


class GetOfflineClient:
    """Encapsulates common GetOffline API tasks behind one interface."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def raw_request(
        self,
        method: str,
        route_name: str,
        *args: object,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.transport.request(
            method, route_name, args, query=query, data=data, headers=headers
        )

    def json_request(
        self,
        method: str,
        route_name: str,
        *args: object,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.raw_request(
            method, route_name, *args, query=query, data=data, headers=headers
        )
        if response.status_code >= 400:
            return {}
        decoded = json.loads(response.content.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}

    def frontend_library(self, *, filter_mode: str = "") -> dict[str, Any]:
        return self.json_request(
            "GET", "api_frontend_library", query={"filter": filter_mode}
        )

    def frontend_jobs(self) -> dict[str, Any]:
        return self.json_request("GET", "api_frontend_jobs")

    def frontend_player(self, episode_id: int, *, start_seconds: str = "") -> dict[str, Any]:
        return self.json_request(
            "GET",
            "api_frontend_player",
            episode_id,
            query={"t": start_seconds},
        )

    def search(self, query: str) -> dict[str, Any]:
        return self.json_request("GET", "api_search", query={"q": query})

    def library(self, *, filter_mode: str = "") -> dict[str, Any]:
        return self.json_request("GET", "api_library", query={"filter": filter_mode})

    def history(self) -> dict[str, Any]:
        return self.json_request("GET", "api_history")

    def user(self) -> dict[str, Any]:
        return self.json_request("GET", "api_user")

    def csrf(self) -> dict[str, Any]:
        return self.json_request("GET", "api_csrf")

    def download(self, url: str, **options: object) -> dict[str, Any]:
        data = {"url": url, **options}
        return self.json_request("POST", "api_download", data=data)

    def playback_start(self, episode_id: int) -> dict[str, Any]:
        return self.json_request("POST", "api_playback_start", data={"episode_id": episode_id})

    def playback_progress(
        self, episode_id: int, position_seconds: float, *, reason: str = "timeupdate"
    ) -> dict[str, Any]:
        return self.json_request(
            "POST",
            "api_playback_progress",
            data={
                "episode_id": episode_id,
                "position_seconds": position_seconds,
                "reason": reason,
            },
        )

    def playback_complete(self, episode_id: int, position_seconds: float) -> dict[str, Any]:
        return self.json_request(
            "POST",
            "api_playback_complete",
            data={"episode_id": episode_id, "position_seconds": position_seconds},
        )
