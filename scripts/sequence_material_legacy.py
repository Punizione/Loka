"""Offline migration reader for v3 authoring bundles; never imported by Loka."""
from array import array
import hashlib
import json
from pathlib import Path
import struct
import zlib


def read_legacy_template(folder, rule):
    from loka.sequence_material_vtf import _block_size, validate_product_header
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads((folder/'manifest.json').read_bytes())
    if manifest['schema_version'] != 3:
        raise ValueError('Migration requires the original schema-3 authoring data')
    entry = manifest['entries'][rule.output_name]
    name = entry['bundle']
    if Path(name).name != name or not name.endswith('.stpl'):
        raise ValueError('Invalid legacy bundle name')
    path = folder/name
    if not path.resolve().is_relative_to(folder):
        raise ValueError('Legacy bundle escapes authoring folder')
    blob = path.read_bytes()
    if len(blob) != entry['bundle_bytes'] or hashlib.sha256(blob).hexdigest() != entry['bundle_sha256']:
        raise ValueError('Legacy authoring checksum mismatch')
    magic, baseline_size, reference_size = struct.unpack_from('<8sII', blob)
    pixels = rule.size**2
    if magic != b'LKTPL3\0\0' or baseline_size != pixels*4:
        raise ValueError('Invalid legacy authoring header')
    raw = zlib.decompress(blob[16:])
    if len(raw) != pixels*(6+rule.frames)+reference_size:
        raise ValueError('Invalid legacy authoring length')
    baseline = raw[:baseline_size]
    phases = raw[pixels*6:pixels*(6+rule.frames)]
    if (hashlib.sha256(baseline).hexdigest() != entry['baseline_sha256']
            or hashlib.sha256(phases).hexdigest() != entry['phase_sha256']):
        raise ValueError('Legacy analysis checksum mismatch')
    encoded = raw[pixels*(6+rule.frames):]
    validate_product_header(encoded, rule)
    restored = bytearray(encoded[:80])
    offset = 80
    for mip in reversed(range(rule.size.bit_length())):
        size = _block_size(max(1, rule.size >> mip), rule.image_format)
        previous = None
        for _ in range(rule.frames):
            frame = array('Q')
            frame.frombytes(encoded[offset:offset+size])
            offset += size
            if previous is not None:
                frame = array('Q', (a ^ b for a,b in zip(frame,previous)))
            restored.extend(frame.tobytes())
            previous = frame
    reference = bytes(restored)
    if hashlib.sha256(reference).hexdigest() != entry['reference_sha256']:
        raise ValueError('Legacy reference checksum mismatch')
    return baseline, reference, tuple(phases[i*pixels:(i+1)*pixels] for i in range(rule.frames)), entry
