"""PCM WAV inspection, decoding, and deterministic encoding."""

from __future__ import annotations

import array
import io
import struct
import sys
import wave
from dataclasses import dataclass
from typing import Iterable

from ..errors import UnsupportedMedia
from ..models import MediaDescriptor
from .errors import LocalReadUnavailable
from .source import MediaByteSource


@dataclass(frozen=True)
class AudioClip:
    sample_rate: int
    channels: tuple[list[int], ...]

    @property
    def frame_count(self) -> int:
        return len(self.channels[0]) if self.channels else 0

    @property
    def channel_count(self) -> int:
        return len(self.channels)


def _open_reader(data: bytes) -> wave.Wave_read:
    try:
        reader = wave.open(io.BytesIO(data), "rb")
    except (EOFError, wave.Error) as exc:
        raise UnsupportedMedia(
            "The audio payload is not a readable RIFF/WAVE file.",
            details={"reason": str(exc)},
        ) from exc
    return reader


def _validate_params(reader: wave.Wave_read) -> None:
    if reader.getcomptype() != "NONE":
        raise UnsupportedMedia(
            "Compressed WAV payloads are not supported.",
            details={"compression": reader.getcomptype()},
        )
    sample_width = reader.getsampwidth()
    if sample_width != 2:
        raise UnsupportedMedia(
            "Only signed 16-bit PCM WAV is supported.",
            details={"sample_width_bytes": sample_width},
        )
    channels = reader.getnchannels()
    if channels not in (1, 2):
        raise UnsupportedMedia(
            "WAV input must contain one or two channels.",
            details={"channels": channels},
        )
    sample_rate = reader.getframerate()
    if sample_rate < 8000 or sample_rate > 192000:
        raise UnsupportedMedia(
            "WAV sample rate is outside the supported range.",
            details={"sample_rate": sample_rate},
        )
    if reader.getnframes() <= 0:
        raise UnsupportedMedia("WAV input has no sample frames.")


def inspect_wav(data: bytes) -> MediaDescriptor:
    reader = _open_reader(data)
    try:
        _validate_params(reader)
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        channels = reader.getnchannels()
        return MediaDescriptor(
            kind="audio",
            mime_type="audio/wav",
            container="wav",
            properties={
                "codec": "pcm_s16le",
                "sample_rate": sample_rate,
                "channels": channels,
                "frame_count": frame_count,
                "duration_seconds": round(frame_count / sample_rate, 9),
            },
        )
    finally:
        reader.close()


def _deinterleave(raw: bytes, channels: int, sample_rate: int) -> AudioClip:
    interleaved = array.array("h")
    interleaved.frombytes(raw)
    if sys.byteorder != "little":
        interleaved.byteswap()
    if len(interleaved) % channels != 0:
        raise UnsupportedMedia("WAV sample data is not frame-aligned.")
    channel_values = tuple(
        interleaved[index::channels].tolist() for index in range(channels)
    )
    return AudioClip(sample_rate=sample_rate, channels=channel_values)


def decode_wav(data: bytes) -> AudioClip:
    reader = _open_reader(data)
    try:
        _validate_params(reader)
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    finally:
        reader.close()
    return _deinterleave(raw, channels, sample_rate)


def _interleave(channels: tuple[list[int], ...], frame_count: int) -> list[int]:
    channel_count = len(channels)
    result = [0] * (frame_count * channel_count)
    for channel_index, values in enumerate(channels):
        result[channel_index::channel_count] = values[:frame_count]
    return result


def encode_wav(clip: AudioClip) -> bytes:
    if not clip.channels:
        raise UnsupportedMedia("Cannot encode an audio clip without channels.")
    if any(len(channel) != clip.frame_count for channel in clip.channels):
        raise UnsupportedMedia("Audio channels do not share one frame count.")
    values = array.array("h", _interleave(clip.channels, clip.frame_count))
    if sys.byteorder != "little":
        values.byteswap()
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(len(clip.channels))
        writer.setsampwidth(2)
        writer.setframerate(clip.sample_rate)
        writer.writeframes(values.tobytes())
    return payload.getvalue()


def replace_channels(
    clip: AudioClip,
    channels: Iterable[list[int]],
) -> AudioClip:
    values = tuple(list(channel) for channel in channels)
    if not values:
        raise UnsupportedMedia("Audio operations produced no channels.")
    expected = len(values[0])
    if any(len(channel) != expected for channel in values):
        raise UnsupportedMedia("Audio operation produced misaligned channels.")
    return AudioClip(sample_rate=clip.sample_rate, channels=values)


@dataclass(frozen=True)
class WavLayout:
    """Header-derived geometry and the byte offset of the PCM payload."""

    sample_rate: int
    channels: int
    frame_count: int
    data_offset: int
    data_size: int

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * 2


def _scan_riff_chunks(source: MediaByteSource) -> tuple[int, int]:
    """Locate the PCM ``data`` chunk without reading its payload.

    Returns ``(data_offset, data_size)``. The layout probe reads only the
    twelve-byte RIFF preamble and eight-byte chunk headers.
    """
    prefix = source.read_range(0, 12)
    if (
        len(prefix) < 12
        or prefix[:4] != b"RIFF"
        or prefix[8:12] != b"WAVE"
    ):
        raise LocalReadUnavailable("wav_not_riff")
    offset = 12
    data_offset = -1
    data_size = 0
    while offset + 8 <= source.size:
        header = source.read_range(offset, 8)
        if len(header) < 8:
            break
        chunk_id = header[:4]
        chunk_size = struct.unpack("<I", header[4:8])[0]
        payload_offset = offset + 8
        if chunk_id == b"data":
            data_offset = payload_offset
            data_size = chunk_size
            break
        # RIFF chunks are word-aligned: odd payloads gain one padding byte.
        offset = payload_offset + chunk_size + (chunk_size & 1)
    if data_offset < 0:
        raise LocalReadUnavailable("wav_data_chunk_missing")
    return data_offset, data_size


def probe_wav(source: MediaByteSource) -> WavLayout:
    """Read only the WAV header and validate it through the standard reader."""
    data_offset, data_size = _scan_riff_chunks(source)
    # The canonical wave reader validates every parameter from the header
    # alone. Reading a prefix ending at the data payload keeps the probe
    # payload-free while still exercising the same acceptance rules.
    header_bytes = source.read_range(0, min(source.size, data_offset))
    reader = _open_reader(header_bytes)
    try:
        _validate_params(reader)
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
    finally:
        reader.close()

    available = source.size - data_offset
    payload_size = min(data_size, max(0, available))
    if frame_count * channels * 2 > payload_size:
        raise LocalReadUnavailable("wav_declared_frames_exceed_payload")
    return WavLayout(
        sample_rate=sample_rate,
        channels=channels,
        frame_count=frame_count,
        data_offset=data_offset,
        data_size=data_size,
    )


def decode_wav_range(
    source: MediaByteSource,
    layout: WavLayout,
    start_frame: int,
    frame_count: int,
) -> AudioClip:
    """Decode exactly ``frame_count`` sample frames beginning at ``start_frame``."""
    if start_frame < 0 or frame_count < 0:
        raise LocalReadUnavailable("wav_range_invalid")
    end_frame = start_frame + frame_count
    if end_frame > layout.frame_count:
        raise LocalReadUnavailable("wav_range_beyond_source")
    bytes_per_frame = layout.bytes_per_frame
    byte_offset = layout.data_offset + start_frame * bytes_per_frame
    raw = source.read_range(byte_offset, frame_count * bytes_per_frame)
    if len(raw) != frame_count * bytes_per_frame:
        raise LocalReadUnavailable("wav_range_short_read")
    return _deinterleave(raw, layout.channels, layout.sample_rate)
