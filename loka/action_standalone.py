"""Read-only loose-MDL drafts and transactional correction into a private directory.

The editor's frame counts come from compiled descriptors. Original SMD counts
are deliberately unknown until saving creates a fresh decompile in a stage.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from types import SimpleNamespace

from .action_session import CorrectionModel, CorrectionSession, fingerprint, owned_path
from .action_preview import PreviewModel
from .decompiler import MdlDecompiler
from .fps_values import round_qc_fps
from .vmodel import copy_include_dependencies, game_search_roots, model_relative_path, read_reference_data
from .weapon_compile import SUFFIXES, _model_name, capture_animation_semantics, compile_and_fill, validate_compiled_model


@dataclass(frozen=True)
class StandaloneSaveResult:
    path: Path
    model_path: Path
    qc_path: Path | None
    session: object


class _StandaloneDecompiler(MdlDecompiler):
    def _process_texture_paths(self):
        # Conversion intentionally normalizes material names. Standalone edits
        # retain exact compiled names and their original search directories.
        self.mdl_file_data.theModifiedTexturePaths = list(self.mdl_file_data.theTexturePaths or [])
        self.mdl_file_data.theModifiedTextureFileNames = [t.thePathFileName for t in self.mdl_file_data.theTextures or []]


def infer_profile(model_path):
    """Suggest a catalog profile from an engine path or a compiled internal path."""
    from .weapon_catalog import WEAPONS
    path = Path(model_path)
    candidates = [path.as_posix().lower()]
    if path.is_file():
        try:
            metadata = _read_metadata(path.absolute())
            if metadata:return metadata['profile_id']
        except (OSError, ValueError, KeyError, TypeError):pass
        try:candidates.append('models/'+model_relative_path(read_reference_data(path.read_bytes()).theModelName).lower())
        except (OSError, ValueError):pass
    for candidate in candidates:
        for weapon in WEAPONS:
            for model in weapon.view_models:
                if candidate == model or candidate.endswith('/'+model) or PurePosixPath(candidate).name == PurePosixPath(model).name:
                    if weapon.id == 'pistol':return 'dual_pistol' if 'dual' in model else 'pistol'
                    return weapon.id
    return None


def _companions(model):
    return {owned_path(model.with_name(model.stem+suffix), model.parent)
            for suffix in SUFFIXES if model.with_name(model.stem+suffix).is_file()}


def _pack_root(model):
    models = next((p for p in model.parents if p.name.lower() == 'models'), None)
    return models.parent if models else model.parent


_METADATA_NAME = '.loka_action_correction.json'


def _read_metadata(model):
    from .vpk import safe_archive_path
    root = _pack_root(model)
    metadata_path = root/_METADATA_NAME
    if not metadata_path.is_file():return None
    owned_path(metadata_path, root)
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if not isinstance(metadata, dict) or metadata.get('version') != 1:
        raise ValueError('独立动作矫正元数据格式无效。')
    relative = safe_archive_path(metadata.get('model_relative_path', ''))
    if relative.lower() != model.relative_to(root).as_posix().lower():return None
    if metadata.get('model_sha256') != fingerprint(model):
        raise ValueError('独立动作矫正元数据与 MDL 哈希不匹配，请重新载入正确成品。')
    return metadata


def _material_bindings(data):
    """Canonical ordered engine lookup candidates for every texture-table row."""
    from .vpk import safe_archive_path
    directories = [d.replace('\\', '/').strip('/').lower() for d in data.theTexturePaths or []] or ['']
    bindings = []
    for texture in data.theTextures or []:
        name = texture.thePathFileName.replace('\\', '/').strip('/').lower().removesuffix('.vmt')
        bindings.append(tuple(dict.fromkeys(safe_archive_path('materials/'+
            (directory+'/' if directory else '')+name+'.vmt').lower() for directory in directories)))
    return tuple(bindings)


def _include_facts(model, game_path):
    """Hash resolved dependency entries, avoiding hashes of entire game archives.

    Resolution follows copy_include_dependencies: local models win, followed by
    game loose files and directory VPKs; companions stay with their provider.
    """
    from .vpk import VpkArchive
    source_data = read_reference_data(model.read_bytes())
    local_root = next((p for p in model.parents if p.name.lower() == 'models'), None)
    roots = game_search_roots(game_path)
    archives, facts, seen = {}, {}, set()
    def loose(path):
        if not path.is_file():return None
        owned_path(path, path.parent)
        raw = path.read_bytes()
        facts[('loose', str(path))] = hashlib.sha256(raw).hexdigest()
        return raw, ('loose', path.parent)
    def locate(relative, provider=None):
        if provider:
            kind, root = provider
            if kind == 'loose':return loose(root/PurePosixPath(relative).name)
            key = 'models/'+relative
            raw = archives[root].read(key)
            if raw is not None:facts[('vpk', str(root), key)] = hashlib.sha256(raw).hexdigest()
            return (raw, provider) if raw is not None else None
        candidates = [local_root/relative] if local_root else []
        if local_root is None and PurePosixPath(relative).parent == PurePosixPath(model_relative_path(source_data.theModelName)).parent:
            candidates.append(model.parent/PurePosixPath(relative).name)
            if model.parent.name.lower() == 'source':candidates.append(model.parent.parent/PurePosixPath(relative).name)
        for candidate in candidates:
            found = loose(candidate)
            if found:return found
        for root in roots:
            found = loose(root/'models'/relative)
            if found:return found
            for archive_path in sorted(root.glob('*_dir.vpk')):
                owned_path(archive_path, root)
                if archive_path not in archives:archives[archive_path] = VpkArchive(archive_path)
                raw = archives[archive_path].read('models/'+relative)
                if raw is not None:
                    facts[('vpk', str(archive_path), 'models/'+relative)] = hashlib.sha256(raw).hexdigest()
                    return raw, ('vpk', archive_path)
        return None
    def visit(reference):
        relative = model_relative_path(reference)
        if relative.lower() in seen:return
        seen.add(relative.lower())
        found = locate(relative)
        if found is None:raise ValueError('无法验证 include 依赖: models/'+relative+'；请提供动画库或配置游戏路径。')
        raw, provider = found
        data = read_reference_data(raw)
        if data.fileSize != len(raw):raise ValueError('include 依赖 MDL 长度无效: '+relative)
        for suffix in SUFFIXES[1:]:
            companion = str(PurePosixPath(relative).with_suffix(suffix))
            located = locate(companion, provider)
            if suffix == '.ani' and data.animBlockCount and located is None:
                raise ValueError('include 动画库缺少 ANI: '+relative)
        for group in data.theModelGroups or []:visit(group.theFileName)
    for group in source_data.theModelGroups or []:visit(group.theFileName)
    return facts


def _compiled_actions(data, profile_id):
    from .action_rules import ActionInfo, _official_slot, _playback_role, _timing, activity_name
    from .weapon_sequences import get_profile
    profile = get_profile(profile_id)
    playback, swaps = [], []
    for index, sequence in enumerate(data.theSequenceDescs or []):
        activity = sequence.theActivityName
        if not activity:continue
        refs = sequence.theAnimDescIndexes or []
        animations = data.theAnimationDescs or []
        if not refs or any(type(i) is not int or not 0 <= i < len(animations) for i in refs):
            raise ValueError('编译动作缺少可验证的本地动画引用: '+sequence.theName)
        primary = animations[refs[0]]
        if not math.isfinite(primary.fps) or primary.fps <= 0 or primary.frameCount < 1:
            raise ValueError('编译动作的 FPS 或帧数无效: '+sequence.theName)
        slot = _official_slot(SimpleNamespace(name=sequence.theName), activity, index, profile, None)
        role = _playback_role(activity, profile_id, slot)
        loop = bool(sequence.flags & 1)
        duration, count, rate, note = _timing(activity, profile, slot, loop)
        note += '；compiled metadata：编译主动画帧数，原始 SMD 帧数未知'
        if role and sequence.activityWeight == 0:
            role = 'conditional_playback'
            note += '；Activity 权重为 0，依赖引擎回退或显式调用'
        action = ActionInfo(index, sequence.theName, activity, slot.index if slot else None,
            primary.fps, primary.frameCount, (), loop,
            activity_name(activity, profile_id) or activity, duration, count, rate, note,
            role or 'activity_only')
        swaps.append(action)
        if role:playback.append(action)
    return tuple(playback), tuple(swaps)


class StandaloneCorrectionSession(CorrectionSession):
    standalone = True
    frames_source = 'compiled'

    @classmethod
    def from_model(cls, model_path, profile_id, *, game_path='', compiler_path=''):
        from .weapon_sequences import get_profile
        get_profile(profile_id)
        model = Path(model_path).absolute()
        owned_path(model, model.parent)
        if model.suffix.lower() != '.mdl' or not model.is_file():
            raise ValueError('请选择存在的编译 MDL 文件。')
        session = cls.__new__(cls)
        session.options = SimpleNamespace(input_path=model, target_id=profile_id,
            game_path=str(game_path), compiler_path=str(compiler_path), keep_ads=True,
            ik_rule_handling='default')
        session.input_path = model
        session.output_dir = None
        session.output_path = None
        session.work_dir = None
        session.pack_dir = _pack_root(model)
        session.updates = {}
        session.assignments = {}
        session.timing_overrides = {}
        session._preview_models = {}
        session._original_model = model
        session._inventory = set()
        session.fingerprints = {}
        session.warnings = []
        session._capture()
        metadata = _read_metadata(model)
        if metadata and metadata.get('profile_id') != profile_id:
            raise ValueError('此成品的计时参考属于另一武器配置，请选择 '+str(metadata.get('profile_id'))+'。')
        preview = PreviewModel.load(model)
        if preview.data.theModelGroups:
            preview.warnings.append('当前仅预览和编辑本地 sequence；外部 include 动作保留原样并在保存时验证依赖。')
        actions, swaps = _compiled_actions(preview.data, profile_id)
        if not swaps:
            groups = preview.data.theModelGroups or []
            if groups:
                raise ValueError('此 MDL 仅含外部 include 动作；当前预览不能验证虚拟 sequence 顺位。'
                    '请载入实际动画库 MDL：'+', '.join(g.theFileName for g in groups))
            raise ValueError('此 MDL 没有可编辑且带 Activity 的本地动作。')
        timings = {}
        if metadata:
            from .action_session import with_timing_reference
            for item in metadata.get('timing_indexes', []):
                if (not isinstance(item, list) or len(item) != 2
                        or any(type(value) is not int for value in item) or item[0] in timings):
                    raise ValueError('独立动作矫正计时参考记录无效。')
                timings[item[0]] = item[1]
            if set(timings)-{a.index for a in swaps}:raise ValueError('独立动作矫正计时参考绑定了未知动作。')
            timed = lambda values: tuple(with_timing_reference(a, profile_id, timings[a.index])
                                         if a.index in timings else a for a in values)
            actions, swaps = timed(actions), timed(swaps)
        session.models = (CorrectionModel(profile_id, None, model, actions, swaps,
                                         tuple(sorted(timings.items()))),)
        session._preview_models[0] = preview
        session.check_unchanged()
        return session

    def _tracked_files(self):
        files = _companions(self._original_model) | _companions(self.input_path)
        metadata = _pack_root(self._original_model)/_METADATA_NAME
        if metadata.is_file():files.add(owned_path(metadata, metadata.parent))
        if self.output_dir is not None:
            owned_path(self.output_dir, self.output_dir.parent)
            if not self.output_dir.is_dir():raise ValueError('独立矫正输出目录已丢失。')
            for path in self.output_dir.rglob('*'):
                owned_path(path, self.output_dir)
                if path.is_file():files.add(path)
        return files

    def _source_include_facts(self):
        facts = {'original':_include_facts(self._original_model, self.options.game_path)}
        if self.input_path != self._original_model:
            facts['current'] = _include_facts(self.input_path, self.options.game_path)
        return facts

    def _capture(self):
        super()._capture()
        self._include_state = self._source_include_facts()

    def check_unchanged(self):
        if self._tracked_files() != self._inventory:
            raise ValueError('来源 MDL 或伴随文件清单已变化，请重新载入。')
        for path, digest in self.fingerprints.items():
            owned_path(path, path.parent)
            if not path.is_file() or fingerprint(path) != digest:
                raise ValueError('来源 MDL 或矫正资源已变化，请重新载入: '+str(path))
        if self._source_include_facts() != self._include_state:
            raise ValueError('include 依赖或伴随文件已变化，请重新载入。')

    def load_preview_assets(self, *, cancelled=None, reload_models=False):
        return load_preview_assets(self, cancelled=cancelled, reload_models=reload_models)

    def save(self, *, output_dir=None, cancelled=None, on_log=None):
        return _save(self, output_dir=output_dir, cancelled=cancelled, on_log=on_log)


def load_preview_assets(session, *, cancelled=None, reload_models=False):
    return _load_assets(session, cancelled=cancelled, reload_models=reload_models)


def _load_assets(session, *, cancelled=None, local_resources=None, reload_models=False):
    from .action_assets import resolve_model_material
    from .action_materials import MaterialResolver
    def check():
        if cancelled and cancelled():raise InterruptedError('独立动作矫正预览载入已取消。')
    roots = [session.pack_dir, _pack_root(session._original_model)]
    roots.extend(parent for parent in session._original_model.parents if (parent/'materials').is_dir())
    roots = list(dict.fromkeys(roots))
    class Resolver(MaterialResolver):
        def __init__(self):
            super().__init__(roots[0], session.options.game_path)
            self.local = [MaterialResolver(root) for root in roots]
        def _read(self, key, warnings):
            for resolver in self.local:
                found = resolver._read(key, warnings)
                if found is not None:
                    if local_resources is not None:local_resources[key] = found
                    return found
            return super()._read(key, warnings)
    check();session.check_unchanged()
    if reload_models:session._preview_models.clear()
    resolver = Resolver()
    previews, materials = [], []
    for index,item in enumerate(session.models):
        check()
        preview=session._preview_models.get(index)
        if preview is None:
            preview=PreviewModel.load(item.model_path)
            session._preview_models[index]=preview
        if preview.data.theModelGroups:
            preview.warnings.append('当前仅预览和编辑本地 sequence；外部 include 动作保留原样并在保存时验证依赖。')
        cache, bound = {}, []
        for mesh in preview.meshes:
            check()
            if mesh.material not in cache:
                cache[mesh.material] = resolve_model_material(resolver, mesh.material, preview.data.theTexturePaths or [])
            bound.append(cache[mesh.material])
        if local_resources is not None:
            # Alternate skins/bodygroups can use texture-table entries that do
            # not belong to the default meshes drawn by the preview.
            for texture in preview.data.theTextures or []:
                check()
                name = texture.thePathFileName
                if name not in cache:
                    cache[name] = resolve_model_material(resolver, name, preview.data.theTexturePaths or [])
        previews.append(preview);materials.append(bound)
    check();session.check_unchanged()
    return session, previews, materials


def _destination(session, output_dir):
    if output_dir is None:output_dir = session.output_dir
    if output_dir is None:raise ValueError('首次保存独立模型时请选择新输出目录。')
    output = Path(output_dir).absolute()
    owned_path(output, output.parent)
    if not output.parent.is_dir():raise ValueError('输出目录的父目录不存在。')
    if output == session._original_model.parent or session._original_model.is_relative_to(output):
        raise ValueError('独立矫正不能覆盖来源目录。')
    if session.options.game_path:
        game = Path(session.options.game_path).resolve()
        engine_names = {'update', 'left4dead2', 'left4dead2_dlc1', 'left4dead2_dlc2', 'left4dead2_dlc3', 'hl2'}
        installation = game.parent if game.name.lower() in engine_names or (game/'gameinfo.txt').is_file() else game
        if any(output.is_relative_to(root) for root in [installation, *game_search_roots(str(game))]):
            raise ValueError('独立矫正输出不能位于游戏安装目录或 update/DLC 目录中。')
    if output.exists() and (not output.is_dir() or
            (output != session.output_dir and any(output.iterdir()))):
        raise ValueError('请选择新目录或空目录；不能覆盖已有文件。')
    if session.output_dir is not None and output != session.output_dir and (
            output.is_relative_to(session.output_dir) or session.output_dir.is_relative_to(output)):
        raise ValueError('新输出目录不能与当前矫正目录重叠。')
    return output


def _publish(candidate, output, check):
    """Move a complete verified tree; restore the previous tree on any failure."""
    backup = candidate.parent/'previous'
    existed = output.exists()
    check()
    if existed:os.replace(output, backup)
    try:
        os.replace(candidate, output)
    except BaseException as failure:
        if existed:
            try:os.replace(backup, output)
            except OSError as restore_error:
                raise ValueError('发布失败且原输出目录无法恢复；请保留并手动恢复完整备份：'
                    +str(backup)+'；恢复错误: '+str(restore_error)) from failure
        raise


@contextmanager
def _staging_directory(parent, session, log):
    # Manual lifetime is deliberate: a failed rollback's only old-result copy
    # must never be removed by TemporaryDirectory's context exit or finalizer.
    stage = Path(tempfile.mkdtemp(prefix='.loka_standalone_', dir=parent))
    try:yield stage
    finally:
        def warn(message):
            session.warnings.append(message)
            try:log(message)
            except Exception:pass
        if (stage/'previous').exists() and (stage/'published').exists():
            warn('输出恢复未完成，完整原成品备份保留在 '+str(stage/'previous'))
        else:
            try:
                owned_path(stage, Path(parent))
                shutil.rmtree(stage)
            except (OSError, ValueError) as error:
                warn('暂存目录清理失败，保留在 '+str(stage)+': '+str(error))


def _save(session, *, output_dir=None, cancelled=None, on_log=None):
    log = on_log or (lambda message: None)
    def check():
        if cancelled and cancelled():raise InterruptedError('独立动作矫正保存已取消。')
    check();session.check_unchanged()
    if not session.changes:return None
    output = _destination(session, output_dir)
    compiler = session.options.compiler_path
    if not compiler:
        from .one_click import find_studiomdl
        compiler = find_studiomdl(session.options.game_path) or ''
    if not Path(compiler).is_file():raise ValueError('未找到配置的模型编译器。')
    from .action_rules import _QcDocument, retime_qc
    from .action_swaps import permute_qc
    from .action_session import with_timing_reference
    from .weapon_conversion import _decompile_options
    with _staging_directory(output.parent, session, log) as stage:
        pack = stage/'pack';pack.mkdir()
        sources = stage/'sources'
        log('在独立暂存目录反编译，并应用 Subtract 矫正')
        decompile_options = _decompile_options(session.options)
        # Keep the selected binary's engine identity and material bindings.
        decompile_options.handle_vmodel_includes = False
        dec = _StandaloneDecompiler(decompile_options)
        result = dec.decompile(str(session.input_path), str(sources), progress=log)
        if not result.success or not result.qc_file:
            raise ValueError('独立模型反编译失败: '+'; '.join(result.errors))
        check()
        if dec.mdl_file_data.theModelGroups:
            copy_include_dependencies(str(session.input_path), dec.mdl_file_data,
                                      str(sources), session.options.game_path)
        qc = Path(result.qc_file)
        document = _QcDocument(qc)
        original = session._preview_models.get(0) or PreviewModel.load(session.input_path)
        if [s.name for s in document.sequences] != [s.theName for s in original.data.theSequenceDescs or []]:
            raise ValueError('反编译 sequence 顺位与所载入 MDL 不符，不能安全应用草稿。')
        origins = capture_animation_semantics(dec.mdl_file_data, validate_ik_errors=False)
        before_origins = deepcopy(origins)
        assignments = {dest:donor for (i,dest),donor in session.assignments.items() if i == 0}
        for destination, donor in assignments.items():
            origins[document.sequences[destination].name] = deepcopy(before_origins[document.sequences[donor].name])
        qc.write_text(permute_qc(qc, assignments, session.models[0].profile_id), encoding='utf-8', newline='\n')
        updates = {change.action.index:change.new_fps for change in session.changes}
        qc.write_text(round_qc_fps(retime_qc(qc, updates)), encoding='utf-8', newline='\n')
        log('编译并验证动作来源、事件、材质及伴随文件')
        compile_and_fill(pack, [qc], compiler, game_path=session.options.game_path,
            semantic_origins={str(qc.resolve()):origins}, cancelled=cancelled, on_log=log)
        model = pack/_model_name(qc)
        data, _ = validate_compiled_model(model, qc, semantic_origins=origins)
        # QC validation alone cannot detect an exporter dropping source facts.
        # Check published payloads against the originally loaded descriptors too.
        for index, after in enumerate(data.theSequenceDescs or []):
            identity = original.data.theSequenceDescs[index]
            donor = original.data.theSequenceDescs[assignments.get(index, index)]
            if after.theName != identity.theName or after.theActivityName != identity.theActivityName:
                raise ValueError('编译动作身份与原始 MDL 槽位不符: '+identity.theName)
            before_events, after_events = donor.theEvents or [], after.theEvents or []
            if len(before_events) != len(after_events) or bool(donor.flags & 1) != bool(after.flags & 1):
                raise ValueError('编译事件或循环语义与原始动画来源不符: '+identity.theName)
            for before, current in zip(before_events, after_events):
                if (before.theName != current.theName or before.options.rstrip('\0') != current.options.rstrip('\0')
                        or not math.isclose(before.cycle, current.cycle, abs_tol=1e-5, rel_tol=1e-5)):
                    raise ValueError('编译事件内容或周期与原始动画来源不符: '+identity.theName)
        if _material_bindings(data) != _material_bindings(original.data):
            raise ValueError('编译材质绑定或查找优先级改变，拒绝发布。')
        if data.theSkinFamilies != original.data.theSkinFamilies:
            raise ValueError('编译皮肤材质绑定改变，拒绝发布。')
        original_groups = [g.theFileName.replace('\\', '/').lower() for g in original.data.theModelGroups or []]
        if [g.theFileName.replace('\\', '/').lower() for g in data.theModelGroups or []] != original_groups:
            raise ValueError('编译 include 依赖改变，拒绝发布。')
        actions, swaps = _compiled_actions(data, session.models[0].profile_id)
        timings = {a.index:session.timing_index(0, a.index) for a in swaps if session.timing_index(0, a.index) is not None}
        timed = lambda values: tuple(with_timing_reference(a, session.models[0].profile_id, timings[a.index])
                                    if a.index in timings else a for a in values)
        actions, swaps = timed(actions), timed(swaps)
        published = stage/'published';published.mkdir()
        # The fresh pack contains verified primary and include binaries. Publish
        # only each model's compiled companions; editable sources stay private.
        for compiled in (pack/'models').rglob('*.mdl'):
            for path in _companions(compiled):
                destination = owned_path(published/path.relative_to(pack), published)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        relative_model = model.relative_to(pack)
        next_model = output/relative_model
        next_models = (CorrectionModel(session.models[0].profile_id, None, next_model,
                                      actions, swaps, tuple(sorted(timings.items()))),)
        # All decoding and hashing completes before the directory swap.
        PreviewModel.load(published/relative_model)
        next_fingerprints = {output/p.relative_to(published):fingerprint(p)
                             for p in published.rglob('*') if p.is_file()}
        for path in _companions(session._original_model):next_fingerprints[path] = fingerprint(path)
        original_metadata = _pack_root(session._original_model)/_METADATA_NAME
        if original_metadata.is_file():next_fingerprints[original_metadata] = session.fingerprints[original_metadata]
        candidate_includes = _include_facts(published/relative_model, session.options.game_path)
        next_include_state = {'original':session._include_state['original'], 'current':{}}
        for key, value in candidate_includes.items():
            kind, location, *entry = key
            location_path = Path(location)
            if location_path.is_relative_to(published):location = str(output/location_path.relative_to(published))
            next_include_state['current'][(kind, location, *entry)] = value
        check();session.check_unchanged()
        _publish(published, output, lambda: (check(), session.check_unchanged(), _destination(session, output)))
        session.models = next_models
        session.input_path = next_model
        session.options = SimpleNamespace(**{**vars(session.options), 'input_path':next_model})
        session.output_dir = session.output_path = session.pack_dir = output
        session.fingerprints = next_fingerprints
        session._inventory = set(next_fingerprints)
        session._include_state = next_include_state
        session.discard();session._preview_models.clear()
        return StandaloneSaveResult(output, next_model, None, session)
