"""Recognize strong binary name-replacement evidence before conversion."""
import struct

from .core.mdl_data import SourceMdlFileData49
from .runtime_options import option_enabled


_REPLACEMENT_BYTES = frozenset(b'"<>:|?*\\/`\r\n\t')


def has_unsupported_names(raw: bytes, header: SourceMdlFileData49) -> bool:
    """Inspect names only, without decoding animation or allocating MDL records.

    Isolated quote/backtick issues are repairable. Multiple names made solely
    of a mixture of reserved symbols are a different, destructive pattern.
    This recognizes that pattern, not author intent or generic corruption.
    """
    tables = [(header.localSequenceCount, header.localSequenceOffset, 212, 4)]
    if not option_enabled(0):
        tables.insert(0, (header.boneCount, header.boneOffset, 216, 0))
    for count, start, stride, field in tables:
        # Ordinary malformed/truncated tables remain ordinary parser errors;
        # their counts must never drive an unbounded probe or classification.
        if count < 2 or start <= 0 or start > len(raw) or count > (len(raw) - start) // stride:
            continue
        affected = 0
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
        if affected >= 2:
            return True
    return False
