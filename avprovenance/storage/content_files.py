"""Content-addressed media files with path checks and atomic writes."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import PersistenceFailure
from ..serialization import sha256_hex


@dataclass
class FileMediaSource:
    """Seekable, byte-counted handle over one content-addressed blob.

    It implements the structural ``MediaByteSource`` protocol without the
    storage layer importing the media layer. Only explicit ranged reads touch
    the disk, and unique bytes served are counted so callers can prove that
    short trims no longer pull an entire source through memory.
    """

    path: Path
    _size: int
    _ranges: list[tuple[int, int]]
    _read_calls: int = 0

    @classmethod
    def open(cls, path: Path) -> "FileMediaSource":
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise PersistenceFailure("A referenced media file is missing.") from exc
        return cls(path=path, _size=size, _ranges=[])

    @property
    def size(self) -> int:
        return self._size

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
        if offset < 0 or length < 0 or offset > self._size:
            raise PersistenceFailure("A media source range is outside the blob.")
        self._read_calls += 1
        end = min(self._size, offset + length)
        try:
            with open(self.path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(end - offset)
        except OSError as exc:
            raise PersistenceFailure("A referenced media file is unreadable.") from exc
        self._ranges.append((offset, offset + len(data)))
        return data

    def close(self) -> None:
        return None


class ContentFileRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for_digest(digest: str) -> str:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PersistenceFailure("A content digest has an invalid shape.")
        return f"{digest[:2]}/{digest[2:4]}/{digest}"

    def _path_for_key(self, key: str) -> Path:
        parts = Path(key).parts
        if len(parts) != 3:
            raise PersistenceFailure("A content key has an invalid shape.")
        root = self.root.resolve()
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PersistenceFailure("A content key escapes the data root.") from exc
        return candidate

    def open_source(self, key: str) -> FileMediaSource:
        """Open a ranged, byte-counted source without loading the whole blob."""
        return FileMediaSource.open(self._path_for_key(key))

    def write(self, data: bytes, digest: str) -> str:
        if sha256_hex(data) != digest:
            raise PersistenceFailure("Content digest does not match the supplied bytes.")
        key = self.key_for_digest(digest)
        target = self._path_for_key(key)
        if target.exists():
            return key
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".content-",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        )
        temporary_path = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            os.replace(temporary_path, target)
        except OSError as exc:
            temporary.close()
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise PersistenceFailure("Media bytes could not be saved.") from exc
        return key

    def read(self, key: str) -> bytes:
        path = self._path_for_key(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PersistenceFailure("A referenced media file is missing.") from exc

    def remove(self, key: str) -> None:
        path = self._path_for_key(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def exists(self, key: str) -> bool:
        return self._path_for_key(key).is_file()
