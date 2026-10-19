"""Read-only, on-demand original viewmodels for animation comparison.

The game index, parsed models and bounded texture decoder are shared with arms.
Only temporary reader staging is written; original game/addon assets are never
extracted into the conversion session or redistributed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import tempfile

from .action_arms import _CACHE_LOCK, _context, _materials
from .action_preview import PreviewModel
from .action_rules import _rules
from .weapon_catalog import MELEE_IDS, TARGETS_BY_ID, get_weapon
from .weapon_sequences import get_profile


@dataclass(frozen=True)
class OriginalAsset:
    model: PreviewModel
    materials: tuple
    label: str


def with_original_assets(payload, *, cancelled=None):
    """Prepare official animation clocks in the existing preview worker.

    Bars need the real compiled animation even when the split view is hidden.
    A missing installation must not prevent editing the player's own model.
    """
    session=payload[0]
    assets={};profiles={}
    for index,item in enumerate(session.models):
        _check(cancelled)
        key=(item.profile_id,session.options.game_path)
        if key not in profiles:
            try:profiles[key]=load_official_viewmodel(*key,cancelled=cancelled)
            except InterruptedError:raise
            except (OSError,ValueError) as error:profiles[key]=str(error)
        assets[index]=profiles[key]
    _check(cancelled)
    return (*payload[:3],assets)


def _check(cancelled):
    if cancelled and cancelled():
        raise InterruptedError('原版动作对比载入已取消。')


def _weapon(profile_id):
    return get_weapon({'dual_pistol': 'pistol_dual', 'pistol': 'pistol_single'}.get(profile_id, profile_id))


def load_official_viewmodel(profile_id, game_path, cancelled=None):
    """Load the selected profile from original game loose/pak resources only."""
    _check(cancelled)
    profile_id = {'pistol_single': 'pistol', 'pistol_dual': 'dual_pistol'}.get(profile_id, profile_id)
    get_profile(profile_id)
    weapon = _weapon(profile_id)
    key = weapon.view_models[0]
    with _CACHE_LOCK:
        _check(cancelled)
        if not game_path or not Path(game_path).is_dir():
            raise ValueError('请先设置有效的求生之路 2 游戏目录，再载入原版对比。')
        context = _context(game_path)
        resolver = context.resolver
        if key not in context.models:
            stem = key.removesuffix('.mdl'); files = {}; warnings = []
            for extension in ('.mdl', '.vvd', '.dx90.vtx', '.ani'):
                _check(cancelled)
                relative = stem + extension
                content = resolver._read(relative, warnings)
                if content is not None:
                    files[relative] = content
            if key not in files:
                raise ValueError('原版游戏中未找到目标武器 V 模: ' + key)
            if stem+'.vvd' not in files or stem+'.dx90.vtx' not in files:
                raise ValueError('原版 V 模缺少配套 VVD / DX90.VTX: ' + key)
            with tempfile.TemporaryDirectory(prefix='loka_official_viewmodel_') as folder:
                root = Path(folder)
                for relative, content in files.items():
                    _check(cancelled)
                    target = root/relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                model = PreviewModel.load(root/key)
            _check(cancelled)
            for warning in warnings:
                model._warn(warning)
            context.models[key] = model
        model = context.models[key]
        materials = _materials(model, resolver, cancelled)
        # Keep only this asset's decoded references in the game resolver. The
        # global bounded BCN LRU still accelerates switching weapons or arms.
        selected = {id(image) for material in materials
                    for image in (material.texture, material.normal_texture) if image is not None}
        for texture_key in list(resolver._textures):
            if id(resolver._textures[texture_key]) not in selected:
                resolver._textures.pop(texture_key, None)
                resolver._texture_warnings.pop(texture_key, None)
        _check(cancelled)
        return OriginalAsset(model, materials, weapon.name)


def _profile_id(model):
    name = str(getattr(model.data, 'theModelName', '')).replace('\\', '/').casefold()
    name = name.removeprefix('models/')
    for identifier, weapon in TARGETS_BY_ID.items():
        if identifier == 'pistol':
            continue
        if any(name == path.removeprefix('models/').casefold() for path in weapon.view_models):
            return {'pistol_single': 'pistol', 'pistol_dual': 'dual_pistol'}.get(identifier, identifier)
    return None


def matching_sequence(model, activity, official_index=None):
    """Preserve valid identity; extra peers use their first exact Activity.

    ADS/MIXED aliases select the ordinary visible action, never a timer-only
    slot. INSPECT and other unsupported actions have no invented fallback.
    """
    sequences = model.data.theSequenceDescs or []
    if not activity:
        return None
    activity = str(activity).upper()
    if (isinstance(official_index, int) and not isinstance(official_index, bool)
            and 0 <= official_index < len(sequences)
            and sequences[official_index].theActivityName.upper() == activity):
        return official_index
    exact = next((i for i, item in enumerate(sequences)
                  if item.theActivityName.upper() == activity), None)
    if exact is not None:
        return exact

    ordinary = activity.removeprefix('MIXED_')
    ordinary = re.sub(r'^ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_', 'ACT_VM_', ordinary)
    profile_id = _profile_id(model)
    if ordinary == 'ACT_VM_DRAW':
        ordinary = 'ACT_VM_DRAW' if profile_id in MELEE_IDS else 'ACT_VM_DEPLOY'
    elif ordinary in ('ACT_VM_MELEE', 'ACT_VM_MELEE_LAYER') and profile_id in MELEE_IDS:
        ordinary = ordinary.replace('MELEE', 'SECONDARYATTACK')
    if ordinary == activity:
        return None

    roles = {}
    if profile_id is not None:
        profile = get_profile(profile_id)
        roles = {(slot.name.casefold(), slot.activity): _rules()['profiles'][profile_id][slot.index]['role']
                 for slot in profile.slots}
    candidates = [ordinary] if ordinary.endswith('_LAYER') else [ordinary+'_LAYER', ordinary]
    if profile_id == 'hunting_rifle':
        # The original hunting rifle uses a distinct, explicit Activity set;
        # its visible fire is SHOOT_SNIPER_LAYER, not its primary timer slot.
        candidates = [candidate if '_SNIPER' in candidate else
                      candidate.removesuffix('_LAYER')+'_SNIPER_LAYER'
                      if candidate.endswith('_LAYER') else candidate+'_SNIPER'
                      for candidate in candidates]
        candidates = [candidate.replace('ACT_VM_PRIMARYATTACK_SNIPER_LAYER',
                                        'ACT_VM_SHOOT_SNIPER_LAYER') for candidate in candidates]
    for candidate in candidates:
        for index, item in enumerate(sequences):
            if item.theActivityName.upper() != candidate:
                continue
            role = roles.get((item.theName.casefold(), candidate))
            if role is None or role in ('playback', 'playback_timer', 'conditional_playback'):
                return index
    return None


def sample_sequence_frame(model, sequence_index, elapsed, loop=False):
    """Map shared elapsed seconds to this sequence's Source timeline frames.

    Pass ``frame_span=model.timeline(index).frame_count-1`` when sampling the
    returned UI frame. This honors blended Source cycles-per-second rather
    than approximating duration by average FPS. Each model ends independently.
    """
    elapsed = float(elapsed)
    if not math.isfinite(elapsed):
        raise ValueError('Comparison elapsed time must be finite')
    timeline = model.timeline(sequence_index)
    span = max(0, timeline.frame_count-1)
    if not span or timeline.duration <= 0:
        return 0.
    elapsed = max(0., elapsed)
    if loop:
        elapsed %= timeline.duration
    return min(elapsed/timeline.duration, 1.)*span
