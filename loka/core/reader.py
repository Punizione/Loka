"""Little-endian binary primitives, strings and bounded stream positioning."""

from __future__ import annotations

import struct
from typing import Final

__all__ = [
    "BinaryReader",
    "EndOfStreamError",
    "SEEK_BEGIN",
    "SEEK_CURRENT",
    "SEEK_END",
    "decode_ascii_bytes",
    "trim_null_chars",
]

SEEK_BEGIN: Final = 0
SEEK_CURRENT: Final = 1
SEEK_END: Final = 2

# .NET ASCIIEncoding's replacement fallback emits "?" for undecodable bytes.
_REPLACEMENT = "?"


class EndOfStreamError(Exception):
    """Raised when a read would run past the end of the buffer."""


def decode_ascii_bytes(raw: bytes) -> str:
    """Decode like .NET ``ASCIIEncoding``: one byte -> one character, ``?`` fallback."""
    return "".join(chr(b) if b < 0x80 else _REPLACEMENT for b in raw)


def trim_null_chars(value: str) -> str:
    """Implement VB.NET ``CStr(chars).Trim(Chr(0))`` -- strips NULs from both ends."""
    return value.strip("\x00")


class BinaryReader:
    """Little-endian byte reader with .NET ``BinaryReader`` semantics."""

    __slots__ = ("_data", "_pos", "path")

    def __init__(self, data: bytes, path: str = "") -> None:
        self._data = memoryview(data)
        self._pos = 0
        self.path = path

    # -- construction helpers ------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "BinaryReader":
        with open(path, "rb") as handle:
            return cls(handle.read(), path)

    # -- stream state --------------------------------------------------------
    @property
    def length(self) -> int:
        """Equivalent of ``BaseStream.Length``."""
        return len(self._data)

    @property
    def position(self) -> int:
        """Equivalent of ``BaseStream.Position``."""
        return self._pos

    @position.setter
    def position(self, value: int) -> None:
        self.seek(value, SEEK_BEGIN)

    def seek(self, offset: int, origin: int = SEEK_BEGIN) -> int:
        """Equivalent of ``BaseStream.Seek``.  Returns the new position."""
        if origin == SEEK_BEGIN:
            new_pos = offset
        elif origin == SEEK_CURRENT:
            new_pos = self._pos + offset
        elif origin == SEEK_END:
            new_pos = len(self._data) + offset
        else:  # pragma: no cover - defensive
            raise ValueError(f"invalid seek origin: {origin!r}")

        if new_pos < 0:
            raise EndOfStreamError(
                f"attempt to seek before the start of {self.path or '<buffer>'}"
            )
        self._pos = new_pos
        return self._pos

    @property
    def at_end(self) -> bool:
        return self._pos >= len(self._data)

    # -- primitive reads -----------------------------------------------------
    def _take(self, count: int) -> bytes:
        end = self._pos + count
        if count < 0 or end > len(self._data):
            raise EndOfStreamError(
                f"tried to read {count} byte(s) at offset {self._pos} but "
                f"{self.path or '<buffer>'} is only {len(self._data)} byte(s) long"
            )
        chunk = self._data[self._pos : end]
        self._pos = end
        return bytes(chunk)

    def read_bytes(self, count: int) -> bytes:
        return self._take(count)

    def read_byte(self) -> int:
        """``BinaryReader.ReadByte`` -- unsigned 8-bit."""
        return self._take(1)[0]

    def read_sbyte(self) -> int:
        """``BinaryReader.ReadSByte`` -- signed 8-bit."""
        return struct.unpack("<b", self._take(1))[0]

    def read_boolean(self) -> bool:
        """``BinaryReader.ReadBoolean`` -- non-zero is True."""
        return self._take(1)[0] != 0

    def read_int16(self) -> int:
        return struct.unpack("<h", self._take(2))[0]

    def read_uint16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def read_int32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def read_int64(self) -> int:
        return struct.unpack("<q", self._take(8))[0]

    def read_single(self) -> float:
        """``BinaryReader.ReadSingle`` -- 32-bit float widened to Python float."""
        return struct.unpack("<f", self._take(4))[0]

    def read_double(self) -> float:
        return struct.unpack("<d", self._take(8))[0]

    # -- character reads -----------------------------------------------------
    def read_chars(self, count: int) -> str:
        """``BinaryReader.ReadChars`` with ASCII: exactly ``count`` bytes -> characters."""
        return decode_ascii_bytes(self._take(count))

    def read_char(self) -> str:
        """``BinaryReader.ReadChar`` with ASCII."""
        return decode_ascii_bytes(self._take(1))

    def peek_char(self) -> int:
        """``BinaryReader.PeekChar`` -- the next byte as an int, or ``-1`` at EOF.

        .NET returns the *decoded character* as an int, which for ASCII is the byte
        value.  Callers only ever compare it against ``0`` or ``-1``.
        """
        if self._pos >= len(self._data):
            return -1
        return self._data[self._pos]

    def read_null_terminated_string(self) -> str:
        """Implement ``FileManager.ReadNullTerminatedString(BinaryReader)``."""
        chars: list[str] = []
        while self.peek_char() > 0:
            chars.append(self.read_char())
        # Consume the NUL terminator (the original does this unconditionally).
        if self.peek_char() == 0:
            self.read_char()
        return "".join(chars)

    # -- convenience ---------------------------------------------------------
    def read_fixed_string(self, count: int) -> str:
        """Read a fixed-size char array and strip NUL padding at both ends.

        This is the equivalent of the parser's ``CStr(field).Trim(Chr(0))``.
        """
        return trim_null_chars(self.read_chars(count))

    def slice(self, offset: int, count: int) -> bytes:
        """Read ``count`` bytes at ``offset`` without disturbing the position."""
        saved = self._pos
        try:
            self.seek(offset, SEEK_BEGIN)
            return self._take(count)
        finally:
            self._pos = saved

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"BinaryReader({self.path or '<buffer>'}, pos={self._pos}/{len(self._data)})"
