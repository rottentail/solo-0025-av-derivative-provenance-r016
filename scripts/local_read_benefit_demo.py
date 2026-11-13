#!/usr/bin/env python3
"""Quantify ranged-read savings on material near the 24 MiB payload budget.

The script generates one WAV and one Y4M source whose media payloads sit just
below the server's 24 MiB decoded-payload limit, renders the same short
derivation twice (full read-and-decode path vs. ranged local path), asserts
byte-identical outputs, and reports source bytes served, samples/frames
touched, and wall time. It also shows a whole-clip-statistic plan (normalize
before trim) correctly staying on the full path.

Run with:

    python3 -I scripts/local_read_benefit_demo.py
"""

from __future__ import annotations

import io
import math
import random
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.application import MediaProvenanceApplication  # noqa: E402
from avprovenance.media import (  # noqa: E402
    BytesMediaSource,
    apply_media_operations,
    apply_media_operations_local,
)
from avprovenance.renditions import plan_is_locally_readable  # noqa: E402


PAYLOAD_BUDGET = 24 * 1024 * 1024
REPEATS = 5


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GiB"


def median(values: list[float]) -> float:
    return sorted(values)[len(values) // 2]


def make_large_wav() -> tuple[bytes, dict[str, Any]]:
    """Stereo 48 kHz PCM whose payload is just under 24 MiB."""
    sample_rate = 48000
    channels = 2
    frame_count = PAYLOAD_BUDGET // (channels * 2) - 2048
    rng = random.Random(20261113)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        chunk = 20000
        for start in range(0, frame_count, chunk):
            count = min(chunk, frame_count - start)
            samples = [
                int(26000 * math.sin((start + i) * 0.0017))
                + rng.randint(-1200, 1200)
                for i in range(count * channels)
            ]
            writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    data = payload.getvalue()
    meta = {
        "frame_count": frame_count,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_seconds": frame_count / sample_rate,
    }
    return data, meta


def make_large_y4m() -> tuple[bytes, dict[str, Any]]:
    """1280x720 C420 stream whose frame payloads are just under 24 MiB."""
    width, height = 1280, 720
    cw, ch = width // 2, height // 2
    frame_size = width * height + 2 * cw * ch
    frame_count = max(1, (PAYLOAD_BUDGET - 4096) // frame_size)
    rng = random.Random(424242)
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} F24:1 Ip C420jpeg\n"
    ).encode("ascii")
    parts = [header]
    for frame_index in range(frame_count):
        parts.append(b"FRAME\n")
        # Deterministic structured content so the planes are not uniform.
        y_plane = bytes(
            (x * 3 + y * 5 + frame_index * 11) % 256
            for y in range(height)
            for x in range(width)
        )
        u_plane = bytes(
            (96 + x * 2 + frame_index * 3 + rng.randrange(7)) % 256
            for _y in range(ch)
            for x in range(cw)
        )
        v_plane = bytes(
            (144 + y * 2 + frame_index * 5 + rng.randrange(7)) % 256
            for y in range(ch)
            for _x in range(cw)
        )
        parts.extend((y_plane, u_plane, v_plane))
    data = b"".join(parts)
    meta = {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "frame_size": frame_size,
        "duration_seconds": frame_count / 24,
    }
    return data, meta


def time_runs(label: str, runner: Callable[[], Any], repeats: int) -> tuple[Any, float]:
    outcome = None
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        outcome = runner()
        timings.append(time.perf_counter() - started)
    return outcome, median(timings)


def report(
    title: str,
    source: bytes,
    plan: list[dict[str, Any]],
    *,
    full_work_label: str,
    local_work_label: str,
) -> None:
    eligible = plan_is_locally_readable(
        "audio" if title.startswith("WAV") else "video",
        plan,
        has_secondary=False,
    )
    print(f"\n=== {title} ===")
    print(f"source size: {human(len(source))} ({len(source)} bytes)")
    print(f"plan: {plan}")
    print(f"locality analyzer: {'LOCAL' if eligible else 'FULL-ONLY'}")

    full_outcome, full_seconds = time_runs(
        "full",
        lambda: apply_media_operations(source, plan, None),
        REPEATS,
    )

    def run_local() -> Any:
        ranged = BytesMediaSource(source)
        result = apply_media_operations_local(ranged, plan)
        return result, ranged

    if not eligible:
        print(
            f"full path median time: {full_seconds * 1000:.1f} ms "
            f"({full_work_label})"
        )
        print("-> plan needs whole-clip state before/across trim; "
              "ranged path deliberately not used")
        return

    (local_outcome, ranged), local_seconds = time_runs("local", run_local, REPEATS)
    assert local_outcome.data == full_outcome.data, "output bytes diverged"
    assert local_outcome.descriptor == full_outcome.descriptor

    served = ranged.bytes_read
    saved = len(source) - served
    print(f"output size: {human(len(full_outcome.data))} "
          f"(identical bytes on both paths: yes)")
    print(f"  full  path: read {human(len(source))} source bytes, "
          f"{full_work_label}, median {full_seconds * 1000:.1f} ms")
    print(
        f"  local path: read {human(served)} source bytes "
        f"({served / len(source):.2%} of source; {ranged.read_calls} ranged "
        f"reads), {local_work_label}, median {local_seconds * 1000:.1f} ms"
    )
    print(
        f"  savings: {human(saved)} source bytes not read "
        f"({saved / len(source):.2%}), "
        f"{(full_seconds / local_seconds):.2f}x median wall-time speedup"
    )


def run_end_to_end(
    app: MediaProvenanceApplication,
    wav: bytes,
    y4m: bytes,
) -> None:
    """Exercise the real storage-backed source through the HTTP application."""
    print("\n=== end-to-end via application storage (FileMediaSource) ===")
    audio_asset = app.ingest_asset("near-limit.wav", wav)
    video_asset = app.ingest_asset("near-limit.y4m", y4m)
    audio_plan = [
        {"type": "media_trim", "start_seconds": 0.5, "duration_seconds": 1.0},
        {"type": "audio_gain", "db": -6},
        {"type": "audio_normalize", "target_peak": 0.8},
    ]
    video_plan = [
        {"type": "media_trim", "start_frame": 2, "frame_count": 2},
        {"type": "video_crop", "x": 16, "y": 16, "width": 320, "height": 180},
        {"type": "video_luma_offset", "amount": 10},
        {"type": "video_mirror"},
    ]
    audio_rendition = app.create_rendition(
        {
            "primary_asset_id": audio_asset["id"],
            "output_name": "window.wav",
            "operations": audio_plan,
        }
    )
    video_rendition = app.create_rendition(
        {
            "primary_asset_id": video_asset["id"],
            "output_name": "window.y4m",
            "operations": video_plan,
        }
    )
    assert audio_rendition["status"] == "completed"
    assert video_rendition["status"] == "completed"

    # The integrity audit still performs a full replay over the whole sources.
    audio_audit = app.verify_rendition(audio_rendition["id"])
    video_audit = app.verify_rendition(video_rendition["id"])
    assert audio_audit["status"] == "verified"
    assert video_audit["status"] == "verified"
    print(
        "both near-limit renditions completed via ranged reads and passed "
        "full-source integrity audits"
    )


def main() -> int:
    print("Generating near-limit source media (one-time cost)...")
    wav, wav_meta = make_large_wav()
    y4m, y4m_meta = make_large_y4m()
    print(
        f"WAV: {human(len(wav))}, {wav_meta['frame_count']} stereo frames, "
        f"{wav_meta['duration_seconds']:.1f} s"
    )
    print(
        f"Y4M: {human(len(y4m))}, {y4m_meta['frame_count']} frames of "
        f"{y4m_meta['width']}x{y4m_meta['height']}, "
        f"{y4m_meta['duration_seconds']:.2f} s"
    )

    short_audio_plan = [
        {"type": "media_trim", "start_seconds": 1.0, "duration_seconds": 0.5},
        {"type": "audio_gain", "db": -3},
        {"type": "audio_normalize", "target_peak": 0.8},
    ]
    report(
        "WAV short 0.5 s selection (trim + gain + normalize)",
        wav,
        short_audio_plan,
        full_work_label=(
            f"all {wav_meta['frame_count']} frames decoded and processed"
        ),
        local_work_label="only 24,000 frames decoded and processed",
    )

    short_video_plan = [
        {"type": "media_trim", "start_frame": 3, "frame_count": 2},
        {"type": "video_crop", "x": 16, "y": 16, "width": 640, "height": 360},
        {"type": "video_luma_offset", "amount": 12},
        {"type": "video_mirror"},
    ]
    report(
        "Y4M short 2-frame selection (trim + crop + luma + mirror)",
        y4m,
        short_video_plan,
        full_work_label=(
            f"all {y4m_meta['frame_count']} frames decoded and processed"
        ),
        local_work_label="only 2 frames decoded and processed",
    )

    # Normalize BEFORE trim: depends on the peak across the entire source, so
    # it must not be mistaken for a local operation.
    whole_clip_plan = [
        {"type": "audio_normalize", "target_peak": 0.8},
        {"type": "media_trim", "start_seconds": 1.0, "duration_seconds": 0.5},
    ]
    report(
        "WAV normalize-before-trim (whole-clip peak, must stay full path)",
        wav,
        whole_clip_plan,
        full_work_label=(
            f"all {wav_meta['frame_count']} frames decoded and processed"
        ),
        local_work_label="n/a",
    )

    with tempfile.TemporaryDirectory(prefix="avprov-benefit-") as directory:
        run_end_to_end(MediaProvenanceApplication(Path(directory)), wav, y4m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
