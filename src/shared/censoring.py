"""Stable video-censorship policy values shared by API and workers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CensorPolicySnapshot:
    enabled: bool = False
    method: str = "duck"
    keep_original: bool = False
    padding_ms: int = 150
    redact_transcript: bool = True

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> CensorPolicySnapshot:
        data: Mapping[str, object] = value if isinstance(value, dict) else {}
        method = str(data.get("method") or "duck").strip().lower()
        try:
            padding_ms = int(str(data.get("padding_ms", 150)))
        except (TypeError, ValueError):
            padding_ms = 150
        return cls(
            enabled=_enabled(data.get("enabled", False)),
            method=method if method in {"duck", "beep"} else "duck",
            keep_original=_enabled(data.get("keep_original", False)),
            padding_ms=max(0, min(1000, padding_ms)),
            redact_transcript=_enabled(data.get("redact_transcript", True)),
        )


def profile_censor_policy(settings: Mapping[str, object]) -> CensorPolicySnapshot:
    method = str(settings.get("video_censor_method") or "duck").strip().lower()
    try:
        raw_padding = settings.get("video_censor_padding_ms")
        padding_ms = int(str(raw_padding or 150))
    except (TypeError, ValueError):
        padding_ms = 150
    return CensorPolicySnapshot(
        enabled=_enabled(settings.get("video_censor_enabled")),
        method=method if method in {"duck", "beep"} else "duck",
        keep_original=_enabled(settings.get("video_censor_keep_original")),
        padding_ms=max(0, min(1000, padding_ms)),
    )


def source_censor_policy(
    settings: Mapping[str, object],
    *,
    policy: object,
    legacy_enabled: bool,
    method: object,
    keep_original: bool,
) -> CensorPolicySnapshot:
    profile_policy = profile_censor_policy(settings)
    normalized = str(policy or "inherit").strip().lower()
    if normalized == "disabled":
        return CensorPolicySnapshot(enabled=False)
    if normalized == "enabled" or legacy_enabled:
        source_method = str(method or "duck").strip().lower()
        return CensorPolicySnapshot(
            enabled=True,
            method=source_method if source_method in {"duck", "beep"} else "duck",
            keep_original=_enabled(keep_original),
            padding_ms=profile_policy.padding_ms,
        )
    return profile_policy
