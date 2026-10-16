"""Validate engine section addresses independently of tolerant MDL decoding.

Some compilers share a physical section table between animation descriptors,
but leave its MDL-relative entries based on a different descriptor. The runtime
adds each entry to the descriptor actually playing it. Only proven shared-table
rebasing is repaired, in private compile staging, without re-encoding frames.
"""
from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import struct
import tempfile


def _fail(detail):
    raise ValueError('编译 MDL 动作分段校验失败: ' + detail)


def _span(raw, offset, size):
    if offset < 80 or size < 0 or offset > len(raw) - size:
        _fail('数据地址越界')


@dataclass(frozen=True)
class _Animation:
    start: int
    flags: int
    frames: int
    block: int
    offset: int
    table: int
    section_frames: int
    entries: tuple

    @property
    def delta(self):
        return self.offset - self.entries[0][1]


def _read(raw):
    if (len(raw) < 188 or len(raw) > 2147483647 or raw[:4] != b'IDST'
            or struct.unpack_from('<i', raw, 4)[0] not in (48, 49)
            or struct.unpack_from('<i', raw, 76)[0] != len(raw)):
        _fail('不支持的标头/长度')
    bones = struct.unpack_from('<i', raw, 156)[0]
    count, offset = struct.unpack_from('<ii', raw, 180)
    if bones < 0 or bones > 256 or count < 0:
        _fail('骨骼/动作数量无效')
    if count:
        _span(raw, offset, count * 100)
    animations = []
    for index in range(count):
        start = offset + index * 100
        flags, frames = struct.unpack_from('<ii', raw, start + 12)
        block, payload = struct.unpack_from('<ii', raw, start + 52)
        section, step = struct.unpack_from('<ii', raw, start + 80)
        if not step and not section:
            continue
        if step <= 0 or section <= 0 or frames <= 0:
            _fail('分段描述无效')
        table = start + section
        size = frames // step + 2
        _span(raw, table, size * 8)
        entries = tuple(struct.unpack_from('<ii', raw, table + n * 8) for n in range(size))
        if block < 0 or any(b < 0 or p < 0 or (not b and not p) for b, p in entries):
            _fail('分段块/偏移无效')
        animations.append(_Animation(start, flags, frames, block, payload, table, step, entries))
    return bones, animations


def _reachable(animation):
    # The engine stores the last frame of a long animation separately. Entries
    # between the preceding playable section and this last frame may be unused.
    if animation.frames > animation.section_frames:
        for index in range((animation.frames - 2) // animation.section_frames + 1):
            yield index, min(animation.section_frames + 1, animation.frames - index * animation.section_frames)
        yield animation.frames // animation.section_frames + 1, 1
    else:
        yield 0, animation.frames


def _rle(raw, address, bones, frames):
    previous = -1
    for _ in range(bones + 1):
        _span(raw, address, 4)
        bone, flags, next_offset = struct.unpack_from('<BBh', raw, address)
        if bone == 255:
            return
        if not previous < bone < bones or flags & ~0x3F:
            _fail('骨骼记录/标志无效')
        if (flags & 1 and flags & 4) or (flags & 2 and flags & (8 | 32)) or (flags & 32 and flags & 8):
            _fail('骨骼记录格式冲突')
        previous = bone
        cursor = address + 4
        cursor += (8 if flags & 32 else 0) + (6 if flags & 2 else 0) + (6 if flags & 1 else 0)
        for flag in (8, 4):
            if not flags & flag:
                continue
            _span(raw, cursor, 6)
            for relative in struct.unpack_from('<3h', raw, cursor):
                if relative < 0 or 0 < relative < 6:
                    _fail('RLE 通道指针无效')
                if not relative:
                    continue
                channel = cursor + relative
                covered = 0
                while covered < frames:
                    _span(raw, channel, 2)
                    valid, total = raw[channel:channel + 2]
                    if not 0 < valid <= total:
                        _fail('RLE 通道帧数无效')
                    _span(raw, channel, 2 * (valid + 1))
                    covered += total
                    channel += 2 * (valid + 1)
            cursor += 6
        _span(raw, address, cursor - address)
        if not next_offset:
            return
        if next_offset < cursor - address:
            _fail('骨骼记录链无效')
        address += next_offset
    _fail('骨骼记录链未终止')


def _frame(raw, address, bones, frames):
    _span(raw, address, 24 + bones)
    constant, data, stride = struct.unpack_from('<iii', raw, address)
    flags = raw[address + 24:address + 24 + bones]
    constants = sum((6 if f & 1 else 0) + (6 if f & 2 else 0)
                    + (12 if f & 32 else 0) + (6 if f & 64 else 0) for f in flags)
    width = sum((6 if f & 4 else 0) + (6 if f & 8 else 0)
                + (12 if f & 16 else 0) + (6 if f & 128 else 0) for f in flags)
    if constants:
        if constant < 24 + bones:
            _fail('帧格式常量指针无效')
        _span(raw, address + constant, constants)
    if width:
        if stride < width or data < 24 + bones:
            _fail('帧格式数据指针/步长无效')
        _span(raw, address + data, stride * frames)


def _payloads(raw, bones, animation, checked, *, delta=0):
    internal = 0
    for index, frames in _reachable(animation):
        block, offset = animation.entries[index]
        if block:
            # External ANI offsets are relative to their animation block,
            # not to an MDL descriptor; this repair must never rebase them.
            continue
        internal += 1
        address = animation.start + offset + delta
        key = (address, frames, bool(animation.flags & 64))
        if key not in checked:
            (_frame if animation.flags & 64 else _rle)(raw, address, bones, frames)
            checked.add(key)
    return internal


def validate_animation_sections(raw):
    """Check raw addresses used by the engine; never apply decode recovery."""
    bones, animations = _read(raw)
    checked = set()
    internal = 0
    for animation in animations:
        first_block = animation.entries[0][0]
        if first_block != animation.block or animation.delta:
            _fail('描述符与首分段偏移不一致（descriptor=' + str(animation.start) + '）')
        internal += _payloads(raw, bones, animation, checked)
    return {'sectioned_animations': len(animations), 'internal_sections': internal}


def repair_compiled_animation_sections(target_mdl, *, staging_root):
    """Clone proven shared tables, verify in memory, then replace atomically."""
    target = Path(target_mdl)
    root = Path(staging_root).resolve()
    if (not root.name.startswith('.loka_compile_models_') or target.is_symlink()
            or not target.resolve().is_relative_to(root) or target.suffix.lower() != '.mdl'):
        _fail('修复目标不属于内部编译暂存目录')
    original = target.read_bytes()
    bones, animations = _read(original)
    groups = defaultdict(list)
    for animation in animations:
        groups[animation.table].append(animation)
    candidate = bytearray(original)
    receipts = []
    checked = set()
    for animation in animations:
        if not animation.delta:
            _payloads(original, bones, animation, checked)
    for animation in animations:
        if not animation.delta:
            continue
        users = groups[animation.table]
        if (len(users) < 2 or any(a.block or a.flags & 64 or any(b for b, _ in a.entries) for a in users)
                or len({(a.frames, a.section_frames) for a in users}) != 1):
            _fail('无法证明是共享分段表的相对基址错误')
        targets = {tuple(a.start + p + a.delta for _, p in a.entries) for a in users}
        if len(targets) != 1:
            _fail('共享分段表的恢复地址不一致')
        # Prove complete payloads against the ORIGINAL boundary. Appended
        # tables must not accidentally make truncated frames appear valid.
        _payloads(original, bones, animation, checked, delta=animation.delta)
        candidate.extend(b'\0' * (-len(candidate) % 4))
        table = len(candidate)
        if table + len(animation.entries) * 8 > 2147483647:
            _fail('MDL 长度溢出')
        for block, offset in animation.entries:
            corrected = offset + animation.delta
            if not 0 < corrected <= 2147483647:
                _fail('恢复偏移无效')
            # Even unused table entries must refer to original payload, never
            # to appended tables or bytes made valid by extending the file.
            _span(original, animation.start + corrected, 4)
            candidate.extend(struct.pack('<ii', block, corrected))
        struct.pack_into('<i', candidate, animation.start + 80, table - animation.start)
        receipts.append({'descriptor': animation.start, 'old_section_table': animation.table,
                         'new_section_table': table, 'delta': animation.delta,
                         'section_count': len(animation.entries)})
    struct.pack_into('<i', candidate, 76, len(candidate))
    validate_animation_sections(candidate)
    if not receipts:
        return []
    # The checksum is a companion-file identity, not a hash of the MDL. Retain
    # it (as for IK curve restoration) so VVD/VTX/PHY remain byte-identical.
    fd, temporary = tempfile.mkstemp(prefix='.section_verify_', suffix='.mdl', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
        if Path(temporary).read_bytes() != candidate:
            _fail('修复文件读回不一致')
        if target.read_bytes() != original:
            _fail('暂存模型在修复期间发生变化')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return receipts
