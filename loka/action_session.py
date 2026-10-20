"""FPS drafts bound to one published, explicitly retained conversion result."""
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

from .weapon_catalog import target_profiles_for
from .weapon_compile import _model_name, validate_compiled_model
from .vpk import safe_archive_path
from .fps_values import round_fps,scaled_fps


def with_timing_reference(action,profile_id,index):
    """Validate a user-selected timer for an extra slot without changing identity."""
    from .weapon_sequences import get_profile
    from .action_rules import _timing,_timing_activity
    profile=get_profile(profile_id)
    if type(index) is not int or not 0<=index<len(profile.slots):raise ValueError('无效的官方计时槽。')
    slot=profile.slots[index]
    if slot.unused or _timing_activity(slot.activity,profile)!=_timing_activity(action.activity,profile):
        raise ValueError('所选官方计时槽与目标 Activity 不匹配。')
    duration,count,fps,note=_timing(action.activity,profile,slot,False)
    if duration is None or action.official_index is not None:
        raise ValueError('官方动作使用其固定实际计时；仅额外动作可以选择计时参考。')
    return replace(action,official_duration=duration,official_frame_count=count,official_fps=fps,
                   timing_note=f'参考官方计时槽 {slot.name}[顺位：{index}]；'+note)


def fingerprint(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def owned_path(path, root):
    """Refuse redirected files/directories even if the redirected target is local."""
    path = Path(path).absolute()
    root = Path(root).absolute()
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError('动作矫正路径越界或被重定向: ' + str(path))
    for part in (path, *path.parents):
        if part == root.parent:break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('动作矫正不接受链接路径: ' + str(part))
    return path


@dataclass(frozen=True)
class CorrectionModel:
    profile_id: str
    qc_path: Path | None
    model_path: Path
    actions: tuple
    swap_actions: tuple = ()
    timing_indexes: tuple = ()


@dataclass(frozen=True)
class FpsChange:
    model_index: int
    model: CorrectionModel
    action: object
    old_fps: float
    new_fps: float


class CorrectionSession:
    def __init__(self, result, options):
        self.result = result
        self.options = options
        self.work_dir = Path(result.work_dir).absolute()
        self.pack_dir = self.work_dir / 'pack'
        self.output_path = Path(result.output_path).absolute()
        self.input_path = Path(options.input_path).absolute()
        self.models = ()
        self.updates = {}
        self.assignments = {}
        self.timing_overrides = {}
        self._preview_models = {}
        self.fingerprints = {}
        self._inventory = set()

    @classmethod
    def from_result(cls, result, options):
        if (options is None or not options.keep_temp or not result.success or not result.published
                or not result.work_dir or not result.output_path):
            raise ValueError('只有成功发布并开启保留临时文件的转模结果才能进行动作矫正。')
        effective=result.conversion_options or options
        if (not effective.keep_temp or effective.input_path != options.input_path
                or effective.target_id != options.target_id):
            raise ValueError('本次转模的实际处理参数与会话请求不符。')
        session = cls(result, effective)
        work, output, source = session.work_dir, session.output_path, session.input_path
        if (not work.name.startswith('temp_') or work.parent != source.parent
                or output.parent != source.parent or output == source or output.is_relative_to(work)
                or output.suffix.lower() != '.vpk' or not work.is_dir()
                or not output.is_file() or not source.is_file()):
            raise ValueError('成品、来源或保留临时目录身份无效。')
        owned_path(work, work)
        owned_path(output, output.parent)
        owned_path(source, source.parent)
        from .action_save import recover_pending, save_lock
        with save_lock(work):
            recover_pending(work, output, source, locked=True)
            # A recovered in-memory result may carry the hash from before a crash;
            # persistent accepted state records the successful replacement hash.
            state = work / 'action_correction_state.json'
            expected = result.package_sha256
            if state.is_file():
                owned_path(state,work)
                saved = json.loads(state.read_text(encoding='utf-8'))
                if saved.get('output_path') != str(output):raise ValueError('动作矫正记录与成品不符。')
                expected = saved['package_sha256']
            if not expected or fingerprint(output) != expected:
                raise ValueError('成品 VPK 已被修改，请重新载入正确的转模结果。')
            if state.is_file():
                accepted=owned_path(work/'compile_result.json',work)
                report=json.loads(accepted.read_text(encoding='utf-8'))
                result.compiled_models=report['models']
                result.compiled_model_files=report['compiled_files']
                result.archive_names=saved['archive_names']
                result.compile_report_file=str(accepted)
                result.package_sha256=expected
            session._reload_models()
            session._capture()
        return session

    def _report_paths(self):
        values = list(self.result.sequence_plan_files)
        values += [self.result.material_report_file, self.result.animation_semantics_file,
                   self.result.compile_report_file]
        return [owned_path(p, self.work_dir) for p in values if p]

    def _tracked_files(self):
        files = {self.output_path, self.input_path}
        roots = [self.pack_dir]
        roots.extend(m.qc_path.parent for m in self.models)
        for root in set(roots):
            owned_path(root, self.work_dir)
            if not root.is_dir():raise ValueError('矫正资源目录已丢失: ' + str(root))
            for path in root.rglob('*'):
                owned_path(path, self.work_dir)
                if path.is_file():files.add(path)
        files.update(self._report_paths())
        state = self.work_dir / 'action_correction_state.json'
        if state.is_file():files.add(owned_path(state, self.work_dir))
        return files

    def _capture(self):
        self._inventory = self._tracked_files()
        self.fingerprints = {p:fingerprint(p) for p in self._inventory}

    def check_unchanged(self):
        if self._tracked_files() != self._inventory:
            raise ValueError('矫正资源清单已被修改，请重新载入。')
        for path, digest in self.fingerprints.items():
            owned_path(path, self.work_dir if path.is_relative_to(self.work_dir) else path.parent)
            if not path.is_file() or fingerprint(path) != digest:
                raise ValueError('矫正资源已被修改，请重新载入: ' + str(path))

    def _reload_models(self):
        from .action_rules import load_actions
        self._report_paths()
        targets = {}
        for path in self.result.sequence_plan_files:
            owned_path(path, self.work_dir)
            for item in json.loads(Path(path).read_text(encoding='utf-8'))['targets']:
                profile = item['target_profile']
                if profile in targets:raise ValueError('重复的动作目标配置: ' + profile)
                targets[profile] = item
        wanted = target_profiles_for(self.options.target_id)
        if set(targets) != set(wanted):raise ValueError('动作配置与本次转模目标不符。')
        origins = (json.loads(Path(self.result.animation_semantics_file).read_text(encoding='utf-8'))
                   if self.result.animation_semantics_file else {})
        bindings = (json.loads(Path(self.result.material_report_file).read_text(encoding='utf-8'))['qc_bindings']
                    if self.result.material_report_file else [])
        models = []
        for profile in wanted:
            candidates = [Path(p).absolute() for p in self.result.converted_qc_files
                          if Path(p).parent.name == profile]
            if len(candidates) != 1:raise ValueError('无法唯一定位成品 V 模 QC: ' + profile)
            qc = owned_path(candidates[0], self.work_dir / 'v_dec/_converted')
            relative = safe_archive_path(_model_name(qc))
            model = owned_path(self.pack_dir / relative, self.pack_dir)
            if not any(Path(item['qc']).absolute() == qc and item['model'].lower() == relative.lower()
                       for item in self.result.compiled_models):
                raise ValueError('成品模型没有匹配的编译验证记录。')
            rows = targets[profile]['sequences']
            indexes = {i:row['official_index'] for i,row in enumerate(rows)}
            data, _ = validate_compiled_model(model, qc, keep_ads=self.options.keep_ads,
                bindings=bindings, semantic_origins=origins.get(str(qc)))
            sequences = data.theSequenceDescs or []
            if len(rows) != len(sequences) or any(row['name'] != s.theName
                    for row,s in zip(rows,sequences)):
                raise ValueError('成品动作身份与转换计划不符。')
            actions = load_actions(qc, profile, indexes)
            swap_actions=load_actions(qc,profile,indexes,include_timers=True)
            timings={i:row['action_correction_timing_index'] for i,row in enumerate(rows)
                     if row.get('action_correction_timing_index') is not None}
            actions=tuple(with_timing_reference(a,profile,timings[a.index]) if a.index in timings else a for a in actions)
            swap_actions=tuple(with_timing_reference(a,profile,timings[a.index]) if a.index in timings else a for a in swap_actions)
            if set(timings)-{a.index for a in swap_actions}:raise ValueError('计时参考绑定到无 Activity 的槽位。')
            models.append(CorrectionModel(profile, qc, model, actions,swap_actions,tuple(sorted(timings.items()))))
        self.models = tuple(models)

    def baseline_action(self, model_index, sequence_index, *, all_slots=False):
        if (type(model_index) is not int or not 0 <= model_index < len(self.models)
                or type(sequence_index) is not int):raise ValueError('无效的模型或动作顺位。')
        model=self.models[model_index]
        rows=(model.swap_actions or model.actions) if all_slots else model.actions
        found = next((a for a in rows if a.index == sequence_index), None)
        if found is None:raise ValueError('所选顺位不是已确认的播放动作。')
        return found

    def source_index(self,model_index,sequence_index):
        return self.assignments.get((model_index,sequence_index),sequence_index)

    def action(self,model_index,sequence_index,*,all_slots=False):
        destination=self.baseline_action(model_index,sequence_index,all_slots=all_slots)
        source=self.baseline_action(model_index,self.source_index(model_index,sequence_index),all_slots=True)
        action=replace(destination,fps=source.fps,frame_count=source.frame_count,
                       smd_frames=source.smd_frames,loop=source.loop)
        timing=self.timing_index(model_index,sequence_index)
        return with_timing_reference(action,self.models[model_index].profile_id,timing) if timing is not None else action

    def timing_index(self,model_index,sequence_index):
        return self.timing_overrides.get((model_index,sequence_index),dict(self.models[model_index].timing_indexes).get(sequence_index))

    def effective_fps(self,model_index,sequence_index):
        return self.updates.get((model_index,sequence_index),
                               self.action(model_index,sequence_index,all_slots=True).fps)

    def timing_choices(self,model_index,sequence_index):
        from .action_rules import _timing, _timing_activity
        from .weapon_sequences import get_profile
        action=self.baseline_action(model_index,sequence_index,all_slots=True)
        profile=get_profile(self.models[model_index].profile_id)
        choices=[]
        for slot in profile.slots:
            if slot.unused or _timing_activity(slot.activity,profile)!=_timing_activity(action.activity,profile):continue
            duration,_,_,_=_timing(action.activity,profile,slot,False)
            if duration is not None:choices.append((slot.index,slot.name,duration))
        return tuple(choices)

    def swap_sequences(self,model_index,first,second,*,preview=None,timing_slots=None):
        from .action_swaps import validate_swap_pair
        if first==second:raise ValueError('请选择两个不同的动作顺位。')
        a=self.action(model_index,first,all_slots=True)
        b=self.action(model_index,second,all_slots=True)
        validate_swap_pair(a,b,self.models[model_index].profile_id)
        rates={first:self.effective_fps(model_index,second),second:self.effective_fps(model_index,first)}
        selected_timings={}
        if a.activity!=b.activity:
            durations={}
            for destination in (a,b):
                requested=(timing_slots or {}).get(destination.index)
                duration=destination.official_duration
                if requested is not None:
                    timed=with_timing_reference(destination,self.models[model_index].profile_id,requested)
                    duration=timed.official_duration
                    selected_timings[destination.index]=requested
                if duration is not None and math.isfinite(duration) and duration>0:
                    durations[destination.index]=duration
            # A missing timer is not a ban on moving the action. Keep that
            # destination's incoming FPS; never invent a limit from an unused
            # partner layer or an unrelated official animation.
            if durations and preview is None:
                from .action_preview import PreviewModel
                if model_index not in self._preview_models:
                    self._preview_models[model_index]=PreviewModel.load(self.models[model_index].model_path)
                preview=self._preview_models[model_index]
            for destination,donor in ((a,b),(b,a)):
                if destination.index not in durations:continue
                source=self.source_index(model_index,donor.index)
                source_fps=self.baseline_action(model_index,source,all_slots=True).fps
                timeline=preview.timeline(source,rate_scale=rates[destination.index]/source_fps)
                if timeline.duration<=0:raise ValueError('静止动作没有可匹配官方时长的播放区间。')
                rates[destination.index]=scaled_fps(rates[destination.index],timeline.duration,durations[destination.index])
        # Validate every result before changing any draft, including the UI range.
        if any(not 0<round_fps(rate)<=100000 for rate in rates.values()):
            raise ValueError('调换后 FPS 超出可编辑范围。')
        sources={first:self.source_index(model_index,second),second:self.source_index(model_index,first)}
        for destination,source in sources.items():
            key=(model_index,destination)
            if destination==source:self.assignments.pop(key,None)
            else:self.assignments[key]=source
            self.updates.pop(key,None)
        for destination,rate in rates.items():self._set_fps(model_index,destination,rate,all_slots=True)
        for destination,timing in selected_timings.items():
            key=(model_index,destination)
            if timing==dict(self.models[model_index].timing_indexes).get(destination):self.timing_overrides.pop(key,None)
            else:self.timing_overrides[key]=timing

    def set_fps(self, model_index, sequence_index, fps):
        return self._set_fps(model_index,sequence_index,fps)

    def _set_fps(self, model_index, sequence_index, fps, *,all_slots=False):
        action = self.action(model_index, sequence_index,all_slots=all_slots)
        if isinstance(fps, bool):raise ValueError('FPS 必须是有限正数。')
        try:rate = float(fps)
        except (TypeError, ValueError, OverflowError) as error:raise ValueError('FPS 无效。') from error
        if not math.isfinite(rate) or not 0 < rate <= 100000:
            raise ValueError('FPS 必须在 0 到 100000 之间且大于 0。')
        rate=round_fps(fps)
        key = (model_index, sequence_index)
        if rate == round_fps(action.fps):self.updates.pop(key, None)
        else:self.updates[key] = rate

    @property
    def changes(self):
        keys=set(self.updates)|set(self.assignments)|set(self.timing_overrides)
        return tuple(FpsChange(i,self.models[i],self.action(i,index,all_slots=True),
                              self.baseline_action(i,index,all_slots=True).fps,round_fps(self.effective_fps(i,index)))
                     for i,index in sorted(keys))

    def discard(self):
        self.updates.clear()
        self.assignments.clear()
        self.timing_overrides.clear()

    def save(self, **kwargs):
        from .action_save import save_session
        return save_session(self, **kwargs)
