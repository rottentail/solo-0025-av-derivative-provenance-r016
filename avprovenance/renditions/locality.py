"""Decide whether a normalized plan can be served by the ranged read path.

The local path is only safe when every operation after the leading trim is
provably independent of frames or samples that the trim discards:

* Audio gain, normalization (post-trim peak), crop, luma offset, and mirror
  are pointwise or window-local.
* Audio mixing reads the full-length secondary source, so any plan with a
  secondary asset stays on the full path.
* Normalization or any other whole-clip statistic computed *before* the trim
  stays on the full path: the eligibility rule requires ``media_trim`` to be
  the first operation.

The analyzer never mutates the plan and never rejects anything; an
ineligible plan simply renders through the unchanged full-read pipeline.
"""

from __future__ import annotations

from typing import Any

from ..media.audio_ops import LOCAL_AUDIO_OPERATIONS
from ..media.video_ops import LOCAL_VIDEO_OPERATIONS


def plan_is_locally_readable(
    media_kind: str,
    operations: list[dict[str, Any]],
    *,
    has_secondary: bool,
) -> bool:
    if has_secondary:
        return False
    if not operations or operations[0].get("type") != "media_trim":
        return False
    if media_kind == "audio":
        allowed = LOCAL_AUDIO_OPERATIONS
    elif media_kind == "video":
        allowed = LOCAL_VIDEO_OPERATIONS
    else:
        return False
    return all(
        isinstance(operation, dict) and operation.get("type") in allowed
        for operation in operations
    )
