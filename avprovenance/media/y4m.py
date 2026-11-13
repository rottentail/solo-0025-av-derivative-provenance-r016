"""Planar YUV4MPEG2 parsing and serialization for bounded C420 media."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import UnsupportedMedia
from ..models import MediaDescriptor
from .errors import LocalReadUnavailable
from .source import MediaByteSource


SUPPORTED_PIXEL_FORMATS = {"C420", "C420jpeg", "C420mpeg2", "C420p"}


@dataclass(frozen=True)
class Y4mFrame:
    y_plane: bytes
    u_plane: bytes
    v_plane: bytes


@dataclass(frozen=True)
class Y4mClip:
    width: int
    height: int
    rate_numerator: int
    rate_denominator: int
    interlace: str
    pixel_format: str
    frames: tuple[Y4mFrame, ...]

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def chroma_width(self) -> int:
        return self.width // 2

    @property
    def chroma_height(self) -> int:
        return self.height // 2


def _read_line(data: bytes, position: int) -> tuple[str, int]:
    newline = data.find(b"\n", position)
    if newline < 0:
        raise UnsupportedMedia("Y4M stream ended inside a header line.")
    raw = data[position:newline]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        line = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise UnsupportedMedia("Y4M header lines must be ASCII.") from exc
    return line, newline + 1


def _parse_header(line: str) -> dict[str, str | int]:
    if not line.startswith("YUV4MPEG2"):
        raise UnsupportedMedia("The payload does not begin with a Y4M header.")
    fields = line.split()
    if not fields or fields[0] != "YUV4MPEG2":
        raise UnsupportedMedia("The Y4M signature is malformed.")

    values: dict[str, str | int] = {
        "interlace": "?",
        "pixel_format": "C420jpeg",
    }
    for field in fields[1:]:
        if len(field) < 2:
            raise UnsupportedMedia("A Y4M header field is too short.")
        key, raw_value = field[0], field[1:]
        if key == "W":
            values["width"] = int(raw_value)
        elif key == "H":
            values["height"] = int(raw_value)
        elif key == "F":
            parts = raw_value.split(":")
            if len(parts) != 2:
                raise UnsupportedMedia("The Y4M frame-rate field is malformed.")
            values["rate_numerator"] = int(parts[0])
            values["rate_denominator"] = int(parts[1])
        elif key == "I":
            values["interlace"] = raw_value
        elif key == "C":
            values["pixel_format"] = f"C{raw_value}"

    required = ("width", "height", "rate_numerator", "rate_denominator")
    if any(key not in values for key in required):
        raise UnsupportedMedia("Y4M header is missing geometry or frame rate.")
    return values


def _validate_header(values: dict[str, str | int]) -> None:
    width = int(values["width"])
    height = int(values["height"])
    numerator = int(values["rate_numerator"])
    denominator = int(values["rate_denominator"])
    pixel_format = str(values["pixel_format"])
    if width < 2 or height < 2 or width > 1920 or height > 1080:
        raise UnsupportedMedia(
            "Y4M geometry is outside the supported bounds.",
            details={"width": width, "height": height},
        )
    if width % 2 or height % 2:
        raise UnsupportedMedia("Y4M width and height must both be even.")
    if numerator <= 0 or denominator <= 0 or numerator > 240 or denominator > 1001:
        raise UnsupportedMedia("Y4M frame rate is outside the supported bounds.")
    if pixel_format not in SUPPORTED_PIXEL_FORMATS:
        raise UnsupportedMedia(
            "Only planar 4:2:0 Y4M payloads are supported.",
            details={"pixel_format": pixel_format},
        )


def decode_y4m(data: bytes) -> Y4mClip:
    if not data.startswith(b"YUV4MPEG2"):
        raise UnsupportedMedia("The payload is not a Y4M stream.")
    header_line, position = _read_line(data, 0)
    try:
        header = _parse_header(header_line)
        _validate_header(header)
    except ValueError as exc:
        raise UnsupportedMedia("A Y4M numeric header field is malformed.") from exc

    width = int(header["width"])
    height = int(header["height"])
    chroma_width = width // 2
    chroma_height = height // 2
    y_size = width * height
    chroma_size = chroma_width * chroma_height
    frames: list[Y4mFrame] = []

    while position < len(data):
        line, position = _read_line(data, position)
        if not line.startswith("FRAME"):
            raise UnsupportedMedia("A Y4M frame header is missing.")
        frame_size = y_size + chroma_size * 2
        end = position + frame_size
        if end > len(data):
            raise UnsupportedMedia("A Y4M frame payload is truncated.")
        y_end = position + y_size
        u_end = y_end + chroma_size
        frames.append(
            Y4mFrame(
                y_plane=data[position:y_end],
                u_plane=data[y_end:u_end],
                v_plane=data[u_end:end],
            )
        )
        position = end
        if len(frames) > 240:
            raise UnsupportedMedia("Y4M frame count exceeds the supported bound.")

    if not frames:
        raise UnsupportedMedia("Y4M stream contains no frames.")
    return Y4mClip(
        width=width,
        height=height,
        rate_numerator=int(header["rate_numerator"]),
        rate_denominator=int(header["rate_denominator"]),
        interlace=str(header["interlace"]),
        pixel_format=str(header["pixel_format"]),
        frames=tuple(frames),
    )


def inspect_y4m(data: bytes) -> MediaDescriptor:
    clip = decode_y4m(data)
    return MediaDescriptor(
        kind="video",
        mime_type="video/x-yuv4mpeg2",
        container="y4m",
        properties={
            "width": clip.width,
            "height": clip.height,
            "frame_count": clip.frame_count,
            "rate_numerator": clip.rate_numerator,
            "rate_denominator": clip.rate_denominator,
            "interlace": clip.interlace,
            "pixel_format": clip.pixel_format,
            "duration_seconds": round(
                clip.frame_count
                * clip.rate_denominator
                / clip.rate_numerator,
                9,
            ),
        },
    )


def encode_y4m(clip: Y4mClip) -> bytes:
    if not clip.frames:
        raise UnsupportedMedia("Cannot encode a Y4M stream without frames.")
    header = (
        "YUV4MPEG2 "
        f"W{clip.width} H{clip.height} "
        f"F{clip.rate_numerator}:{clip.rate_denominator} "
        f"I{clip.interlace} {clip.pixel_format}\n"
    ).encode("ascii")
    parts = [header]
    for frame in clip.frames:
        parts.append(b"FRAME\n")
        parts.append(frame.y_plane)
        parts.append(frame.u_plane)
        parts.append(frame.v_plane)
    return b"".join(parts)


@dataclass(frozen=True)
class Y4mLayout:
    """Header-derived geometry plus byte offsets for every frame payload."""

    width: int
    height: int
    rate_numerator: int
    rate_denominator: int
    interlace: str
    pixel_format: str
    frame_count: int
    frame_payload_size: int
    frame_payload_offsets: tuple[int, ...]
    frame_header_offsets: tuple[int, ...]

    @property
    def chroma_width(self) -> int:
        return self.width // 2

    @property
    def chroma_height(self) -> int:
        return self.height // 2

    def descriptor(self) -> MediaDescriptor:
        return MediaDescriptor(
            kind="video",
            mime_type="video/x-yuv4mpeg2",
            container="y4m",
            properties={
                "width": self.width,
                "height": self.height,
                "frame_count": self.frame_count,
                "rate_numerator": self.rate_numerator,
                "rate_denominator": self.rate_denominator,
                "interlace": self.interlace,
                "pixel_format": self.pixel_format,
                "duration_seconds": round(
                    self.frame_count
                    * self.rate_denominator
                    / self.rate_numerator,
                    9,
                ),
            },
        )


def _read_source_line(
    source: MediaByteSource,
    position: int,
    *,
    max_length: int = 256,
) -> tuple[bytes, int]:
    """Read one newline-terminated header line using bounded ranged reads."""
    buffer = bytearray()
    cursor = position
    while cursor < source.size and len(buffer) < max_length:
        chunk = source.read_range(cursor, 64)
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            buffer.extend(chunk[: newline + 1])
            cursor += newline + 1
            return bytes(buffer), cursor
        buffer.extend(chunk)
        cursor += len(chunk)
    raise LocalReadUnavailable("y4m_header_line_unbounded")


def probe_y4m(source: MediaByteSource) -> Y4mLayout:
    """Parse the stream header and index frame offsets without reading payloads.

    Frames in an accepted Y4M stream have fixed geometry, so every frame
    header occupies the same small slot. The probe reads the stream header and
    the first frame header only, then derives the remaining offsets
    arithmetically. The range decoder re-reads each consumed frame header and
    rejects the layout if a slot does not contain ``FRAME``, so a malformed or
    variable layout triggers :class:`LocalReadUnavailable` and a full fallback
    instead of producing wrong bytes.
    """
    header_raw, position = _read_source_line(source, 0)
    if not header_raw.startswith(b"YUV4MPEG2"):
        raise LocalReadUnavailable("y4m_not_stream")
    if header_raw.endswith(b"\r\n"):
        header_line = header_raw[:-2].decode("ascii", errors="strict")
    else:
        header_line = header_raw[:-1].decode("ascii", errors="strict")
    try:
        header = _parse_header(header_line)
        _validate_header(header)
    except ValueError as exc:
        raise LocalReadUnavailable("y4m_header_malformed") from exc
    except UnsupportedMedia:
        raise

    width = int(header["width"])
    height = int(header["height"])
    y_size = width * height
    frame_size = y_size + 2 * (width // 2) * (height // 2)

    first_header, first_payload = _read_source_line(source, position)
    if not first_header.rstrip(b"\r\n").startswith(b"FRAME"):
        raise LocalReadUnavailable("y4m_frame_header_missing")
    first_header_length = first_payload - position
    stride = first_header_length + frame_size

    # With constant frame geometry every frame occupies one fixed stride
    # (frame header plus payload). The whole stream after the stream header
    # must therefore be an integer number of strides. Anything else means a
    # partial frame or a variable layout, which the full parser is left to
    # judge; the per-frame header checks in the range decoder validate the
    # indexed slots when a plan actually consumes them.
    stream_span = source.size - position
    if stream_span < stride or stream_span % stride != 0:
        raise LocalReadUnavailable("y4m_trailing_or_truncated")
    frame_count = stream_span // stride
    if frame_count > 240:
        raise LocalReadUnavailable("y4m_frame_count_unbounded")

    payload_offsets = [
        first_payload + index * stride for index in range(frame_count)
    ]
    header_offsets = [
        position + index * stride for index in range(frame_count)
    ]
    return Y4mLayout(
        width=width,
        height=height,
        rate_numerator=int(header["rate_numerator"]),
        rate_denominator=int(header["rate_denominator"]),
        interlace=str(header["interlace"]),
        pixel_format=str(header["pixel_format"]),
        frame_count=frame_count,
        frame_payload_size=frame_size,
        frame_payload_offsets=tuple(payload_offsets),
        frame_header_offsets=tuple(header_offsets),
    )


def decode_y4m_range(
    source: MediaByteSource,
    layout: Y4mLayout,
    start_frame: int,
    frame_count: int,
) -> Y4mClip:
    """Decode only the requested frame window from the stored Y4M stream."""
    if start_frame < 0 or frame_count <= 0:
        raise LocalReadUnavailable("y4m_range_invalid")
    end_frame = start_frame + frame_count
    if end_frame > layout.frame_count:
        raise LocalReadUnavailable("y4m_range_beyond_source")

    y_size = layout.width * layout.height
    chroma_size = layout.chroma_width * layout.chroma_height
    frames: list[Y4mFrame] = []
    for frame_index in range(start_frame, end_frame):
        # Confirm the frame header itself before trusting the indexed offset.
        header_offset = layout.frame_header_offsets[frame_index]
        header = source.read_range(header_offset, 6)
        if not header.startswith(b"FRAME"):
            raise LocalReadUnavailable("y4m_frame_header_drift")
        payload_offset = layout.frame_payload_offsets[frame_index]
        payload = source.read_range(
            payload_offset,
            layout.frame_payload_size,
        )
        if len(payload) != layout.frame_payload_size:
            raise LocalReadUnavailable("y4m_frame_short_read")
        y_end = y_size
        u_end = y_end + chroma_size
        frames.append(
            Y4mFrame(
                y_plane=payload[:y_end],
                u_plane=payload[y_end:u_end],
                v_plane=payload[u_end:],
            )
        )
    return Y4mClip(
        width=layout.width,
        height=layout.height,
        rate_numerator=layout.rate_numerator,
        rate_denominator=layout.rate_denominator,
        interlace=layout.interlace,
        pixel_format=layout.pixel_format,
        frames=tuple(frames),
    )
