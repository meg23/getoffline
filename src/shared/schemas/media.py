"""Stable JSON DTO helpers shared by HTTP clients."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EpisodeSummary:
    id: int
    title: str
    source_name: str
    source_type: str
    description: str
    duration_seconds: float | None
    played: bool
    favorite: bool
    last_position_seconds: float
    total_listened_seconds: float
    download_status: str
    media_url: str
    stream_url: str
    subtitles_url: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PlaybackState:
    episode_id: int
    position_seconds: float
    played: bool
    total_listened_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadStatusDTO:
    id: int
    title: str
    status: str
    source_type: str
    source_name: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
