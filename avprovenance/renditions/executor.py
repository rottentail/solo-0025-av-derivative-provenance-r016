"""Execute a normalized plan and convert media failures to domain errors."""

from __future__ import annotations

from typing import Any

from ..errors import RenderFailed, UnsupportedMedia
from ..media import (
    LocalReadUnavailable,
    LocalRenderOutcome,
    MediaByteSource,
    MediaOperationFailure,
    RenderOutcome,
    apply_media_operations,
    apply_media_operations_local,
)


def execute_plan(
    primary_bytes: bytes,
    operations: list[dict[str, Any]],
    secondary_bytes: bytes | None,
) -> RenderOutcome:
    try:
        return apply_media_operations(primary_bytes, operations, secondary_bytes)
    except MediaOperationFailure as exc:
        raise RenderFailed(
            "The rendition could not be produced.",
            details={"reason": exc.code, "detail": exc.message},
        ) from exc
    except UnsupportedMedia as exc:
        raise RenderFailed(
            "The rendition produced unsupported media bytes.",
            details={"reason": exc.code},
        ) from exc


def execute_plan_local(
    primary_source: MediaByteSource,
    operations: list[dict[str, Any]],
) -> LocalRenderOutcome:
    """Run the ranged path.

    :class:`LocalReadUnavailable` is re-raised unchanged so the application
    can retry with the full read. Genuine media failures use the same domain
    mapping as the full path, keeping error codes identical.
    """
    try:
        return apply_media_operations_local(primary_source, operations)
    except MediaOperationFailure as exc:
        raise RenderFailed(
            "The rendition could not be produced.",
            details={"reason": exc.code, "detail": exc.message},
        ) from exc
    except UnsupportedMedia as exc:
        raise RenderFailed(
            "The rendition produced unsupported media bytes.",
            details={"reason": exc.code},
        ) from exc
