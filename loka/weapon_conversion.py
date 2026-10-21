"""Phase-one weapon conversion: read-only inspection, extraction and decompilation."""
from dataclasses import dataclass, field, asdict,replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from copy import deepcopy
import secrets
import shutil
import json
import hashlib

from .decompiler import DecompileOptions, MdlDecompiler
from .vmodel import copy_include_dependencies, read_reference_data
from .vpk import VpkArchive, safe_archive_path
from .weapon_catalog import WEAPONS, Weapon, get_weapon, validate_target, target_profiles_for, target_family_id
from .weapon_sequences import SourceAnimation,SourceSequence, plan_sequences, validate_sequence_layout
from .weapon_qc import rewrite_viewmodel_qc, rewrite_worldmodel_qc, sequence_dependencies, _tokens
from .qc_preflight import prepare_conversion_qcs
from .weapon_audio import plan_audio_conversion, apply_audio_conversion
from .weapon_l4n import plan_l4n_conversion,build_l4n_pack
from .weapon_audio_metadata import load_target_metadata
from .weapon_materials import plan_material_conversion,apply_material_conversion
from .weapon_compile import compile_and_fill,capture_animation_semantics
from .vpk_writer import write_vpk,write_vpk_set
from .one_click import find_studiomdl,_fix_melee_layer_fadein
from .core.constants import STUDIO_LOOPING
from .process_limits import exception_text
from .mdl_names import has_unsupported_names
from .model_status import UNSUPPORTED_MODEL_MESSAGE, UnsupportedModelError


UNSUPPORTED_VIEW_MODEL_MESSAGE = UNSUPPORTED_MODEL_MESSAGE


class UnsupportedViewModelError(UnsupportedModelError):
    pass


def _read_conversion_header(raw, path, *, view_model):
    try:
        header = read_reference_data(raw)
    except ValueError as error:
        if view_model and str(error) == 'include 依赖不是受支持的 Source MDL':
            raise UnsupportedViewModelError() from None
        raise
    if has_unsupported_names(raw, header):
        raise UnsupportedViewModelError()
    return header


@dataclass(frozen=True)
class PackageInspection:
    path: Path
    weapon: Weapon
    view_models: tuple[str, ...]
    world_models: tuple[str, ...]
    entry_count: int

    @property
    def source_pistol_modes(self) -> tuple[str, ...]:
        if self.weapon.id != 'pistol':return ()
        return tuple(mode for mode in ('single','dual') if any(
            p.endswith('/v_dual_pistola.mdl') == (mode=='dual') for p in self.view_models))

    def resolve_view_model(self, source_pistol_mode='single') -> str:
        if source_pistol_mode not in ('single','dual'):
            raise ValueError('手枪模型选择无效（仅支持 single/dual）: '+str(source_pistol_mode))
        if self.weapon.id != 'pistol':return self.view_models[0]
        mode=source_pistol_mode
        found=next((p for p in self.view_models if p.endswith('/v_dual_pistola.mdl')==(mode=='dual')),None)
        if found is None:raise ValueError('VPK 未包含所选'+('双持' if mode=='dual' else '单持')+'手枪来源模型')
        return found

    def resolve_world_model(self, source_pistol_mode='single') -> str | None:
        view=self.resolve_view_model(source_pistol_mode)
        if not self.world_models:return None
        suffix='/w_pistol_b.mdl' if view.endswith('/v_dual_pistola.mdl') else '/w_pistol_a.mdl'
        return next((p for p in self.world_models if p.endswith(suffix)),self.world_models[0])

    @property
    def primary_view_model(self) -> str:
        # Inspection describes the package, independently of conversion defaults.
        return next((p for p in self.view_models if not p.endswith('/v_dual_pistola.mdl')),self.view_models[0])

    @property
    def primary_world_model(self) -> str | None:
        mode='dual' if self.primary_view_model.endswith('/v_dual_pistola.mdl') else 'single'
        return self.resolve_world_model(mode)


@dataclass(frozen=True)
class ConversionOptions:
    input_path: Path
    target_id: str
    output_name: str = ''
    keep_temp: bool = False
    game_path: str = ''
    keep_ads: bool = True
    isolate_materials: bool = False
    compiler_path: str = ''
    l4n_enabled: bool = False
    normalize_vmt: bool = False
    audio_logic: str = 'frequency'
    ik_rule_handling: str = 'delete'
    melee_layer_fadein_fix: bool = False
    source_pistol_mode: str = 'single'
    fps_match: bool = False
    exclude_inspect_fps: bool = False


@dataclass
class PreprocessResult:
    success: bool = False
    cancelled: bool = False
    error: str = ''
    inspection: PackageInspection | None = None
    output_path: Path | None = None
    work_dir: Path | None = None
    decompiled_models: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sequence_plan_files: list[str] = field(default_factory=list)
    converted_qc_files: list[str] = field(default_factory=list)
    audio_report_file: str | None = None
    converted_audio_files: list[str] = field(default_factory=list)
    archive_names: dict[str,str] = field(default_factory=dict)
    ads_report_file: str | None = None
    removed_ads_sequences: list[str] = field(default_factory=list)
    material_report_file: str | None = None
    material_salt: str = ''
    converted_material_files: list[str] = field(default_factory=list)
    compiled_models: list[dict] = field(default_factory=list)
    compiled_model_files: list[str] = field(default_factory=list)
    compile_report_file: str | None = None
    package_sha256: str = ''
    published: bool = False
    l4n_output_path: Path | None = None
    l4n_report_file: str | None = None
    l4n_audio_files: list[str] = field(default_factory=list)
    l4n_event_names: list[str] = field(default_factory=list)
    l4n_package_sha256: str = ''
    l4n_published: bool = False
    audio_slot_report_file: str | None = None
    animation_semantics_file: str | None = None
    qc_name_report_file: str | None = None
    unsupported_view_model: bool = False
    vtx_recoveries: list[dict] = field(default_factory=list)
    # Actual compiler/game resolution for a retained correction session.
    # keep_temp remains the caller's original request, never preprocessing's override.
    conversion_options: ConversionOptions | None = None


def _decompile_options(options):
    # Match the decompile page's real-idle subtract correction, including
    # Delta references without touch IK; semantic preservation alone only
    # requires a real base for touch-IK references in the default IK mode.
    return DecompileOptions(game_path=options.game_path,preserve_animation_semantics=True,
                            subtract_correction=True,
                            validate_ik_errors=options.ik_rule_handling!='default',
                            ik_rule_handling=options.ik_rule_handling,resolve_world_physics_includes=True)


def _validate_processing_options(options):
    if options.source_pistol_mode not in ('single','dual'):raise ValueError('手枪模型选择无效（仅支持 single/dual）: '+str(options.source_pistol_mode))
    if options.audio_logic not in ('frequency','balanced','complete'):raise ValueError('音频处理逻辑无效: '+str(options.audio_logic))
    if options.ik_rule_handling not in ('default','delete','usesource'):raise ValueError('IK 处理方式无效: '+str(options.ik_rule_handling))


def _apply_processing_options(qcs,options,log):
    if options.melee_layer_fadein_fix:
        for qc in qcs:_fix_melee_layer_fadein(str(qc),log)


def _apply_nekomdl_buffer_size(qcs, compiler_path):
    """Persist Neko's fixed buffer setting in final conversion QCs only."""
    if not compiler_path:return
    compiler = Path(compiler_path).resolve()
    if compiler.name.lower() != 'nekomdl.exe' or not compiler.is_file():return
    for path in qcs:
        qc = Path(path)
        text = qc.read_text(encoding='utf-8-sig')
        tokens = _tokens(text)
        changes = []
        depth = 0
        for index, token in enumerate(tokens):
            if token.quoted:continue
            if token.value == '{':depth += 1
            elif token.value == '}':depth -= 1
            elif depth == 0 and token.value.lower() == '$filebuffersize':
                end = token.end
                if index + 1 < len(tokens):
                    value = tokens[index + 1]
                    if value.quoted or (not value.value.startswith('$') and value.value not in ('{', '}')):
                        end = value.end
                changes.append((token.start, end, '$FileBufferSize 32' if not changes else ''))
        updated = text
        for start, end, value in reversed(changes):
            updated = updated[:start] + value + updated[end:]
        if not changes:updated = '$FileBufferSize 32\n' + updated
        if updated != text:qc.write_text(updated, encoding='utf-8')


def inspect_weapon_package(path: Path) -> PackageInspection:
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != '.vpk':
        raise ValueError('请选择存在的 .vpk 文件')
    archive = VpkArchive(path, include_all=True)
    found = [w for w in WEAPONS if any(p in archive.entries for p in w.view_models)]
    if not found:
        raise ValueError('VPK 中未找到支持的枪械或近战 v 模型')
    if len(found) != 1:
        raise ValueError('VPK 包含多种武器，无法确定单一来源: ' + '、'.join(w.name for w in found))
    weapon = found[0]
    views = tuple(p for p in weapon.view_models if p in archive.entries)
    worlds = tuple(p for p in weapon.world_models if p in archive.entries)
    if weapon.id=='pistol':
        singles=[p for p in views if not p.endswith('/v_dual_pistola.mdl')]
        if len(singles)>1:raise ValueError('VPK 包含多套单持手枪模型，无法确定来源')
    elif len(views)!=1:
        raise ValueError('除单双持手枪组合外，只允许一个主 v 模型')
    all_worlds={p for w in WEAPONS for p in w.world_models if p in archive.entries}
    if all_worlds!=set(worlds):
        raise ValueError('VPK 包含其他武器的主 w 模型，无法确定单一来源')
    for p in views + worlds:
        header=_read_conversion_header(archive.read(p),p,view_model=p in views)
    return PackageInspection(path, weapon, views, worlds, len(archive.entries))


def plan_output(path: Path, target_id: str, name: str = '') -> Path:
    get_weapon(target_id)
    source = Path(path).resolve()
    filename = name.strip() or f'{source.stem}_{target_id}'
    if not filename.lower().endswith('.vpk'):
        filename += '.vpk'
    filename = safe_archive_path(filename)
    if '/' in filename:
        raise ValueError('结果名称只能是文件名，不能包含目录')
    output = source.parent / filename
    if output == source:
        raise ValueError('结果 VPK 不能覆盖输入 VPK')
    return output


def _create_workspace(parent: Path) -> Path:
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%y%m%d%H%M%S')
    for _ in range(100):
        path = parent / f'temp_{stamp}{secrets.randbelow(1000):03d}'
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path.resolve()
    raise FileExistsError('无法创建唯一的转模临时目录')


def _backup_model(path: Path, rel: str, source_dir: Path) -> None:
    suffixes = {'.mdl','.vvd','.phy','.ani','.vtx','.dx11.vtx','.dx90.vtx','.dx80.vtx','.sw.vtx','.xbox.vtx'}
    for file in sorted(path.parent.iterdir()):
        if not file.is_file() or not file.name.lower().startswith(path.stem.lower()+'.'):
            continue
        if file.name[len(path.stem):].lower() not in suffixes:
            continue
        destination = source_dir / PurePosixPath(rel).parent / file.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != file.read_bytes():
                raise ValueError(f'原始模型备份冲突: {destination}')
        else:
            shutil.copy2(file, destination)


def preprocess_weapon_package(options: ConversionOptions, *, on_stage=None, on_detail=None,
                              cancelled=None) -> PreprocessResult:
    stage = on_stage or (lambda text: None)
    detail = on_detail or (lambda text: None)
    _validate_processing_options(options)
    stage('1/7 解析 VPK 和来源武器')
    try:
        inspection = inspect_weapon_package(options.input_path)
    except UnsupportedViewModelError as error:
        detail(str(error))
        return PreprocessResult(error=str(error),unsupported_view_model=True)
    primary=inspection.resolve_view_model(options.source_pistol_mode).lower()
    world=inspection.resolve_world_model(options.source_pistol_mode)
    source_profile='dual_pistol' if primary.endswith('/v_dual_pistola.mdl') else inspection.weapon.id
    validate_target(inspection.weapon.id,options.target_id)
    output = plan_output(inspection.path, options.target_id, options.output_name)
    if output.exists():
        raise FileExistsError(f'结果文件已存在，请修改结果名称: {output}')
    result = PreprocessResult(inspection=inspection, output_path=output)
    if inspection.weapon.id=='pistol' and world:
        wanted='w_pistol_b.mdl' if source_profile=='dual_pistol' else 'w_pistol_a.mdl'
        if not world.endswith('/'+wanted):
            result.warnings.append('所选手枪来源未包含 '+wanted+'，世界模型回退为 '+world+'。')
    work = None

    def check_cancel():
        if cancelled and cancelled():
            raise InterruptedError('预处理已取消')

    try:
        check_cancel()
        stage('2/7 完整解包原始内容并创建独立待打包副本')
        work = _create_workspace(inspection.path.parent)
        for name in ('unpack', 'pack', 'v_dec', 'w_dec', 'source'):
            (work/name).mkdir()
        archive = VpkArchive(inspection.path, include_all=True)
        unpacked = archive.extract_to(work/'unpack', cancelled=cancelled)
        detail(f'已解包 {len(unpacked)} 个文件到 {work / "unpack"}')
        # Keep the complete extraction untouched. Never share hard links with pack.
        for path in unpacked.values():
            check_cancel()
            destination=work/'pack'/path.relative_to(work/'unpack')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination)
        check_cancel()
        result.archive_names=dict(archive.original_names)
        detail(f'已复制独立待打包内容到 {work / "pack"}；unpack 仅作为原始输入')
        stage('3/7 备份原始 v/w 模型和附属文件')
        backups = [('v', rel, unpacked[rel]) for rel in inspection.view_models]
        backups += [('w', rel, unpacked[rel]) for rel in inspection.world_models]
        queue = [('v', primary, unpacked[primary])]
        if world:queue.append(('w',world,unpacked[world]))
        if not inspection.world_models:
            result.warnings.append('VPK 未包含对应 w 模型，本次只处理 v 模型。')
        for _kind, rel, path in backups:
            check_cancel()
            _backup_model(path, rel, work/'source')
        stage('4/7 反编译模型及递归 include 依赖')
        visited = set()
        view_sources = {}
        view_includes = {}
        model_semantics = {}
        semantic_origins = {}
        qc_sources = {}
        decompiled = []
        while queue:
            check_cancel()
            kind, rel, path = queue.pop(0)
            key = (kind, rel.lower())
            if key in visited:
                continue
            visited.add(key)
            _backup_model(path, rel, work/'source')
            model_rel = PurePosixPath(rel).relative_to('models').with_suffix('')
            destination = work/(kind+'_dec')/str(model_rel)
            detail(f'反编译 {rel}')
            dec = MdlDecompiler(_decompile_options(options))
            outcome = dec.decompile(str(path), str(destination), progress=detail)
            if not outcome.success:
                if UNSUPPORTED_MODEL_MESSAGE in outcome.errors:
                    raise UnsupportedModelError()
                raise ValueError(f'{rel} 反编译失败: ' + '; '.join(outcome.errors))
            result.warnings.extend(outcome.warnings)
            for recovery in outcome.vtx_recoveries:
                result.vtx_recoveries.append(dict(recovery,model=rel))
                detail(f"VTX MDL 引导恢复 {rel}: {recovery['original']} → {recovery['recovered']}")
            if result.vtx_recoveries:
                (work/'vtx_recovery.json').write_text(json.dumps(result.vtx_recoveries,ensure_ascii=False,indent=2),encoding='utf-8')
            decompiled.append((kind,rel.lower(),dec))
            if kind=='v':
                view_includes[rel.lower()]=[]
            includes = outcome.included_files
            if dec.mdl_file_data.theModelGroups and not includes:
                includes = copy_include_dependencies(str(path), dec.mdl_file_data, str(destination), options.game_path)
            for copied in includes:
                copied = Path(copied)
                if copied.suffix.lower() != '.mdl':
                    continue
                dependency_rel = 'models/' + copied.relative_to(destination/'models').as_posix()
                dependency_path = unpacked.get(dependency_rel.lower(), copied)
                queue.append((kind, dependency_rel, dependency_path))
                if kind=='v':view_includes[rel.lower()].append(dependency_rel.lower())
            result.decompiled_models.append(rel)
        check_cancel()
        detail('QC 前置校验：修复名称并同步声明、引用及动作来源')
        original_sequence_names={rel:[s.theName for s in dec.mdl_file_data.theSequenceDescs or []]
                                 for _kind,rel,dec in decompiled}
        qc_sources,repairs=prepare_conversion_qcs((rel,dec) for _kind,rel,dec in decompiled)
        report_path=work/'v_dec/qc_preflight.json'
        report_path.write_text(json.dumps({'qcs':{rel:str(path) for rel,path in qc_sources.items()},
                                          'repairs':repairs},ensure_ascii=False,indent=2),encoding='utf-8')
        result.qc_name_report_file=str(report_path)
        if repairs:
            notice=f'QC 前置校验修复 {len(repairs)} 处名称/资源引用；原始反编译文件保留，详情见 {report_path}'
            detail(notice);result.warnings.append(notice)
        for kind,rel,dec in decompiled:
            check_cancel()
            data=dec.mdl_file_data
            model_semantics[rel]=capture_animation_semantics(data,ik_rule_handling=options.ik_rule_handling,
                validate_ik_errors=options.ik_rule_handling!='default')
            if kind!='v':continue
            animations=data.theAnimationDescs or [];sources=[]
            dependencies=sequence_dependencies(qc_sources[rel].read_text(encoding='utf-8-sig'))
            for sequence_index,sequence in enumerate(data.theSequenceDescs or []):
                indexes=sequence.theAnimDescIndexes or []
                animation=animations[indexes[0]] if indexes and 0<=indexes[0]<len(animations) else None
                sources.append(SourceSequence(sequence.theName,sequence.theActivityName,
                    animation.frameCount if animation else 1,animation.fps if animation else 30,
                    bool(sequence.flags & STUDIO_LOOPING),
                    tuple(SourceAnimation(animations[j].theName,animations[j].frameCount,animations[j].fps,
                                          bool(animations[j].flags & 4))
                          for j in indexes if 0<=j<len(animations)),
                    original_name=original_sequence_names[rel][sequence_index],
                    dependencies=dependencies.get(sequence.theName.lower(),())))
            view_sources[rel]=sources
        # Retain only the planning/origin facts, not all decoded model payloads.
        decompiled.clear()
        stage('5/7 校验官方 sequence 顺序与帧/FPS 计划')

        def gather(rel,seen):
            if rel in seen:return []
            seen.add(rel)
            found=list(view_sources.get(rel,[]))
            for child in view_includes.get(rel,[]):found.extend(gather(child,seen))
            return found

        # Local sequence labels override same-labelled virtual/include sequences.
        effective={}
        for source in gather(primary,set()):effective.setdefault(source.name.lower(),source)
        target_profiles=target_profiles_for(options.target_id)
        reports=[];plans={}
        for target_profile in target_profiles:
            plan=plan_sequences(effective.values(),source_profile,target_profile,keep_ads=options.keep_ads,
                                fps_match=options.fps_match,exclude_inspect_fps=options.exclude_inspect_fps)
            problems=validate_sequence_layout(plan.sequences,target_profile)
            if problems:raise ValueError('sequence 计划校验失败: '+'; '.join(problems))
            plans[target_profile]=plan
            reports.append({'target_profile':target_profile,'sequences':[asdict(s) for s in plan.sequences],
                            'missing_actions':plan.missing_actions,'warnings':plan.warnings,
                            'excluded_ads_sequences':plan.excluded_sequences})
            if plan.missing_actions:
                detail(f'{target_profile} 缺少动作，需要转换阶段处理: '+', '.join(plan.missing_actions))
            for warning in plan.warnings:
                if warning not in result.warnings:result.warnings.append(warning)
                detail(warning)
            for name in plan.excluded_sequences:detail(f'排除 ADS/MIXED sequence: {name}')
        result.removed_ads_sequences=list(dict.fromkeys(name for plan in plans.values() for name in plan.excluded_sequences))
        report_path=work/'v_dec/sequence_plan.json'
        report_path.write_text(json.dumps({'source_view_model':primary,'source_world_model':world,'source_pistol_mode':options.source_pistol_mode,'source_profile':source_profile,'keep_ads':options.keep_ads,
                                         'fps_match':options.fps_match,'exclude_inspect_fps':options.exclude_inspect_fps,
                                         'targets':reports},ensure_ascii=False,indent=2),encoding='utf-8')
        result.sequence_plan_files.append(str(report_path))
        detail(f'已生成 sequence 顺序和时长计划: {report_path}')
        check_cancel()
        if any(p.missing_actions for p in plans.values()):
            result.warnings.append('缺少必需动作，仅生成分析计划，未生成转换 QC。')
        else:
            stage('生成并校验独立转换 QC')
            audio_targets=[];ads_reports=[]
            def include_qcs(rel,seen):
                if rel in seen:return []
                seen.add(rel);found=[]
                for child in view_includes.get(rel,[]):
                    if child not in seen:
                        found.append(qc_sources[child]);found.extend(include_qcs(child,seen))
                return found
            libraries=include_qcs(primary,set())
            original_view_qcs=(qc_sources[primary],*libraries)
            original_semantics={}
            def collect_semantics(rel,seen):
                if rel in seen:return
                seen.add(rel)
                for name,origin in model_semantics[rel].items():original_semantics.setdefault(name,origin)
                for child in view_includes.get(rel,[]):collect_semantics(child,seen)
            collect_semantics(primary,set())
            for target_profile,plan in plans.items():
                check_cancel()
                ads_layers=[]
                fps_matches=[]
                qc=rewrite_viewmodel_qc(qc_sources[primary],libraries,plan,target_profile,
                                       work/'v_dec/_converted'/target_profile,ads_removals=ads_layers,fps_matches=fps_matches)
                for match in fps_matches:
                    prefix=f'FPS匹配 {match["sequence"]} <- {match["source"]} / {match["reference"]}: '
                    if match['fallback_reason']:
                        detail(prefix+f'{match["fallback_reason"]} ({match["fps"]:g})。')
                    else:
                        detail(prefix+f'{match["original_fps"]:g} × {match["scale"]:g} = {match["fps"]:g} FPS，保留 {match["frames"]} 帧。')
                ads_reports.append({'target_profile':target_profile,'qc':str(qc),
                                    'removed_sequences':list(plan.excluded_sequences),'removed_layers':ads_layers})
                result.converted_qc_files.append(str(qc))
                mapped_origins={}
                for row in plan.sequences:
                    if row.source is None:continue
                    origin=deepcopy(original_semantics[row.source.name])
                    if row.source_reference_index is not None:
                        index=row.source_reference_index
                        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(origin['references']):
                            raise ValueError('来源动画语义引用不存在: '+row.source.name)
                        origin['references']=[origin['references'][index]]
                    if row.idle_fallback:
                        # This is a new bare-SMD helper, not a clone of idle's QC
                        # animation/sequence constraints. Keep all other origins strict.
                        origin={'delta_post':0,'references':[{'delta':False,'ik':[],
                                'frames':row.source.frame_count}],'ik_guard':False}
                    mapped_origins[row.name]=origin
                semantic_origins[str(qc.resolve())]=mapped_origins
                audio_targets.append((qc,target_profile))
                detail(f'已生成转换 v QC: {qc}')
                for layer in ads_layers:detail(f'移除 ADS/MIXED 层引用: {layer["command"]} / {layer["directive"]} {layer["source_sequence"]}')
            ads_report=work/'v_dec/ads_conversion.json'
            ads_report.write_text(json.dumps({'keep_ads':options.keep_ads,'targets':ads_reports},ensure_ascii=False,indent=2),encoding='utf-8')
            result.ads_report_file=str(ads_report)
            detail(f'ADS/MIXED：{"保留" if options.keep_ads else "移除"}；排除 {len(result.removed_ads_sequences)} 个源 sequence；报告 {ads_report}')
            if world:
                for i,target_world in enumerate(get_weapon(options.target_id).world_models):
                    check_cancel()
                    qc=rewrite_worldmodel_qc(qc_sources[world.lower()],target_world.removeprefix('models/'),
                                            work/'w_dec/_converted'/f'{options.target_id}_{i}')
                    result.converted_qc_files.append(str(qc))
                    semantic_origins[str(qc.resolve())]=model_semantics[world.lower()]
                    audio_profile=target_profiles[i] if options.target_id=='pistol' else target_profiles[0]
                    audio_targets.append((qc,audio_profile))
                    detail(f'已生成转换 w QC: {qc}')
            _apply_processing_options(result.converted_qc_files,options,detail)
            origin_path=work/'v_dec/animation_semantics.json'
            origin_path.write_text(json.dumps(semantic_origins,ensure_ascii=False,indent=2),encoding='utf-8')
            result.animation_semantics_file=str(origin_path)
            check_cancel();stage('6/7 转换声音事件及包内 WAV 路径')
            from .weapon_audio import sound_reference
            sounds=sound_reference()
            metadata_paths={p for profile in target_profiles for name in sounds.profiles[profile].values() for p in sounds.events[name.lower()].paths}
            target_metadata=load_target_metadata(paths=metadata_paths)
            l4n_plan=plan_l4n_conversion(work/'pack',original_view_qcs,target_profiles,source_profile,
                audio_logic=options.audio_logic,target_metadata=target_metadata,cancelled=cancelled)
            event_overrides={profile:dict(values) for profile,values in l4n_plan.event_overrides.items()}
            preserve_events={profile:set(values) for profile,values in l4n_plan.preserve_events.items()}
            retain_paths=set(l4n_plan.retain_wave_paths)
            remove_sources=list(l4n_plan.remove_wave_sources)
            applied_decisions=[dict(d) for d in l4n_plan.decisions]
            overflow_paths={item['source_path'] for item in l4n_plan.extra_sounds}
            retained_overflow=set()
            melee_audio_cleanup=inspection.weapon.category=='melee'
            if options.l4n_enabled:
                l4n=build_l4n_pack(l4n_plan,work/'l4n/pack',
                    title=f'{inspection.path.stem}_{options.target_id}_L4N音频',cancelled=cancelled)
                result.l4n_audio_files=l4n.wave_files;result.l4n_event_names=l4n.event_names
                result.warnings.extend(l4n.warnings)
                if l4n.pack_dir:
                    result.l4n_output_path=output.with_name(output.stem+'_L4N音频.vpk')
                    if result.l4n_output_path==inspection.path or result.l4n_output_path.exists():
                        raise FileExistsError('L4N 结果不能覆盖已有文件: '+str(result.l4n_output_path))
                l4n_report=work/'v_dec/l4n_conversion.json'
                l4n_report.write_text(json.dumps({**asdict(l4n),'enabled':True,
                    'source_qcs':[str(p) for p in original_view_qcs]},ensure_ascii=False,indent=2),encoding='utf-8')
                result.l4n_report_file=str(l4n_report)
                detail(f'L4N 准备：{len(l4n.wave_files)} 个过剩 WAV；报告 {l4n_report}')
            else:
                retained_overflow=set() if melee_audio_cleanup else set(overflow_paths)
                retain_paths.update(retained_overflow)
                remove_sources=[p for p in remove_sources if p not in retained_overflow]
                for profile,values in event_overrides.items():
                    for name,(_,reason) in list(values.items()):
                        if reason=='l4n':
                            preserve_events[profile].add(name);del values[name]
                for decision in applied_decisions:
                    if decision['reason']=='l4n':
                        decision['reason']='dropped_source_audio' if melee_audio_cleanup else 'retained_overflow'
                        decision.pop('target_event',None);decision.pop('archive_path',None)
                if retained_overflow:
                    result.warnings.append(f'目标声音槽容量不足，L4N 未开启；保留 {len(retained_overflow)} 个过剩原始 WAV 及声音事件。')
                elif melee_audio_cleanup and overflow_paths:
                    result.warnings.append(f'目标声音槽容量不足，L4N 未开启；移除 {len(overflow_paths)} 个过剩近战 WAV 及声音事件。')
                # OFF must still preserve the allocation's planning hash boundary.
                for source,digest in l4n_plan.source_hashes.items():
                    check_cancel()
                    with source.open('rb') as handle:
                        if hashlib.file_digest(handle,'sha256').hexdigest()!=digest:
                            raise ValueError('规划后源音频已修改: '+str(source))
            result.warnings.extend(w for w in l4n_plan.warnings if w not in result.warnings)
            l4n_keywords=dict(event_overrides=event_overrides,preserve_events=preserve_events,
                extra_wave_requests=l4n_plan.wave_requests,remove_wave_sources=remove_sources,retain_wave_paths=retain_paths)
            audio_plan=plan_audio_conversion(work/'pack',audio_targets,source_profile,
                original_names=archive.original_names,target_metadata=target_metadata,cancelled=cancelled,
                expected_source_hashes=l4n_plan.source_hashes,**l4n_keywords)
            audio=apply_audio_conversion(audio_plan,cancelled=cancelled)
            result.warnings.extend(audio.warnings)
            result.converted_audio_files=audio.converted_files
            result.archive_names=audio.archive_names
            audio_report=work/'v_dec/audio_conversion.json'
            audio_report.write_text(json.dumps(asdict(audio),ensure_ascii=False,indent=2),encoding='utf-8')
            result.audio_report_file=str(audio_report)
            slot_report=work/'v_dec/audio_slot_allocation.json'
            slot_report.write_text(json.dumps({'enabled':options.l4n_enabled,'source_qcs':[str(p) for p in original_view_qcs],
                'source_profile':source_profile,'source_view_model':primary,'source_world_model':world,'source_pistol_mode':options.source_pistol_mode,'target_id':options.target_id,'target_family':target_family_id(options.target_id),
                'statistics':l4n_plan.statistics,'audio_logic':options.audio_logic,'decisions':applied_decisions,'event_overrides':event_overrides,
                'source_wave_analysis':l4n_plan.source_wave_analysis,
                'preserve_events':{p:sorted(values) for p,values in preserve_events.items()},
                'wave_requests':l4n_plan.wave_requests,'retain_wave_paths':sorted(retain_paths),'remove_wave_sources':remove_sources,
                'retained_overflow_paths':sorted(retained_overflow),'retained_overflow_wav_count':len(retained_overflow),
                'dropped_overflow_paths':sorted(overflow_paths) if melee_audio_cleanup and not options.l4n_enabled else [],
                'generated_extra_wav_count':len(result.l4n_audio_files),'applied_wave_manifest':audio.file_manifest},
                ensure_ascii=False,indent=2),encoding='utf-8')
            result.audio_slot_report_file=str(slot_report)
            detail(f'音频：{audio.changed_events} 个声音事件、{len(audio.converted_files)} 个目标 WAV；报告 {audio_report}')
            for decision in audio.decisions:
                if decision['reason'] in ('normal_slot','l4n'):
                    detail(f'声音映射 {decision["source"]} → {decision["target"]}')
                elif decision['reason']=='dropped_source_audio':detail(f'移除来源专属声音 {decision["source"]}')
                elif decision['reason']!='converted':detail(f'保留声音 {decision["source"]}: {decision["reason"]}')
            check_cancel();stage('7/7 处理材质分离与 HUD 映射')
            material_plan=plan_material_conversion(work/'pack',result.converted_qc_files,inspection.weapon.id,
                target_family_id(options.target_id),isolate=options.isolate_materials,original_names=result.archive_names,
                normalize_vmt=options.normalize_vmt)
            materials=apply_material_conversion(material_plan,cancelled=cancelled)
            result.warnings.extend(materials.warnings)
            result.archive_names=materials.archive_names;result.material_salt=materials.salt
            result.converted_material_files=[item['archive_path'] for item in materials.file_manifest]
            material_report=work/'v_dec/material_conversion.json'
            material_report.write_text(json.dumps(asdict(materials),ensure_ascii=False,indent=2),encoding='utf-8')
            result.material_report_file=str(material_report)
            detail(f'材质/HUD：{len(materials.file_manifest)} 个文件、{len(materials.qc_bindings)} 个模型材质映射；目录 {materials.salt or "不分离"}；报告 {material_report}')
            if options.normalize_vmt:
                detail(f'VMT规范化：{len(materials.normalization_repairs)} 个文件修复；原始输入保留，详情见材质报告')
        stage('7/7 预处理完成（转换源及资源就绪或附缺失说明，尚未编译封包）')
        result.success = True
    except InterruptedError as error:
        result.cancelled = True
        result.error = '\n'.join([str(error),*getattr(error,'__notes__',())])
    except UnsupportedModelError:
        result.unsupported_view_model = True
        result.error = UNSUPPORTED_MODEL_MESSAGE
    except Exception as error:
        result.error = '\n'.join([exception_text(error),*getattr(error,'__notes__',())])
    finally:
        if work is not None:
            if options.keep_temp:
                result.work_dir = work
            else:
                # Verify the exact owned absolute directory before recursive deletion.
                if work.resolve() != work or work.parent != inspection.path.parent.resolve() or not work.name.startswith('temp_'):
                    raise RuntimeError(f'临时目录位置改变，拒绝清理: {work}')
                shutil.rmtree(work)
    return result


def convert_weapon_package(options:ConversionOptions,*,on_stage=None,on_detail=None,cancelled=None)->PreprocessResult:
    """Complete the approved pipeline; preprocessing callers retain their API."""
    result=PreprocessResult();work=None;stage=on_stage or (lambda text:None);detail=on_detail or (lambda text:None)
    def check():
        if cancelled and cancelled():raise InterruptedError('武器转模已取消')
    def preprocessing_stage(message):
        if message.startswith('7/7 预处理完成'):return
        stage(message.replace('/7 ','/9 ',1))
    try:
        _validate_processing_options(options)
        check()
        try:inspect_weapon_package(options.input_path)
        except UnsupportedViewModelError as error:
            detail(str(error))
            return PreprocessResult(error=str(error),unsupported_view_model=True)
        except MemoryError:
            raise
        except Exception:
            # Only an unsupported V overrides compiler-configuration priority.
            # Other inspection errors are reported by normal preprocessing.
            pass
        compiler=options.compiler_path.strip();game=options.game_path
        if not compiler:
            compiler,found_game=find_studiomdl(game)
            game=found_game or game
        if not compiler or not Path(compiler).is_file():raise ValueError('请在设置中配置有效的模型编译器')
        compiler=str(Path(compiler).resolve())
        result=preprocess_weapon_package(replace(options,keep_temp=True,game_path=game),on_stage=preprocessing_stage,
                                         on_detail=detail,cancelled=cancelled)
        result.conversion_options=replace(options,compiler_path=compiler,game_path=game)
        work=result.work_dir
        if not result.success:return result
        result.success=False
        if not result.converted_qc_files:raise ValueError('缺少必需动作，不能编译或生成转模 VPK')
        _apply_nekomdl_buffer_size(result.converted_qc_files,compiler)
        check();stage('8/9 编译、验证并回填目标模型')
        bindings=json.loads(Path(result.material_report_file).read_text(encoding='utf-8'))['qc_bindings'] if result.material_report_file else []
        compiled=compile_and_fill(work/'pack',result.converted_qc_files,compiler,game_path=game,
            retired_models=result.inspection.view_models+result.inspection.world_models,archive_names=result.archive_names,
            keep_ads=options.keep_ads,bindings=bindings,cancelled=cancelled,on_log=detail,
            semantic_origins=json.loads(Path(result.animation_semantics_file).read_text(encoding='utf-8')) if result.animation_semantics_file else None)
        result.archive_names=compiled.archive_names;result.compiled_models=compiled.models;result.compiled_model_files=compiled.compiled_files
        report=work/'compile_result.json';report.write_text(json.dumps(asdict(compiled),ensure_ascii=False,indent=2),encoding='utf-8');result.compile_report_file=str(report)
        check();stage('9/9 封包并验证最终 VPK')
        if result.l4n_audio_files:
            extra,package=write_vpk_set([(work/'l4n/pack',result.l4n_output_path,None),
                (work/'pack',result.output_path,result.archive_names)],forbidden_paths=[result.inspection.path],
                cancelled=cancelled,on_log=detail)
            result.l4n_package_sha256=extra.sha256;result.l4n_published=True
        else:
            package=write_vpk(work/'pack',result.output_path,archive_names=result.archive_names,
                              forbidden_paths=[result.inspection.path],cancelled=cancelled,on_log=detail)
        result.package_sha256=package.sha256;result.published=True;result.success=True
    except InterruptedError as error:
        result.cancelled=True;result.error='\n'.join([str(error),*getattr(error,'__notes__',())])
    except UnsupportedModelError:
        result.unsupported_view_model=True;result.error=UNSUPPORTED_MODEL_MESSAGE
    except Exception as error:
        result.error='\n'.join([exception_text(error),*getattr(error,'__notes__',())])
    finally:
        if work is not None and not options.keep_temp:
            if work.resolve()!=work or work.parent!=result.inspection.path.parent or not work.name.startswith('temp_'):
                raise RuntimeError('工作目录位置改变，拒绝清理')
            try:shutil.rmtree(work);result.work_dir=None
            except OSError as error:result.warnings.append('临时目录清理失败，保留在 '+str(work)+': '+str(error))
    return result
