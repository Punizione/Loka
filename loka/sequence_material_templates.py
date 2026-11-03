"""Embedded, UV-local RGB/alpha transfer from authoritative animated VTFs.

Each bundle holds the original target-resolution static baseline, per-pixel
temporal maxima, construction stages and reference VTF. No UV sweep is invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from array import array
import hashlib
import json
from pathlib import Path
import struct
import zlib


TEMPLATE_METHOD = 'uv-rgba-tone-anchors-v1'
TEMPLATE_MAGIC = b'LKTPL3\0\0'
REFERENCE_ENCODING = 'mip-frame-xor64-v1'
TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / 'data' / 'sequence_materials'
_MAX_BUNDLE = 32 * 1024 * 1024


@dataclass(frozen=True)
class MaterialTemplate:
    digest: str
    source_digest: str
    baseline: bytes
    peak_rgb: bytes
    peak_alpha: bytes
    reference: bytes
    sweep_color: tuple[int, int, int] | None
    phases: bytes
    wire_color: tuple[int, int, int] | None = None

    def phase_frame(self, index):
        pixels = len(self.baseline) // 4
        if not 0 <= index < len(self.phases) // pixels:
            raise ValueError('材质阶段帧索引超出范围。')
        return self.phases[index * pixels:(index + 1) * pixels]


def default_sweep_color(recipe_id, rule):
    """Read only the small per-output color index for UI palette initialization."""
    from .sequence_material_recipes import get_recipe
    from .sequence_material_colors import SWEEP_COLOR_METHOD, validate_sweep_color
    recipe = get_recipe(recipe_id)
    if rule not in recipe.rules:
        raise ValueError('当前选项不属于所选材质配方。')
    if rule.layer == 'normal':
        return None
    try:
        with (TEMPLATE_DIRECTORY / recipe.identifier / 'manifest.json').open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError('材质模板索引过大。')
        entry = json.loads(raw)['entries'][rule.output_name]
        if entry['sweep_color_method'] != SWEEP_COLOR_METHOD:
            raise ValueError('内置扫过颜色数据缺失或损坏。')
        color = validate_sweep_color(entry['sweep_color_rgb'])
        return color
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('内置扫过颜色数据缺失或损坏。') from error


def default_wire_color(recipe_id, rule):
    from .sequence_material_recipes import get_recipe
    from .sequence_material_colors import WIRE_COLOR_METHOD, validate_sweep_color
    recipe = get_recipe(recipe_id)
    if rule not in recipe.rules:
        raise ValueError('当前选项不属于所选材质配方。')
    if rule.layer in ('normal', 'glow'):
        return None
    try:
        with (TEMPLATE_DIRECTORY / recipe.identifier / 'manifest.json').open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError('材质模板索引过大。')
        entry = json.loads(raw)['entries'][rule.output_name]
        if entry.get('wire_color_method') != WIRE_COLOR_METHOD:
            return None
        return validate_sweep_color(entry.get('wire_color_rgb'))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('内置线框颜色数据缺失或损坏。') from error


def load_material_template(recipe_id, rule, *, cancelled=lambda: False):
    from .sequence_material_recipes import get_recipe
    from .sequence_material_vtf import check_cancel
    from .sequence_material_phases import MATERIAL, PHASE_METHOD
    recipe = get_recipe(recipe_id)
    if rule not in recipe.rules:
        raise ValueError('当前选项不属于所选材质配方。')
    check_cancel(cancelled)
    folder = TEMPLATE_DIRECTORY / recipe.identifier
    try:
        with (folder / 'manifest.json').open('rb') as stream:
            manifest_bytes = stream.read(256 * 1024 + 1)
        if len(manifest_bytes) > 256 * 1024:
            raise ValueError('材质模板索引过大。')
        manifest = json.loads(manifest_bytes)
        if (manifest['schema_version'] != 3 or manifest['method'] != TEMPLATE_METHOD
                or manifest['phase_method'] != PHASE_METHOD
                or manifest['reference_encoding'] != REFERENCE_ENCODING
                or manifest['recipe_id'] != recipe_id):
            raise ValueError('材质模板版本与配方不匹配。')
        entry = manifest['entries'][rule.output_name]
        if (entry['source'] != rule.source_name or entry['size'] != rule.size
                or entry['frames'] != rule.frames or entry['format'] != rule.image_format
                or entry['flags'] != rule.flags):
            raise ValueError('材质模板的源图或输出规格与配方不匹配。')
        filename = entry['bundle']
        if Path(filename).name != filename or not filename.endswith('.stpl'):
            raise ValueError('材质模板资源路径无效。')
        path = folder / filename
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('材质模板资源路径无效。')
        with path.open('rb') as stream:
            blob = stream.read(_MAX_BUNDLE + 1)
        if not 16 <= len(blob) <= _MAX_BUNDLE:
            raise ValueError('材质模板资源长度无效。')
        digest = hashlib.sha256(blob).hexdigest()
        if digest != entry['bundle_sha256']:
            raise ValueError('材质模板资源校验失败。')
        magic, baseline_size, reference_size = struct.unpack_from('<8sII', blob)
        pixels = rule.size * rule.size
        if magic != TEMPLATE_MAGIC or baseline_size != pixels * 4 or not 80 < reference_size < _MAX_BUNDLE:
            raise ValueError('材质模板数据头无效。')
        expected = pixels * (6 + rule.frames) + reference_size
        check_cancel(cancelled)
        decoder = zlib.decompressobj()
        payload = decoder.decompress(blob[16:], expected + 1)
        if (len(payload) != expected or not decoder.eof or decoder.unconsumed_tail
                or decoder.unused_data):
            raise ValueError('材质模板解压长度无效。')
        baseline = payload[:baseline_size]
        peak_rgb = payload[baseline_size:baseline_size + pixels]
        peak_alpha = payload[baseline_size + pixels:baseline_size + pixels * 2]
        phase_start = baseline_size + pixels * 2
        phases = payload[phase_start:phase_start + pixels * rule.frames]
        if any(value > MATERIAL for value in phases) or hashlib.sha256(phases).hexdigest() != entry['phase_sha256']:
            raise ValueError('材质阶段数据校验失败。')
        reference = transform_reference_frames(payload[phase_start + pixels * rule.frames:], rule,
                                               decode=True, cancelled=cancelled)
        check_cancel(cancelled)
        if (hashlib.sha256(baseline).hexdigest() != entry['baseline_sha256']
                or hashlib.sha256(reference).hexdigest() != entry['reference_sha256']):
            raise ValueError('材质模板参考数据校验失败。')
        identity = hashlib.sha256(TEMPLATE_METHOD.encode() + manifest_bytes + bytes.fromhex(digest)).hexdigest()
        from .sequence_material_colors import SWEEP_COLOR_METHOD, WIRE_COLOR_METHOD, validate_sweep_color
        if entry['sweep_color_method'] != SWEEP_COLOR_METHOD:
            raise ValueError('内置扫过颜色数据缺失或损坏。')
        color = validate_sweep_color(entry['sweep_color_rgb'])
        if rule.layer == 'normal' and color is not None:
            raise ValueError('内置扫过颜色数据缺失或损坏。')
        wire_color = validate_sweep_color(entry.get('wire_color_rgb'))
        if wire_color is not None and (entry.get('wire_color_method') != WIRE_COLOR_METHOD
                                       or rule.layer in ('normal', 'glow')):
            raise ValueError('内置线框颜色数据缺失或损坏。')
        return MaterialTemplate(identity, entry['source_sha256'], baseline,
                                peak_rgb, peak_alpha, reference, color, phases, wire_color)
    except FileNotFoundError as error:
        raise ValueError('缺少内置材质效果模板，请补齐配方资源；不会回退为统一 UV 扫描。') from error
    except (KeyError, TypeError, struct.error, json.JSONDecodeError, zlib.error) as error:
        raise ValueError('内置材质效果模板索引或数据损坏。') from error


def transform_reference_frames(data, rule, *, decode=False, cancelled=lambda: False):
    """Lossless temporal XOR inside each mip; improves zlib's small window.

    The VTF header and every frame's byte count stay intact. XOR groups of eight
    bytes only reduce Python loop overhead; the byte transform is endian-neutral.
    """
    from .sequence_material_vtf import _block_size, check_cancel, validate_product_header
    validate_product_header(data, rule)
    result = bytearray(data[:80])
    offset = 80
    for mip in reversed(range(rule.size.bit_length())):
        size = _block_size(max(1, rule.size >> mip), rule.image_format)
        previous = None
        for frame in range(rule.frames):
            check_cancel(cancelled)
            raw = data[offset:offset + size]
            offset += size
            current = array('Q')
            current.frombytes(raw)
            if previous is not None:
                restored = array('Q', (a ^ b for a, b in zip(current, previous)))
            else:
                restored = current
            result.extend(restored.tobytes())
            previous = restored if decode else current
    return bytes(result)


def _relative_channel(new, baseline, reference, fallback):
    """Anchor black, reference-static and white; preserve reference at identity.

    This transfers the measured darkening/highlighting at this UV coordinate
    and this frame. It neither estimates frame timing nor assumes monotonicity.
    """
    if new == baseline:
        return reference
    if reference < baseline:
        return (reference * new + baseline // 2) // baseline
    if reference > baseline:
        denominator = 255 - baseline
        return 255 - ((255 - reference) * (255 - new) + denominator // 2) // denominator
    if 0 < baseline < 255:
        return new
    # A saturated reference cannot identify its local transfer alone. Use the
    # other RGB components, or the reference's actual UV-local temporal gate.
    return max(0, min(255, round(reference + fallback * (new - baseline))))


def _measured_gain(baseline, reference):
    if reference < baseline:
        return reference / baseline
    if reference > baseline:
        return (255 - reference) / (255 - baseline)
    return 1. if 0 < baseline < 255 else None


def transfer_template_frame(static, reference, template, rule, frame_index, *, cancelled=lambda: False):
    from .sequence_material_vtf import check_cancel
    from .sequence_material_phases import MATERIAL
    from PIL import Image
    current = static.tobytes()
    base = template.baseline
    output = bytearray(len(current))
    phases = template.phase_frame(frame_index)
    for at in range(0, len(current), 4):
        pixel = at // 4
        if pixel % (rule.size * 16) == 0:
            check_cancel(cancelled)
        if phases[pixel] != MATERIAL:
            output[at:at + 4] = reference[at:at + 4]
            continue
        if rule.layer == 'glow':
            peak = template.peak_rgb[pixel]
            gate = max(reference[at:at + 3]) / peak if peak else 0.
        else:
            peak = template.peak_alpha[pixel]
            gate = reference[at + 3] / peak if peak else 0.
        gains = [_measured_gain(base[at + c], reference[at + c]) for c in range(3)]
        measured = [g for g in gains if g is not None]
        fallback = min(measured) if measured else gate
        for channel in range(3):
            index = at + channel
            output[index] = _relative_channel(current[index], base[index], reference[index], fallback)
        if rule.layer == 'glow':
            output[at + 3] = 255
        else:
            # Geometry visibility is authored in the animation's alpha, not in
            # the static image's often unrelated gloss/specular alpha channel.
            # Exact zero/one visibility anchors must remain zero/one.
            alpha = reference[at + 3]
            output[at + 3] = alpha if alpha in (0, 255) else _relative_channel(
                current[at + 3], base[at + 3], alpha, gate)
    return Image.frombytes('RGBA', static.size, bytes(output))
