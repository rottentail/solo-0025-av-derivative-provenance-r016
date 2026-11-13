"""Seekable, byte-counted media source abstraction for ranged reads.

The full processing path loads complete media blobs into memory. Plans whose
first operation is a trim only need a narrow window of the source, so the
local rendering path reads exactly the header and payload ranges it requires.
Every implementation records how many source bytes were served, which lets
the application demonstrate that work shrinks with the requested window
without pre-caching any decoded content.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaByteSource(Protocol):
    """A seekable byte source that exposes its length and read accounting."""

    @property
    def size(self) -> int:
        """Total length of the underlying media in bytes."""

    @property
    def bytes_read(self) -> int:
        """Number of unique source bytes served since the source was opened."""

    @property
    def read_calls(self) -> int:
        """Number of ranged read requests served."""

    def read_range(self, offset: int, length: int) -> bytes:
        """Return up to ``length`` bytes beginning at ``offset``.

        A short read at end of source returns the available tail bytes.
        """

    def close(self) -> None:
        """Release any underlying handle."""


class BytesMediaSource:
    """In-memory implementation used by tests and differential checks."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._ranges: list[tuple[int, int]] = []
        self._read_calls = 0

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def bytes_read(self) -> int:
        if not self._ranges:
            return 0
        merged = sorted(self._ranges)
        total = 0
        current_start, current_end = merged[0]
        for start, end in merged[1:]:
            if start <= current_end:
                if end > current_end:
                    current_end = end
            else:
                total += current_end - current_start
                current_start, current_end = start, end
        total += current_end - current_start
        return total

    @property
    def read_calls(self) -> int:
        return self._read_calls

    def read_range(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("Media source ranges must use non-negative offsets.")
        if offset > len(self._data):
            raise ValueError("Media source range starts beyond the end of the blob.")
        self._read_calls += 1
        end = min(len(self._data), offset + length)
        self._ranges.append((offset, end))
        return self._data[offset:end]

    def close(self) -> None:
        return None
