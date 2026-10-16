"""Read-only VPK v1/v2 indexing and safe extraction for L4D2 assets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import zlib


@dataclass(frozen=True)
class VpkEntry:
    crc: int
    preload: bytes
    archive: int
    offset: int
    length: int


_DEVICES = {'con', 'prn', 'aux', 'nul'} | {f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10)}


def safe_archive_path(value: str) -> str:
    value = value.replace('\\', '/')
    parts = value.split('/')
    if not value or any(p in ('', '.', '..') or p.endswith(('.', ' ')) or
                        any(c in p for c in ':\x00\r\n"<>|?*') or
                        p.split('.')[0].lower() in _DEVICES for p in parts):
        raise ValueError(f'不安全的 VPK 文件路径: {value!r}')
    return '/'.join(parts)


class VpkArchive:
    def __init__(self, path: Path, *, include_all: bool = False):
        self.path = Path(path)
        self.include_all = include_all
        self.original_names: dict[str, str] = {}
        path = self.path
        with path.open("rb") as handle:
            header = handle.read(12)
            if len(header) != 12:
                raise ValueError(f"VPK 文件头不完整: {path}")
            signature, version, size = struct.unpack("<III", header)
            if signature != 0x55AA1234 or version not in (1, 2):
                raise ValueError(f"不支持的 VPK 文件头: {path}")
            self.data_offset = (28 if version == 2 else 12) + size
            if self.data_offset > path.stat().st_size:
                raise ValueError(f"VPK 目录长度越界: {path}")
            if version == 2:
                extra = handle.read(16)
                if len(extra) != 16:
                    raise ValueError(f'VPK v2 文件头不完整: {path}')
                self.data_size = struct.unpack('<IIII', extra)[0]
            else:
                self.data_size = path.stat().st_size - self.data_offset
            if self.data_offset + self.data_size > path.stat().st_size:
                raise ValueError(f'VPK 数据段长度越界: {path}')
            handle.seek(28 if version == 2 else 12)
            tree = handle.read(size)
        pos = 0
        self.entries: dict[str, VpkEntry] = {}

        def read_string():
            nonlocal pos
            try:
                end = tree.index(b"\0", pos)
            except ValueError as error:
                raise ValueError(f"VPK 目录字符串不完整: {path}") from error
            value = tree[pos:end].decode("utf-8", errors="replace")
            pos = end + 1
            return value

        while extension := read_string():
            while directory := read_string():
                while name := read_string():
                    if pos + 18 > len(tree):
                        raise ValueError(f"VPK 条目不完整: {path}")
                    crc, preload, archive, offset, length, terminator = struct.unpack_from("<IHHIIH", tree, pos)
                    pos += 18
                    if terminator != 0xffff or pos + preload > len(tree):
                        raise ValueError(f"VPK 条目长度无效: {path}")
                    prefix = "" if directory == " " else directory + "/"
                    filename = prefix + name + ('.' + extension if extension != ' ' else '')
                    filename = filename.replace('\\', '/')
                    key = filename.lower()
                    if include_all or extension.lower() in ("mdl", "vvd", "vtx", "phy", "ani"):
                        if key in self.entries:
                            raise ValueError(f'VPK 有重复或大小写冲突的路径: {filename}')
                        self.entries[key] = VpkEntry(crc, tree[pos:pos + preload], archive, offset, length)
                        self.original_names[key] = filename
                    pos += preload

    def read(self, path: str) -> bytes | None:
        entry = self.entries.get(path.replace("\\", "/").lower())
        if entry is None:
            return None
        payload = b""
        if entry.length:
            if entry.archive == 0x7fff:
                archive = self.path
                offset = self.data_offset + entry.offset
                limit = self.data_offset + self.data_size
            else:
                base = self.path.name[:-8] if self.path.name.lower().endswith('_dir.vpk') else self.path.stem
                archive = self.path.with_name(base + f"_{entry.archive:03d}.vpk")
                offset = entry.offset
                limit = archive.stat().st_size
            if offset + entry.length > limit:
                raise ValueError(f'VPK 文件数据范围越界: {path} ({archive})')
            with archive.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(entry.length)
            if len(payload) != entry.length:
                raise ValueError(f"VPK 数据不完整: {path} ({archive})")
        raw = entry.preload + payload
        if (self.include_all or entry.crc) and zlib.crc32(raw) != entry.crc:
            raise ValueError(f"VPK 校验和不匹配: {path}")
        return raw

    def extract_to(self, destination: Path, *, cancelled=None) -> dict[str, Path]:
        """Preserve stored spelling/hierarchy, refusing overwrites and path escapes."""
        root = Path(destination).resolve()
        targets = {}
        for key in self.entries:
            rel = safe_archive_path(self.original_names[key])
            target = root / rel
            if not target.resolve().is_relative_to(root):
                raise ValueError(f'VPK 解包路径越界: {rel}')
            if target.exists():
                raise FileExistsError(f'VPK 解包目标已存在: {target}')
            targets[key] = target
        file_keys = set(targets)
        for key in targets:
            parts = key.split('/')
            if any('/'.join(parts[:i]) in file_keys for i in range(1, len(parts))):
                raise ValueError(f'VPK 文件与目录路径冲突: {key}')
        root.mkdir(parents=True, exist_ok=True)
        for key, target in targets.items():
            if cancelled and cancelled():
                raise InterruptedError('VPK 解包已取消')
            raw = self.read(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle:
                handle.write(raw)
        return targets
