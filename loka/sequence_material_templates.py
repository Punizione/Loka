"""Load analyzed UV construction rules, never complete reference animations."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .sequence_material_rules import MaterialProgram
from .sequence_material_islands import ISLAND_METHOD, decode_island_program

TEMPLATE_METHOD = ISLAND_METHOD
TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / 'data' / 'sequence_materials'
_MAX_BUNDLE = 32 * 1024 * 1024


@dataclass(frozen=True)
class MaterialTemplate:
    digest: str
    program: MaterialProgram
    sweep_color: tuple[int, int, int] | None
    wire_color: tuple[int, int, int] | None = None


def _read_index(recipe_id, rule):
    from .sequence_material_recipes import get_recipe
    recipe = get_recipe(recipe_id)
    if rule not in recipe.rules:
        raise ValueError('当前选项不属于所选材质配方。')
    folder = TEMPLATE_DIRECTORY / recipe.identifier
    with (folder / 'manifest.json').open('rb') as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError('材质规则索引过大。')
    manifest = json.loads(raw)
    if (manifest['schema_version'] != 5 or manifest['method'] != ISLAND_METHOD
            or manifest['recipe_id'] != recipe_id):
        raise ValueError('材质规则版本与配方不匹配，请更新完整配方资源。')
    entry=manifest['entries'][rule.output_name]
    return folder, raw, entry, manifest['atlases'][entry['atlas']]


def default_sweep_color(recipe_id, rule):
    from .sequence_material_colors import SWEEP_COLOR_METHOD, validate_sweep_color
    if rule.layer == 'normal':
        return None
    try:
        _, _, entry, _ = _read_index(recipe_id, rule)
        if entry['sweep_color_method'] != SWEEP_COLOR_METHOD:
            raise ValueError('内置扫过颜色数据缺失或损坏。')
        return validate_sweep_color(entry['sweep_color_rgb'])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('内置扫过颜色数据缺失或损坏。') from error


def default_wire_color(recipe_id, rule):
    from .sequence_material_colors import WIRE_COLOR_METHOD, validate_sweep_color
    if rule.layer in ('normal', 'glow'):
        return None
    try:
        _, _, entry, _ = _read_index(recipe_id, rule)
        if entry.get('wire_color_method') != WIRE_COLOR_METHOD:
            return None
        return validate_sweep_color(entry.get('wire_color_rgb'))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('内置线框颜色数据缺失或损坏。') from error


def load_material_template(recipe_id, rule, *, cancelled=lambda: False):
    from .sequence_material_vtf import check_cancel
    from .sequence_material_colors import SWEEP_COLOR_METHOD, WIRE_COLOR_METHOD, validate_sweep_color
    check_cancel(cancelled)
    try:
        folder, manifest_bytes, entry, atlas_entry = _read_index(recipe_id, rule)
        if atlas_entry['part']!=rule.part:
            raise ValueError('材质规则与 UV 岛索引不匹配。')
        if (entry['source'] != rule.source_name or entry['size'] != rule.size
                or entry['frames'] != rule.frames or entry['format'] != rule.image_format
                or entry['flags'] != rule.flags):
            raise ValueError('材质规则的源图或输出规格与配方不匹配。')
        filename = entry['bundle']
        if Path(filename).name != filename or not filename.endswith('.uvrule'):
            raise ValueError('材质规则资源路径无效。')
        path = folder / filename
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('材质规则资源路径无效。')
        with path.open('rb') as stream:
            blob = stream.read(_MAX_BUNDLE + 1)
        if not 20 <= len(blob) <= _MAX_BUNDLE or len(blob) != entry['bundle_bytes']:
            raise ValueError('材质规则资源长度无效。')
        digest = hashlib.sha256(blob).hexdigest()
        if digest != entry['bundle_sha256']:
            raise ValueError('材质规则资源校验失败。')
        atlas_name=entry['atlas']
        if Path(atlas_name).name!=atlas_name or not atlas_name.endswith('.uvatlas'):
            raise ValueError('材质规则资源路径无效。')
        atlas_path=folder/atlas_name
        if not atlas_path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('材质规则资源路径无效。')
        with atlas_path.open('rb') as stream:
            atlas_blob=stream.read(_MAX_BUNDLE+1)
        if (not 20<=len(atlas_blob)<=_MAX_BUNDLE or len(atlas_blob)!=atlas_entry['bundle_bytes']
                or hashlib.sha256(atlas_blob).hexdigest()!=atlas_entry['bundle_sha256']):
            raise ValueError('材质 UV 岛索引校验失败。')
        program = decode_island_program(blob,atlas_blob,rule,cancelled=cancelled)
        if entry['sweep_color_method'] != SWEEP_COLOR_METHOD:
            raise ValueError('内置扫过颜色数据缺失或损坏。')
        color = validate_sweep_color(entry['sweep_color_rgb'])
        wire_color = validate_sweep_color(entry.get('wire_color_rgb'))
        if rule.layer == 'normal' and (color is not None or wire_color is not None):
            raise ValueError('法线层不支持颜色自定义。')
        if wire_color is not None and (entry.get('wire_color_method') != WIRE_COLOR_METHOD
                                       or rule.layer == 'glow'):
            raise ValueError('内置线框颜色数据缺失或损坏。')
        identity = hashlib.sha256(ISLAND_METHOD.encode() + manifest_bytes + bytes.fromhex(digest)).hexdigest()
        return MaterialTemplate(identity, program, color, wire_color)
    except FileNotFoundError as error:
        raise ValueError('缺少内置材质分析规则，请补齐配方资源；不会回退为统一 UV 扫描。') from error
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('内置材质规则索引或数据损坏。') from error
