"""Recognize strong binary name-replacement evidence before conversion."""
import struct

from .core.mdl_data import SourceMdlFileData49


_REPLACEMENT_BYTES = frozenset(b'"<>:|?*\\/`\r\n\t')


def unsupported_name_reason(raw: bytes, header: SourceMdlFileData49) -> str | None:
    """Inspect names only, without decoding animation or allocating MDL records.

    Isolated quote/backtick issues are repairable. Multiple names made solely
    of a mixture of reserved symbols are a different, destructive pattern.
    This recognizes that pattern, not author intent or generic corruption.
    """
    for label, count, start, stride, field in (
        ('骨骼', header.boneCount, header.boneOffset, 216, 0),
        ('动作序列', header.localSequenceCount, header.localSequenceOffset, 212, 4),
    ):
        # Ordinary malformed/truncated tables remain ordinary parser errors;
        # their counts must never drive an unbounded probe or classification.
        if count < 2 or start <= 0 or start > len(raw) or count > (len(raw) - start) // stride:
            continue
        affected = 0
        examples = []
        for index in range(count):
            record = start + index * stride
            relative = struct.unpack_from('<i', raw, record + field)[0]
            offset = record + relative
            if not relative or not 0 <= offset < len(raw):
                continue
            # A bounded prefix is enough for this signature. Do not infer a
            # missing terminator or truncate a long legitimate name to match.
            end = raw.find(b'\0', offset, min(len(raw), offset + 4096))
            if end < 0:
                continue
            value = raw[offset:end]
            symbols = set(value)
            if len(symbols) < 2 or not symbols <= _REPLACEMENT_BYTES:
                continue
            affected += 1
            if len(examples) < 3:
                examples.append(f'{label}[{index}]@0x{offset:X}={value[:64]!r}')
        if affected >= 2:
            return (f'MDL {label}名称表异常：{affected}/{count} 个名称仅由混合非法符号或控制字符组成，'
                    '疑似被人为替换，当前不支持；' + '；'.join(examples))
    return None
