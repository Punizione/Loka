"""Analyze standard VTFs into UV rules. No reference images ship at runtime.

Requires Pillow and NumPy in the authoring environment only. --legacy-dir can
migrate existing v3 analysis without access to the original reference folder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from loka.action_materials import _decode_vtf_uncached
from loka.sequence_material_colors import SWEEP_COLOR_METHOD, WIRE_COLOR_METHOD, SweepColorAccumulator
from loka.sequence_material_phases import PHASE_METHOD, extract_phase_maps
from loka.sequence_material_recipes import get_recipe
from loka.sequence_material_rules import GAIN_STEPS, LIGHT_STEPS
from loka.sequence_material_islands import ISLAND_METHOD
from loka.sequence_material_templates import TEMPLATE_DIRECTORY
from loka.sequence_material_vtf import MAX_SOURCE_BYTES, iter_product_frames, resize_texture
from sequence_material_rule_extract import analyze_parameters
from sequence_material_uv import read_smd_uv, build_island_atlas
from sequence_material_island_extract import encode_atlas, pool_island_parameters


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Reference exceeds source read limit: ' + str(path))
    return raw


def _standard_input(reference_dir, rule, recipe):
    source_bytes = _read(reference_dir / rule.source_name)
    reference = _read(reference_dir / rule.output_name)
    source = _decode_vtf_uncached(source_bytes)
    original = Image.frombytes('RGBA', (source.width, source.height), source.rgba)
    baseline_image = resize_texture(original, (rule.size, rule.size), normal=rule.layer == 'normal')
    if rule.layer == 'glow':
        baseline_image.putalpha(255)
    baseline = baseline_image.tobytes()
    frames = tuple(frame.rgba for frame in iter_product_frames(reference, rule))
    phases = extract_phase_maps(frames, rule)
    sweep, wire = SweepColorAccumulator(rule), SweepColorAccumulator(rule, wire=True)
    paired_name = None
    paired_frames = None
    if rule.layer == 'print':
        paired_name = (rule.output_name[:-8] + '.vtf' if rule.output_name.endswith('_red.vtf')
                       else rule.output_name[:-4] + '_red.vtf')
        if not any(candidate.output_name == paired_name for candidate in recipe.rules):
            raise ValueError('Missing explicit wire reference pair: ' + rule.output_name)
        paired_frames = iter(iter_product_frames(_read(reference_dir / paired_name), rule))
    for index, rgba in enumerate(frames):
        sweep.add(rgba, phases[index])
        wire.add(rgba, phases[index], paired_frame=next(paired_frames).rgba if paired_frames else None)
    metadata = {'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
                'source_dimensions':[source.width,source.height],
                'reference_sha256':hashlib.sha256(reference).hexdigest(),
                'sweep_color_rgb':sweep.color(), 'sweep_color_method':SWEEP_COLOR_METHOD,
                'wire_color_rgb':wire.color(), 'wire_color_method':WIRE_COLOR_METHOD,
                'wire_reference_pair':paired_name}
    return baseline, frames, phases, metadata


def _legacy_input(folder, rule):
    from sequence_material_legacy import read_legacy_template
    baseline, reference, phases, metadata = read_legacy_template(folder, rule)
    frames = tuple(frame.rgba for frame in iter_product_frames(reference, rule))
    return baseline, frames, phases, metadata


def extract_rules(output_dir, recipe_id, *, smd_dir, reference_dir=None, legacy_dir=None):
    recipe = get_recipe(recipe_id)
    inputs = Path(reference_dir or legacy_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    smd_dir = Path(smd_dir).resolve(strict=True)
    if output_dir == inputs or output_dir.is_relative_to(inputs):
        raise ValueError('Rules output must be outside the original authoring folder')
    if reference_dir is not None:
        for rule in recipe.rules:
            for name in (rule.source_name, rule.output_name):
                if not (inputs / name).is_file():
                    raise FileNotFoundError('Missing exact reference mapping: ' + name)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_index = output_dir / 'manifest.json'
    previous_files = set()
    if previous_index.is_file():
        previous = json.loads(previous_index.read_bytes())
        previous_files = {entry['bundle'] for entry in previous['entries'].values()} | set(previous.get('atlases',{}))
    entries, bundles = {}, {}
    atlases,geometry = {},{}
    with tempfile.TemporaryDirectory(prefix='.loka-rule-extract-', dir=output_dir) as staging_name:
        staging = Path(staging_name).resolve()
        if staging.parent != output_dir:
            raise ValueError('Rule staging directory escapes its output folder')
        for part in dict.fromkeys(rule.part for rule in recipe.rules):
            basis=next(rule for rule in recipe.rules if rule.part==part
                       and rule.layer in ('print','soliddepth') and not rule.output_name.endswith('_red.vtf'))
            size=max(rule.size for rule in recipe.rules if rule.part==part)
            if size!=basis.size:raise ValueError('UV basis must use the largest authored part resolution')
            print('Analyzing SMD UV islands: '+part,flush=True)
            triangles,islands,source=read_smd_uv(smd_dir,part)
            baseline,frames,phases,authoring=(_standard_input(inputs,basis,recipe)
                                            if reference_dir is not None else _legacy_input(inputs,basis))
            atlas,statistics,training=build_island_atlas(triangles,islands,size,phases,basis.frames)
            blob=encode_atlas(atlas,statistics['regions'])
            digest=hashlib.sha256(blob).hexdigest()
            filename=digest+'.uvatlas'
            (staging/filename).write_bytes(blob)
            atlases[filename]={'part':part,'bundle_sha256':digest,'bundle_bytes':len(blob),
                               'smd':source,'scan':statistics,
                               'timing_reference':basis.output_name,
                               'timing_reference_sha256':authoring['reference_sha256']}
            geometry[part]=(atlas,statistics['regions'],digest,filename,training)
            print(f'UV atlas {part}: {statistics["source_islands"]} islands, {statistics["regions"]} cells, {len(blob):,} bytes',flush=True)
            del baseline,frames,phases,triangles,islands,blob
        for number, rule in enumerate(recipe.rules, 1):
            print(f'Analyzing {number}/{len(recipe.rules)}: {rule.output_name}', flush=True)
            baseline, frames, phases, authoring = (_standard_input(inputs, rule, recipe)
                                                  if reference_dir is not None else _legacy_input(inputs, rule))
            palette, fields = analyze_parameters(baseline, frames, phases, rule, authoring.get('wire_color_rgb'))
            atlas,regions,atlas_digest,atlas_name,training=geometry[rule.part]
            blob, statistics = pool_island_parameters(palette,fields,atlas,regions,rule,atlas_digest,training)
            bundle_hash = hashlib.sha256(blob).hexdigest()
            filename = bundle_hash + '.uvrule'
            if filename not in bundles:
                (staging / filename).write_bytes(blob)
                bundles[filename] = len(blob)
            entries[rule.output_name] = {
                'source':rule.source_name,
                'source_sha256':authoring['source_sha256'],
                'source_dimensions':authoring['source_dimensions'],
                'reference_sha256':authoring['reference_sha256'],
                'part':rule.part, 'layer':rule.layer, 'variant':rule.variant,
                'size':rule.size, 'frames':rule.frames, 'format':rule.image_format, 'flags':rule.flags,
                'bundle':filename, 'bundle_sha256':bundle_hash, 'bundle_bytes':len(blob),
                'atlas':atlas_name,
                'sweep_color_rgb':authoring['sweep_color_rgb'], 'sweep_color_method':SWEEP_COLOR_METHOD,
                'wire_color_rgb':authoring.get('wire_color_rgb'), 'wire_color_method':WIRE_COLOR_METHOD,
                'wire_reference_pair':authoring.get('wire_reference_pair'),
                'regional_parameters':statistics,
            }
            print(f'Encoded {number}/{len(recipe.rules)}: {len(blob):,} rule bytes', flush=True)
            del baseline, frames, phases, palette, fields, blob
        manifest = {
            'schema_version':5, 'method':ISLAND_METHOD, 'phase_method':PHASE_METHOD,
            'recipe_id':recipe.identifier, 'recipe_name':recipe.name,
            'applicability':'Same UV layout as the matching standard; no UV or model inference at runtime.',
            'representation':'Shared SMD UV-island scan cells and regional numeric operators; compact local indices retain exact mode/alpha histories.',
            'precision':{'signed_gain_steps':GAIN_STEPS, 'light_steps':LIGHT_STEPS,
                         'hue_component_steps':31,
                         'shared_material_fit_rgb_error':6,
                         'exact_categorical_histories':['mode','alpha'],
                         'regional_categorical_fields':['effect_hue'],
                         'surface_training':'SMD-covered samples only; nearest-profile synthesis padding',
                         'exact_analog_anchors':True},
            'entries':entries,
            'atlases':atlases,
            'resource_summary':{'output_mappings':len(entries), 'unique_bundles':len(bundles),
                                'bundle_bytes':sum(bundles.values()),
                                'shared_atlases':len(atlases),
                                'atlas_bytes':sum(entry['bundle_bytes'] for entry in atlases.values()),
                                'embedded_reference_vtf_bytes':0, 'embedded_static_baseline_bytes':0},
        }
        manifest_path = staging/'manifest.json'
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for filename in set(bundles)|set(atlases):
            destination = output_dir/filename
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != filename.split('.')[0]:
                    raise ValueError('Existing content-addressed rule is damaged: ' + filename)
            else:
                os.replace(staging/filename,destination)
        os.replace(manifest_path,output_dir/'manifest.json')
        for filename in previous_files-set(bundles)-set(atlases):
            old = output_dir/filename
            stem, suffix = Path(filename).stem, Path(filename).suffix
            if (len(stem)==64 and all(c in '0123456789abcdef' for c in stem)
                    and suffix in ('.stpl','.uvrule','.uvatlas') and old.resolve().parent==output_dir
                    and not old.is_symlink() and old.is_file()
                    and hashlib.sha256(old.read_bytes()).hexdigest()==stem):
                old.unlink()
    print(json.dumps(manifest['resource_summary']),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--reference-dir',type=Path)
    inputs.add_argument('--legacy-dir',type=Path)
    parser.add_argument('--smd-dir',type=Path,required=True,help='Authoring-only directory containing the fixed recipe SMD meshes')
    parser.add_argument('--recipe',default='kc14-jiuci')
    parser.add_argument('--output-dir',type=Path)
    args = parser.parse_args()
    extract_rules(args.output_dir or TEMPLATE_DIRECTORY/args.recipe,args.recipe,smd_dir=args.smd_dir,
                  reference_dir=args.reference_dir,legacy_dir=args.legacy_dir)
