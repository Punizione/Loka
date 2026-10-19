"""Original-installation/custom arms, isolated staging and Source bone merging.

No Valve assets are distributed. Original files are read from the selected game,
staged only for the compiled-model reader, then removed. Cached parsed models and
materials are process-local and invalidated when observed game resources change.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import re
import tempfile
import threading
from types import MappingProxyType

from .action_assets import resolve_model_material
from .action_materials import MaterialResolver, _case_path, _game_roots
from .action_pose import matrix, multiply, hierarchy_order
from .action_preview import PreviewModel, PreviewPose


@dataclass(frozen=True)
class ArmsPreset:
    label: str
    model_path: str


OFFICIAL_ARMS = MappingProxyType({
    key: ArmsPreset(label, 'models/weapons/arms/v_arms_' + name + '.mdl')
    for key, label, name in (
        ('nick', 'Nick', 'gambler_new'), ('bill', 'Bill', 'bill'),
        ('coach', 'Coach', 'coach_new'), ('francis', 'Francis', 'francis'),
        ('louis', 'Louis', 'louis'), ('ellis', 'Ellis', 'mechanic_new'),
        ('rochelle', 'Rochelle', 'producer_new'), ('zoey', 'Zoey', 'zoey'))
})


@dataclass(frozen=True)
class ArmsAsset:
    model: PreviewModel
    materials: tuple
    label: str


def _check(cancelled):
    if cancelled and cancelled():
        raise InterruptedError('手臂预览载入已取消。')


def _stat(path):
    try:
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ino
    except FileNotFoundError:
        return None


def _game_signature(game_path):
    result = []
    for root in _game_roots(game_path):
        result.append((str(root), _stat(root)))
        if root.is_dir():
            result.extend((str(path), _stat(path)) for path in sorted(root.iterdir())
                          if re.fullmatch(r'pak\d+(?:_dir|_\d+)?\.vpk', path.name, re.I))
    return tuple(result)


class _OriginalResolver(MaterialResolver):
    def __init__(self, empty_root, game_path):
        super().__init__(empty_root, game_path)
        self.observed_loose = {}

    def _loose_signature(self, key):
        return tuple((str(root), str(path) if path else '', _stat(path) if path else None)
                     for root in self.game_roots
                     for path in [_case_path(root, key, exclude_addons=True)])

    def _read(self, key, warnings):
        raw = super()._read(key, warnings)
        self.observed_loose[key] = self._loose_signature(key)
        return raw

    def unchanged(self):
        return all(self._loose_signature(key) == value for key, value in self.observed_loose.items())


class _GameContext:
    def __init__(self, game_path):
        self.signature = _game_signature(game_path)
        # This empty root is intentionally outside the game and the mod pack.
        self.directory = tempfile.TemporaryDirectory(prefix='loka_arms_cache_')
        self.resolver = _OriginalResolver(self.directory.name, game_path)
        self.models = {}

    def close(self):
        self.directory.cleanup()


_CONTEXTS = OrderedDict()
_CACHE_LOCK = threading.RLock()


def clear_arms_cache():
    """Release original indexes, parsed arms and per-resolver texture references."""
    with _CACHE_LOCK:
        for context in _CONTEXTS.values():
            context.close()
        _CONTEXTS.clear()


def _context(game_path):
    if not game_path or not Path(game_path).is_dir():
        raise ValueError('请先设置有效的求生之路 2 游戏目录，再选择官方手臂。')
    key = str(Path(game_path).resolve())
    context = _CONTEXTS.pop(key, None)
    if context is not None and (context.signature != _game_signature(game_path) or not context.resolver.unchanged()):
        context.close(); context = None
    if context is None:
        context = _GameContext(game_path)
    _CONTEXTS[key] = context
    while len(_CONTEXTS) > 2:
        _, old = _CONTEXTS.popitem(last=False)
        old.close()
    return context


class _CustomResolver(MaterialResolver):
    def __init__(self, model_path, game_path):
        self.local_roots = tuple(parent for parent in (model_path.parent, *model_path.parent.parents)
                                 if parent != parent.parent and (parent / 'materials').is_dir())
        super().__init__(self.local_roots[0] if self.local_roots else model_path.parent, game_path)

    def _read(self, key, warnings):
        # Keep the same resource size bounds and symlink confinement as the
        # standard resolver, while trying each custom ancestor before the game.
        for root in self.local_roots:
            path = _case_path(root, key)
            if path is not None:
                limit = 2 * 1024 * 1024 if key.endswith('.vmt') else 128 * 1024 * 1024
                if path.stat().st_size > limit:
                    raise ValueError('手臂材质资源超出预览大小限制: ' + key)
                return path.read_bytes()
        return super()._read(key, warnings)


def _materials(model, resolver, cancelled):
    bound = []; cache = {}
    for mesh in model.meshes:
        _check(cancelled)
        if mesh.material not in cache:
            cache[mesh.material] = resolve_model_material(resolver, mesh.material, model.data.theTexturePaths or [])
        bound.append(cache[mesh.material])
    return tuple(bound)


def load_arms(source, game_path='', *, cancelled=None):
    """Load one OFFICIAL_ARMS ID or custom MDL; never mutate source content."""
    _check(cancelled)
    preset = OFFICIAL_ARMS.get(str(source).casefold())
    if preset is None:
        path = Path(source).resolve(strict=True)
        if path.suffix.lower() != '.mdl':
            raise ValueError('自选手臂需要已编译的 .mdl 文件及配套 VVD/VTX。')
        model = PreviewModel.load(path)
        _check(cancelled)
        resolver = _CustomResolver(path, game_path)
        materials = _materials(model, resolver, cancelled)
        _check(cancelled)
        return ArmsAsset(model, materials, path.stem)

    with _CACHE_LOCK:
        _check(cancelled)
        context = _context(game_path)
        resolver = context.resolver
        key = preset.model_path
        if key not in context.models:
            stem = key.removesuffix('.mdl'); files = {}; warnings = []
            for extension in ('.mdl', '.vvd', '.dx90.vtx', '.ani'):
                _check(cancelled)
                raw = resolver._read(stem + extension, warnings)
                if raw is not None:
                    files[stem + extension] = raw
            if key not in files:
                raise ValueError('原版游戏中未找到官方手臂: ' + key)
            if stem + '.vvd' not in files or stem + '.dx90.vtx' not in files:
                raise ValueError('官方手臂缺少配套 VVD / DX90.VTX: ' + key)
            with tempfile.TemporaryDirectory(prefix='loka_arms_model_') as folder:
                root = Path(folder)
                for relative, raw in files.items():
                    _check(cancelled)
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                model = PreviewModel.load(root / key)
            _check(cancelled)
            for warning in warnings:
                model._warn(warning)
            context.models[key] = model
        model = context.models[key]
        materials = _materials(model, resolver, cancelled)
        # A game context retains only the selected arms' decoded textures. The
        # decoder's bounded shared LRU handles switching back to other presets.
        selected = {id(image) for item in materials for image in (item.texture, item.normal_texture)
                    if image is not None}
        for texture_key in list(resolver._textures):
            if id(resolver._textures[texture_key]) not in selected:
                resolver._textures.pop(texture_key, None)
                resolver._texture_warnings.pop(texture_key, None)
        _check(cancelled)
        return ArmsAsset(model, materials, preset.label)


class CompositePreviewModel:
    """Primary weapon + named-bone-merged arms in a combined GPU palette."""
    def __init__(self, weapon, arms):
        self.weapon = weapon
        self.arms = arms.model if isinstance(arms, ArmsAsset) else arms
        self._bone_offset = len(weapon.data.theBones or [])
        self.meshes = tuple(weapon.meshes) + tuple(replace(mesh,
            bone_indices=tuple(tuple(index + self._bone_offset for index in group)
                               for group in mesh.bone_indices)) for mesh in self.arms.meshes)
        names = {}
        for i, bone in enumerate(weapon.data.theBones or []):
            key = bone.theName.casefold()
            if key in names and key:
                raise ValueError('武器存在重名骨骼，无法可靠合并手臂: ' + bone.theName)
            if key:
                names[key] = i
        arm_names = [b.theName.casefold() for b in self.arms.data.theBones or []]
        nonempty = [name for name in arm_names if name]
        if len(nonempty) != len(set(nonempty)):
            raise ValueError('手臂存在重名骨骼，无法可靠合并。')
        self._matches = tuple(names.get(name) for name in arm_names)
        self._merge_warnings = []
        if not any(index is not None for index in self._matches):
            self._merge_warnings.append('手臂与武器没有匹配骨骼；手臂当前使用原始绑定姿势。')

    def __getattr__(self, name):
        return getattr(self.weapon, name)

    @property
    def warnings(self):
        return list(dict.fromkeys([*self.weapon.warnings, *self.arms.warnings, *self._merge_warnings]))

    def sample(self, sequence_index, frame, *, frame_span=None):
        primary = self.weapon.sample(sequence_index, frame, frame_span=frame_span)
        if len(primary.bone_world_matrices) != self._bone_offset:
            raise ValueError('武器骨骼世界矩阵不完整，无法合并手臂。')
        bones = self.arms.data.theBones or []
        world=[None]*len(bones)
        for index in hierarchy_order(bones):
            # Validate every parent even when this bone directly matches.
            parent = bones[index].parentBoneIndex
            parent_world = world[parent] if parent >= 0 else None
            match = self._matches[index]
            if match is not None:
                value = primary.bone_world_matrices[match]
            else:
                value = matrix(*self.arms._evaluator.bind[index])
                if parent_world is not None:
                    value = multiply(parent_world, value)
            world[index]=value
        arm_world=tuple(world)
        skin = tuple(multiply(value, inverse) for value, inverse in zip(arm_world, self.arms._inverse_bind))
        if not all(math.isfinite(value) for values in (*arm_world, *skin) for value in values):
            raise ValueError('手臂骨骼姿态包含非有限数值。')
        return PreviewPose(primary.bone_matrices + skin, primary.attachments,
                           primary.bone_world_matrices + arm_world)
