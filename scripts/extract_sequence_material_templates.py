"""Extract built-in UV-local material effect data from original VTF references.

This prepares source recipe resources. It never generates replacement VTFs,
changes the reference directory, runs tests, or invokes the exe build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageChops

from loka.action_materials import _decode_vtf_uncached
from loka.sequence_material_colors import SWEEP_COLOR_METHOD, WIRE_COLOR_METHOD, SweepColorAccumulator
from loka.sequence_material_phases import PHASE_METHOD, extract_phase_maps
from loka.sequence_material_recipes import get_recipe
from loka.sequence_material_templates import (REFERENCE_ENCODING, TEMPLATE_DIRECTORY,
                                             TEMPLATE_MAGIC, TEMPLATE_METHOD,
                                             transform_reference_frames)
from loka.sequence_material_vtf import MAX_SOURCE_BYTES, iter_product_frames, resize_texture


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('Reference exceeds source read limit: ' + str(path))
    return raw


def extract_templates(reference_dir, output_dir, recipe_id):
    recipe = get_recipe(recipe_id)
    reference_dir = Path(reference_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if output_dir == reference_dir or output_dir.is_relative_to(reference_dir):
        raise ValueError('Template output must be outside the original reference directory')
    # Refuse incomplete input rather than guessing a similarly named texture.
    for rule in recipe.rules:
        for name in (rule.source_name, rule.output_name):
            if not (reference_dir / name).is_file():
                raise FileNotFoundError('Missing exact reference mapping: ' + name)
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_index = output_dir / 'manifest.json'
    previous_files = set()
    if previous_index.is_file():
        previous = json.loads(previous_index.read_text(encoding='utf-8'))
        previous_files = {entry['bundle'] for entry in previous['entries'].values()}
    entries = {}
    bundles = {}
    total_reference_bytes = 0
    with tempfile.TemporaryDirectory(prefix='.loka-template-extract-', dir=output_dir) as staging_name:
        staging = Path(staging_name).resolve()
        if staging.parent != output_dir:
            raise ValueError('Template staging directory escapes its output folder')
        for number, rule in enumerate(recipe.rules, 1):
            source_bytes = _read(reference_dir / rule.source_name)
            reference_bytes = _read(reference_dir / rule.output_name)
            source = _decode_vtf_uncached(source_bytes)
            original = Image.frombytes('RGBA', (source.width, source.height), source.rgba)
            baseline_image = resize_texture(original, (rule.size, rule.size), normal=rule.layer == 'normal')
            if rule.layer == 'glow':
                baseline_image.putalpha(255)
            baseline = baseline_image.tobytes()
            peak_rgb = Image.new('L', (rule.size, rule.size))
            peak_alpha = Image.new('L', (rule.size, rule.size))
            first_alpha = last_alpha = None
            color_samples = SweepColorAccumulator(rule)
            wire_samples = SweepColorAccumulator(rule, wire=True)
            paired_name = None
            paired_frames = None
            if rule.layer == 'print':
                paired_name = (rule.output_name[:-8] + '.vtf' if rule.output_name.endswith('_red.vtf')
                               else rule.output_name[:-4] + '_red.vtf')
                if not any(candidate.output_name == paired_name for candidate in recipe.rules):
                    raise ValueError('Missing explicit wire reference pair: ' + rule.output_name)
                paired_frames = iter(iter_product_frames(_read(reference_dir / paired_name), rule))
            reference_frames = tuple(decoded.rgba for decoded in iter_product_frames(reference_bytes, rule))
            phase_frames = extract_phase_maps(reference_frames, rule)
            phase_data = b''.join(phase_frames)
            for index, rgba in enumerate(reference_frames):
                color_samples.add(rgba, phase_frames[index])
                paired_frame = next(paired_frames).rgba if paired_frames is not None else None
                wire_samples.add(rgba, phase_frames[index], paired_frame=paired_frame)
                frame = Image.frombytes('RGBA', (rule.size, rule.size), rgba)
                r, g, b, alpha = frame.split()
                light = ImageChops.lighter(ImageChops.lighter(r, g), b)
                peak_rgb = ImageChops.lighter(peak_rgb, light)
                peak_alpha = ImageChops.lighter(peak_alpha, alpha)
                if index == 0:
                    first_alpha = alpha.getextrema()
                last_alpha = alpha.getextrema()
            packed_reference = transform_reference_frames(reference_bytes, rule)
            payload = baseline + peak_rgb.tobytes() + peak_alpha.tobytes() + phase_data + packed_reference
            blob = struct.pack('<8sII', TEMPLATE_MAGIC, len(baseline), len(reference_bytes)) + zlib.compress(payload, 9)
            bundle_hash = hashlib.sha256(blob).hexdigest()
            filename = bundle_hash + '.stpl'
            if filename not in bundles:
                (staging / filename).write_bytes(blob)
                bundles[filename] = len(blob)
            entries[rule.output_name] = {
                'source': rule.source_name,
                'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
                'source_dimensions': [source.width, source.height],
                'part': rule.part, 'layer': rule.layer, 'variant': rule.variant,
                'size': rule.size, 'frames': rule.frames,
                'format': rule.image_format, 'flags': rule.flags,
                'bundle': filename, 'bundle_sha256': bundle_hash,
                'bundle_bytes': len(blob),
                'baseline_sha256': hashlib.sha256(baseline).hexdigest(),
                'reference_sha256': hashlib.sha256(reference_bytes).hexdigest(),
                'reference_bytes': len(reference_bytes),
                'first_alpha_range': first_alpha, 'last_alpha_range': last_alpha,
                'sweep_color_rgb': color_samples.color(),
                'sweep_color_method': SWEEP_COLOR_METHOD,
                'wire_color_rgb': wire_samples.color(),
                'wire_color_method': WIRE_COLOR_METHOD,
                'wire_reference_pair': paired_name,
                'phase_sha256': hashlib.sha256(phase_data).hexdigest(),
                'phase_counts': dict(zip(('wire', 'white_model', 'print_head', 'material'),
                                         (phase_data.count(bytes([value])) for value in range(4)))),
            }
            total_reference_bytes += len(reference_bytes)
            print(f'Extracted {number}/{len(recipe.rules)}: {rule.output_name}', flush=True)
            del reference_frames, phase_frames, phase_data
        manifest = {
            'schema_version': 3, 'method': TEMPLATE_METHOD,
            'phase_method': PHASE_METHOD,
            'reference_encoding': REFERENCE_ENCODING,
            'recipe_id': recipe.identifier, 'recipe_name': recipe.name,
            'reference_root': 'materials/models/sjz/kc17zwjy/jc',
            'baseline_resampling': 'linear-light RGB box; normalized normal box; independent alpha',
            'applicability': 'Original UV layout only; no UV remapping or SMD inference.',
            'entries': entries,
            'resource_summary': {
                'output_mappings': len(entries), 'unique_bundles': len(bundles),
                'bundle_bytes': sum(bundles.values()),
                'reference_vtf_bytes': total_reference_bytes,
            },
        }
        manifest_path = staging / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        # Content-addressed bundles are immutable; publish the index last.
        for filename in bundles:
            destination = output_dir / filename
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != filename[:-5]:
                    raise ValueError('Existing content-addressed resource is damaged: ' + filename)
            else:
                os.replace(staging / filename, destination)
        os.replace(manifest_path, output_dir / 'manifest.json')
        for filename in previous_files - set(bundles):
            old = output_dir / filename
            if (len(filename) == 69 and filename.endswith('.stpl')
                    and all(c in '0123456789abcdef' for c in filename[:-5])
                    and old.resolve().parent == output_dir and not old.is_symlink()
                    and old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest() == filename[:-5]):
                old.unlink()
    print(json.dumps(manifest['resource_summary'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', required=True, type=Path)
    parser.add_argument('--recipe', default='kc14-jiuci')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    extract_templates(args.reference_dir, args.output_dir or TEMPLATE_DIRECTORY / args.recipe, args.recipe)
