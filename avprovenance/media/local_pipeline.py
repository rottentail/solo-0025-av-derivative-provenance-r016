"""Ranged rendering path that reads only the media window a plan delivers.

This mirrors :mod:`pipeline` for plans the locality analyzer proves safe. The
ranged parsers raise :class:`LocalReadUnavailable` whenever they cannot
guarantee equivalence with the full parser; callers then fall back to the
unchanged full read-and-decode path, so no plan becomes newly rejectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import UnsupportedMedia
from ..models import MediaDescriptor
from .audio_ops import render_audio_local
from .errors import LocalReadUnavailable, MediaOperationFailure
from .inspector import inspect_media
from .source import MediaByteSource
from .video_ops import render_video_local


@dataclass(frozen=True)
class LocalRenderOutcome:
    data: bytes
    descriptor: MediaDescriptor
    operation_types: list[str]
    source_bytes_read: int
    source_size: int
    source_read_calls: int


def _sniff_kind(source: MediaByteSource) -> str:
    prefix = source.read_range(0, 12)
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio"
    if prefix.startswith(b"YUV4MPEG2"):
        return "video"
    raise LocalReadUnavailable("unknown_media_prefix")


def apply_media_operations_local(
    source: MediaByteSource,
    operations: list[dict[str, Any]],
) -> LocalRenderOutcome:
    kind = _sniff_kind(source)
    if kind == "audio":
        output = render_audio_local(source, operations)
    elif kind == "video":
        output = render_video_local(source, operations)
    else:  # pragma: no cover - _sniff_kind exhausts supported kinds
        raise UnsupportedMedia("The primary media kind has no renderer.")

    descriptor = inspect_media(output)
    if descriptor.kind != kind:
        raise UnsupportedMedia("The renderer changed the media kind unexpectedly.")
    return LocalRenderOutcome(
        data=output,
        descriptor=descriptor,
        operation_types=[str(operation["type"]) for operation in operations],
        source_bytes_read=source.bytes_read,
        source_size=source.size,
        source_read_calls=source.read_calls,
    )


__all__ = [
    "LocalReadUnavailable",
    "LocalRenderOutcome",
    "MediaByteSource",
    "MediaOperationFailure",
    "apply_media_operations_local",
]
