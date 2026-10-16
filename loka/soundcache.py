"""Bounded PCM WAV metadata and the offline Source sound.cache wire format.

The codec is independent of conversion/GUI state and never launches a utility.
Only admitted PCM inputs reproduce the researched utility's serialized fields.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
import tempfile

from .vpk import safe_archive_path


_HEADER = struct.Struct('<4I')
_LENGTH = struct.Struct('<I')
_TAIL = struct.Struct('<IIBIIiI')
_MAX_U32 = 0xffffffff


@dataclass(frozen=True)
class CachedSound:
    path: str
    file_size: int
    format_bits: int
    data_offset: int
    data_size: int
    loop_start: int
    sample_count: int


@dataclass(frozen=True)
class SoundCacheResult:
    path: str | None
    entries: tuple[CachedSound, ...]
    sha256: str


def _cache_path(value: str) -> bytes:
    if '\\' in value or not value.startswith('sound/') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'Unsafe sound cache path: {value!r}')
    safe_archive_path(value)
    return value[6:].encode('utf-8')


def read_sound_cache(data: bytes) -> tuple[CachedSound, ...]:
    """Decode framed cache records, retaining opaque packed format words."""
    if len(data) < _HEADER.size:
        raise ValueError('Truncated sound cache header')
    version, kind, reserved, count = _HEADER.unpack_from(data)
    if (version, kind, reserved) != (2, 3, 0):
        raise ValueError('Unsupported sound cache header')
    # Each body needs at least sound/a, a NUL and its 25-byte tail.
    if count > (len(data) - _HEADER.size) // (4 + 8 + _TAIL.size):
        raise ValueError('Sound cache count exceeds physical record bounds')
    entries = []
    offset = _HEADER.size
    previous = None
    seen = set()
    for _ in range(count):
        if offset + _LENGTH.size > len(data):
            raise ValueError('Truncated sound cache record length')
        size, = _LENGTH.unpack_from(data, offset)
        offset += _LENGTH.size
        end = offset + size
        if size < _TAIL.size + 2 or end > len(data):
            raise ValueError('Invalid sound cache record bounds')
        path_end = end - _TAIL.size
        raw = data[offset:path_end]
        if not raw.endswith(b'\0') or b'\0' in raw[:-1]:
            raise ValueError('Invalid sound cache path terminator or tail size')
        try:
            path = raw[:-1].decode('utf-8', errors='strict')
        except UnicodeDecodeError as error:
            raise ValueError('Invalid sound cache UTF-8 path') from error
        key = _cache_path(path)
        collision = path.casefold()
        if collision in seen or (previous is not None and key <= previous):
            raise ValueError('Duplicate, colliding or unsorted sound cache path')
        seen.add(collision)
        previous = key
        file_size, packed, zero, data_offset, data_size, loop, frames = _TAIL.unpack_from(data, path_end)
        if zero:
            raise ValueError('Nonzero sound cache record reserved byte')
        entries.append(CachedSound(path, file_size, packed, data_offset, data_size, loop, frames))
        offset = end
    if offset != len(data):
        raise ValueError('Trailing sound cache bytes')
    return tuple(entries)


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError('Sound cache build cancelled')


def _is_alias(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _real_path(path: Path):
    # Do not resolve an alias into a seemingly safe path before checking it.
    for component in (path, *path.parents):
        if _is_alias(component):
            raise ValueError(f'Symlink/junction aliases are unsupported: {component}')


def _read_wave(path: Path, relative: str, cancelled) -> CachedSound:
    def bad(message):
        raise ValueError(f'{path}: {message}')

    _check_cancel(cancelled)
    with path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        size = before.st_size
        if size > _MAX_U32:
            bad('WAV file size exceeds 32-bit cache fields')

        def read_at(position, length):
            _check_cancel(cancelled)
            if position < 0 or position + length > size:
                bad('WAV metadata exceeds physical file bounds')
            handle.seek(position)
            payload = handle.read(length)
            if len(payload) != length:
                bad('Truncated WAV metadata')
            return payload

        header = read_at(0, 12)
        if header[:4] != b'RIFF' or header[8:12] != b'WAVE':
            bad('Expected RIFF/WAVE')
        position = 12
        selected = None
        data_offset = data_size = None
        loop = -1
        while position < size:
            chunk_header = read_at(position, 8)
            tag, amount = struct.unpack('<4sI', chunk_header)
            payload_at = position + 8
            next_position = payload_at + amount + (amount & 1)
            if next_position > size:
                bad(f'Truncated {tag!r} chunk or odd padding')
            if selected is None:
                if tag == b'fmt ' and 16 <= amount <= 1024:
                    fields = struct.unpack('<HHIIHH', read_at(payload_at, 16))
                    if fields[0]:
                        codec, channels, rate, byte_rate, alignment, bits = fields
                        if codec != 1 or bits not in (8, 16, 24) or channels not in (1, 2):
                            bad('Only PCM8/16/24 mono/stereo is supported')
                        width = channels * (bits // 8)
                        if not 0 < rate <= 0x1ffff or alignment != width or byte_rate != rate * width:
                            bad('Unrepresentable sample rate or invalid PCM alignment/byte rate')
                        selected = (codec, channels, rate, bits, width)
            elif tag == b'data':
                if amount % selected[4]:
                    bad('Data payload is not a whole number of PCM frames')
                data_offset, data_size = payload_at, amount
            elif tag == b'cue ':
                if amount < 4:
                    bad('Short cue header')
                count, = struct.unpack('<I', read_at(payload_at, 4))
                if 4 + count * 24 > amount:
                    bad('Truncated cue point table')
                if count:
                    loop, = struct.unpack('<I', read_at(payload_at + 24, 4))
            elif tag == b'smpl':
                if amount < 36:
                    bad('Short sampler header')
                sampler = read_at(payload_at, 36)
                count, = struct.unpack_from('<I', sampler, 28)
                if 36 + count * 24 > amount:
                    bad('Truncated sampler loop table')
                if count:
                    kind, start = struct.unpack('<II', read_at(payload_at + 40, 8))
                    if kind == 0:
                        loop = start
            elif tag == b'VDAT':
                bad('Sentence audio VDAT is unsupported')
            position = next_position
        if selected is None:
            bad('No qualifying fmt chunk')
        if data_offset is None:
            bad('No data chunk after selected fmt')
        codec, channels, rate, bits, width = selected
        frames = data_size // width
        if loop != -1 and (loop > 0x7fffffff or loop >= frames):
            bad('Loop start is outside the sample frames')
        after = os.fstat(handle.fileno())
        current = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
                after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) or (
                current.st_ino, current.st_dev) != (before.st_ino, before.st_dev):
            bad('WAV changed while reading metadata')
    packed_width = ((bits * channels) >> 3) & 7 or 1
    packed = (1 | (bits & 31) << 2 | (channels & 3) << 7 | packed_width << 9
              | (codec & 3) << 12 | (rate & 0x1ffff) << 14)
    return CachedSound('sound/' + relative, size, packed, data_offset, data_size, loop, frames)


def build_sound_cache(sound_dir, *, output_path=None, cancelled=None, on_log=None) -> SoundCacheResult:
    """Collect an explicit sound root and atomically publish a complete WAV cache."""
    _check_cancel(cancelled)
    root = Path(os.path.abspath(sound_dir))
    _real_path(root)
    if root.name != 'sound' or not root.is_dir():
        raise ValueError(f'Expected an explicit real directory named sound: {root}')
    output = root / 'sound.cache' if output_path is None else Path(os.path.abspath(output_path))
    _real_path(output)
    try:
        output.relative_to(root)
    except ValueError as error:
        raise ValueError(f'Cache output escapes sound root: {output}') from error
    if output == root or not output.parent.is_dir():
        raise ValueError(f'Cache output parent must exist inside sound root: {output}')
    waves = []
    seen_paths = set()
    identities = set()
    def traversal_error(error):
        raise OSError(error.errno, 'Cannot read sound directory: ' + str(error),
                      error.filename or str(root)) from error

    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=traversal_error):
        _check_cancel(cancelled)
        for name in directories + filenames:
            candidate = Path(directory) / name
            if _is_alias(candidate):
                raise ValueError(f'Symlink/junction aliases are unsupported: {candidate}')
        for name in filenames:
            _check_cancel(cancelled)
            path = Path(directory) / name
            if path.suffix == '.mp3':
                raise ValueError(f'{path}: MP3 is unsupported by WAV-only sound cache')
            if path.suffix != '.wav':
                continue
            if not path.is_file():
                raise ValueError(f'WAV input is not a regular file: {path}')
            relative = path.relative_to(root).as_posix()
            key = _cache_path('sound/' + relative)
            collision = relative.casefold()
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if collision in seen_paths or identity in identities:
                raise ValueError(f'Colliding or aliased WAV input: {path}')
            if output == path or (output.exists() and output.samefile(path)):
                raise ValueError(f'Cache output aliases an input WAV: {path}')
            seen_paths.add(collision)
            identities.add(identity)
            waves.append((key, relative, path))
    waves.sort(key=lambda item: item[0])
    entries = []
    for _, relative, path in waves:
        _check_cancel(cancelled)
        if on_log is not None:
            on_log('Reading WAV metadata: ' + relative)
        entries.append(_read_wave(path, relative, cancelled))
    if not entries:
        return SoundCacheResult(None, (), '')
    payload = bytearray(_HEADER.pack(2, 3, 0, len(entries)))
    for entry in entries:
        _check_cancel(cancelled)
        body = entry.path.encode('utf-8') + b'\0' + _TAIL.pack(
            entry.file_size, entry.format_bits, 0, entry.data_offset, entry.data_size,
            entry.loop_start, entry.sample_count)
        payload += _LENGTH.pack(len(body)) + body
    encoded = bytes(payload)
    decoded = read_sound_cache(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    temporary = None
    try:
        _check_cancel(cancelled)
        with tempfile.NamedTemporaryFile(mode='wb', prefix='.' + output.name + '.', suffix='.tmp',
                                         dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _check_cancel(cancelled)
        _real_path(output)
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return SoundCacheResult(str(output), decoded, digest)
