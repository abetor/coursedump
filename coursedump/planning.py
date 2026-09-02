"""Shared read-only planning model for single-source and queue commands."""

from __future__ import annotations

from pathlib import Path

from . import archives, manifest, sources
from .util import ffprobe_duration


ASR_SPEED = 8.0


def build(source: str, cookies_from_browser: str = "", full_video: bool = False) -> dict:
    path = Path(source).expanduser()
    if archives.is_zip_source(path):
        src = archives.ZipPlanSource(path.resolve())
    else:
        src = sources.detect(
            source,
            cookies_from_browser,
            full_video=full_video,
        )
    items = manifest.finalize(src.list())
    by_kind: dict[str, list[manifest.Item]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)

    groups = [
        {
            "kind": kind,
            "count": len(group),
            "size_bytes": sum(item.size for item in group),
            "work_items": sum(1 for item in group if item.target),
        }
        for kind, group in sorted(by_kind.items())
    ]
    media = [item for item in items if item.kind in ("video", "audio") and item.target]
    media_size = sum(item.size for item in media)
    estimate: dict[str, object] = {
        "basis": "none",
        "media_items": len(media),
        "media_bytes": media_size,
        "media_duration_seconds": None,
        "asr_seconds": None,
    }
    can_probe = getattr(src, "probe_media", getattr(src, "is_local", False))
    if media and can_probe:
        seconds = sum(ffprobe_duration(src.root / item.rel) or 0 for item in media)
        estimate.update(
            basis="duration",
            media_duration_seconds=seconds,
            asr_seconds=seconds / ASR_SPEED,
        )
    elif media:
        seconds = media_size / (0.7 * 1024**3) * 3600
        estimate.update(
            basis="size",
            media_duration_seconds=seconds,
            asr_seconds=seconds / ASR_SPEED,
        )
    return {
        "source": {
            "input": source,
            "title": src.title(),
            "adapter": type(src).__name__,
            "is_local": bool(src.is_local),
        },
        "items": groups,
        "estimate": estimate,
        "_by_kind": by_kind,
    }
