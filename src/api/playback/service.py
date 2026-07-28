"""Playback progress service independent from UI clients."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from models.models import Download
from shared.schemas.media import PlaybackState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlaybackUpdate:
    position: float
    reason: str
    completed: bool
    listened_delta: float


def build_update(
    position_seconds: object,
    reason_value: object,
    item: Download,
) -> PlaybackUpdate | None:
    try:
        position = max(0.0, float(str(position_seconds or 0.0)))
    except (TypeError, ValueError):
        return None
    reason = str(reason_value or "").strip().lower()
    completed = reason in {"ended", "mini-ended", "complete", "completed"}
    previous_position = float(item.last_position_seconds or 0.0)
    return PlaybackUpdate(
        position=position,
        reason=reason,
        completed=completed,
        listened_delta=max(0.0, position - previous_position),
    )


def apply_update(
    item: Download, update: PlaybackUpdate, *, now: object = None
) -> PlaybackState:
    now = now or timezone.now()
    item.last_position_seconds = 0.0 if update.completed else update.position
    item.total_listened_seconds = (
        float(item.total_listened_seconds or 0.0) + update.listened_delta
    )
    item.last_position_updated_at = now
    item.last_seen_at = now
    update_fields = [
        "last_position_seconds",
        "total_listened_seconds",
        "last_position_updated_at",
        "last_seen_at",
    ]
    if update.completed:
        item.played = True
        item.played_at = now
        update_fields.extend(["played", "played_at"])
    log.info(
        "playback progress download_id=%s position=%.3f reason=%s completed=%s delta=%.3f",
        item.id,
        update.position,
        update.reason or "unknown",
        update.completed,
        update.listened_delta,
    )
    item.save(update_fields=update_fields)
    return PlaybackState(
        item.id,
        float(item.last_position_seconds or 0.0),
        bool(item.played),
        float(item.total_listened_seconds or 0.0),
    )


def start(item: Download) -> PlaybackState:
    return PlaybackState(
        item.id,
        float(item.last_position_seconds or 0.0),
        bool(item.played),
        float(item.total_listened_seconds or 0.0),
    )
