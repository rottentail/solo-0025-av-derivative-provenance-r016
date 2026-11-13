#!/usr/bin/env python3
"""Differential checks: ranged local rendering must equal full rendering.

This is a standalone verification script (not part of the deferred automated
suite). It exercises both render paths over randomized plans and asserts
byte-level identity, plus the locality/fallback rules and the independent
source-digest audit. Run with:

    python3 -I scripts/local_read_checks.py
"""

from __future__ import annotations

import hashlib
import io
import random
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.application import MediaProvenanceApplication
from avprovenance.errors import IntegrityMismatch
from avprovenance.media import (
    BytesMediaSource,
    LocalReadUnavailable,
    apply_media_operations,
    apply_media_operations_local,
)
from avprovenance.renditions import plan_is_locally_readable


def make_wav(
    *,
    frame_count: int,
    sample_rate: int = 8000,
    channels: int = 1,
    seed: int = 7,
) -> bytes:
    rng = random.Random(seed)
    frames = [
        rng.randint(-30000, 30000) for _ in range(frame_count * channels)
    ]
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return payload.getvalue()


def make_y4m(
    *,
    width: int = 64,
    height: int = 36,
    frame_count: int = 24,
    seed: int = 11,
) -> bytes:
    rng = random.Random(seed)
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} F12:1 Ip C420jpeg\n"
    ).encode("ascii")
    cw, ch = width // 2, height // 2
    parts = [header]
    for _ in range(frame_count):
        parts.append(b"FRAME\n")
        parts.append(bytes(rng.randrange(256) for _ in range(width * height)))
        parts.append(bytes(rng.randrange(256) for _ in range(cw * ch)))
        parts.append(bytes(rng.randrange(256) for _ in range(cw * ch)))
    return b"".join(parts)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


AUDIO_PLANS: list[list[dict[str, Any]]] = [
    [
        {"type": "media_trim", "start_seconds": 0.1, "duration_seconds": 0.2},
    ],
    [
        {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.4},
        {"type": "audio_gain", "db": -6.5},
    ],
    [
        {"type": "media_trim", "start_seconds": 0.2, "duration_seconds": 0.3},
        {"type": "audio_gain", "db": 3.0},
        {"type": "audio_normalize", "target_peak": 0.7},
    ],
    [
        {"type": "media_trim", "start_seconds": 0.0, "duration_seconds": 0.05},
        {"type": "audio_normalize", "target_peak": 0.95},
        {"type": "audio_gain", "db": -2},
    ],
    [
        {"type": "media_trim", "start_seconds": 0.15, "duration_seconds": 0.25},
        {"type": "media_trim", "start_seconds": 0.02, "duration_seconds": 0.1},
        {"type": "audio_gain", "db": 12},
    ],
    # Whole-clip trim window: local path may read everything but stays equal.
    [
        {"type": "media_trim", "start_seconds": 0.0, "duration_seconds": 600.0},
        {"type": "audio_gain", "db": -1},
    ],
]

VIDEO_PLANS: list[list[dict[str, Any]]] = [
    [
        {"type": "media_trim", "start_frame": 2, "frame_count": 3},
    ],
    [
        {"type": "media_trim", "start_frame": 1, "frame_count": 8},
        {"type": "video_crop", "x": 4, "y": 6, "width": 32, "height": 18},
    ],
    [
        {"type": "media_trim", "start_frame": 0, "frame_count": 5},
        {"type": "video_luma_offset", "amount": 17},
        {"type": "video_mirror"},
    ],
    [
        {"type": "media_trim", "start_frame": 3, "frame_count": 10},
        {"type": "video_crop", "x": 8, "y": 4, "width": 40, "height": 20},
        {"type": "video_luma_offset", "amount": -21},
        {"type": "video_mirror"},
    ],
    [
        {"type": "media_trim", "start_frame": 5, "frame_count": 4},
        {"type": "media_trim", "start_frame": 1, "frame_count": 2},
        {"type": "video_mirror"},
    ],
]

# Plans whose statistics or mixing scope forbids the ranged path.
INELIGIBLE_PLANS = [
    ("audio", [{"type": "audio_gain", "db": -3},
               {"type": "media_trim", "start_seconds": 0.1,
                "duration_seconds": 0.2}]),
    ("audio", [{"type": "audio_normalize", "target_peak": 0.8},
               {"type": "media_trim", "start_seconds": 0.1,
                "duration_seconds": 0.2}]),
]


def check_differential() -> None:
    wav = make_wav(frame_count=4800, sample_rate=8000, channels=2)
    y4m = make_y4m(frame_count=24)

    for index, plan in enumerate(AUDIO_PLANS):
        assert plan_is_locally_readable("audio", plan, has_secondary=False), index
        full = apply_media_operations(wav, plan, None)
        source = BytesMediaSource(wav)
        local = apply_media_operations_local(source, plan)
        assert local.data == full.data, f"audio plan {index} bytes differ"
        assert local.descriptor == full.descriptor, f"audio plan {index} descriptor"
        assert local.source_bytes_read <= len(wav), index
        print(
            f"  audio plan {index}: bytes identical, "
            f"read {local.source_bytes_read}/{len(wav)} source bytes "
            f"({local.source_bytes_read / len(wav):.1%}), "
            f"{local.source_read_calls} read calls"
        )

    for index, plan in enumerate(VIDEO_PLANS):
        assert plan_is_locally_readable("video", plan, has_secondary=False), index
        full = apply_media_operations(y4m, plan, None)
        source = BytesMediaSource(y4m)
        local = apply_media_operations_local(source, plan)
        assert local.data == full.data, f"video plan {index} bytes differ"
        assert local.descriptor == full.descriptor, f"video plan {index} descriptor"
        assert local.source_bytes_read <= len(y4m), index
        print(
            f"  video plan {index}: bytes identical, "
            f"read {local.source_bytes_read}/{len(y4m)} source bytes "
            f"({local.source_bytes_read / len(y4m):.1%}), "
            f"{local.source_read_calls} read calls"
        )

    # Ineligibility rules.
    assert not plan_is_locally_readable(
        "audio", INELIGIBLE_PLANS[0][1], has_secondary=False
    )
    assert not plan_is_locally_readable(
        "audio", INELIGIBLE_PLANS[1][1], has_secondary=False
    )
    assert not plan_is_locally_readable(
        "audio", AUDIO_PLANS[1], has_secondary=True
    ), "secondary source (mix) must use the full path"
    assert not plan_is_locally_readable(
        "video", VIDEO_PLANS[0], has_secondary=True
    )
    assert not plan_is_locally_readable(
        "audio", [{"type": "audio_gain", "db": -3}], has_secondary=False
    ), "plan without leading trim is not local"
    print("  locality rules: ineligible plans correctly route to full path")


def check_truncation_fallback() -> None:
    """A layout the ranged parser cannot prove must raise the fallback signal."""
    wav = make_wav(frame_count=2000)
    plan = [
        {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.1}
    ]

    # 1) Destroyed RIFF signature: the ranged probe must refuse.
    broken_signature = b"XXXX" + wav[4:]
    source = BytesMediaSource(broken_signature)
    try:
        apply_media_operations_local(source, plan)
    except LocalReadUnavailable as exc:
        print(f"  broken RIFF signature raised fallback signal: {exc.reason}")
    else:  # pragma: no cover
        raise AssertionError("broken RIFF signature should be unavailable locally")

    # 2) Valid header but payload truncated inside the requested window.
    truncated = wav[: len(wav) - 40]
    source = BytesMediaSource(truncated)
    try:
        apply_media_operations_local(source, plan)
    except LocalReadUnavailable as exc:
        print(f"  truncated payload raised fallback signal: {exc.reason}")
    else:  # pragma: no cover
        raise AssertionError("truncated payload should be unavailable locally")

    # 3) Y4M with a truncated final frame: probe indexes past end of source.
    y4m = make_y4m(frame_count=12)
    cut = len(y4m) - 30
    source = BytesMediaSource(y4m[:cut])
    video_plan = [
        {"type": "media_trim", "start_frame": 0, "frame_count": 10}
    ]
    try:
        apply_media_operations_local(source, video_plan)
    except LocalReadUnavailable as exc:
        print(f"  truncated Y4M raised fallback signal: {exc.reason}")
    else:  # pragma: no cover
        raise AssertionError("truncated Y4M should be unavailable locally")


def check_application_audit() -> None:
    """End-to-end: local rendition succeeds; audit catches tail corruption."""
    with tempfile.TemporaryDirectory(prefix="avprov-local-") as directory:
        app = MediaProvenanceApplication(Path(directory))
        wav = make_wav(frame_count=4000, sample_rate=8000)
        asset = app.ingest_asset("long.wav", wav)
        plan = [
            {"type": "media_trim", "start_seconds": 0.1, "duration_seconds": 0.2},
            {"type": "audio_gain", "db": -4},
            {"type": "audio_normalize", "target_peak": 0.8},
        ]
        rendition = app.create_rendition(
            {
                "primary_asset_id": asset["id"],
                "output_name": "short.wav",
                "operations": plan,
            }
        )
        assert rendition["status"] == "completed"

        # Healthy source: full replay + full-source digest both verify.
        healthy = app.verify_rendition(rendition["id"])
        assert healthy["status"] == "verified"

        # Corrupt bytes well outside the 0.1-0.3s trimmed window (near EOF).
        key = app.content_files.key_for_digest(asset["sha256"])
        path = app.content_files._path_for_key(key)
        blob = bytearray(path.read_bytes())
        blob[-100] ^= 0xFF
        path.write_bytes(bytes(blob))
        try:
            app.verify_rendition(rendition["id"])
        except IntegrityMismatch as exc:
            reason = exc.details["reason"]
            assert reason == "source_digest_drift", reason
            print(f"  tail corruption independently detected: {reason}")
        else:  # pragma: no cover
            raise AssertionError("corrupted source must fail the audit")

        # A fresh rendition of the same short window still succeeds locally
        # (the corruption lies outside the window) and produces the same
        # output bytes; the audit remains the authority on whole-source health.
        redo = app.create_rendition(
            {
                "primary_asset_id": asset["id"],
                "output_name": "short-again.wav",
                "operations": plan,
            }
        )
        assert redo["status"] == "completed"
        assert (
            redo["output_asset"]["sha256"]
            == rendition["output_asset"]["sha256"]
        )
        print("  short-window render unaffected by out-of-window corruption")


def main() -> int:
    started = time.perf_counter()
    print("[1/3] differential byte-identity over randomized plans")
    check_differential()
    print("[2/3] truncation and malformed-layout fallback")
    check_truncation_fallback()
    print("[3/3] end-to-end audit with independent source digest")
    check_application_audit()
    print(f"all local-read checks passed in {time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
