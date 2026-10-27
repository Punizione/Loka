"""Official firearm slots and Activity-based planning; source SMDs stay unchanged."""
from dataclasses import dataclass, replace
from functools import lru_cache
from fractions import Fraction
import json
import math
import re
from pathlib import Path
from typing import Iterable
from .weapon_ads import is_ads_or_mixed_activity
from .fps_values import round_fps, scaled_fps


@dataclass(frozen=True)
class OfficialSlot:
    index: int
    name: str
    activity: str
    semantic: str
    layer: bool
    hidden: bool
    loop: bool
    unused: bool
    end_frame: int | None
    frame_count: int | None
    fps: float | None
    flexible: bool


@dataclass(frozen=True)
class WeaponProfile:
    id: str
    slots: tuple[OfficialSlot, ...]


@lru_cache(maxsize=1)
def _profiles():
    data=json.loads((Path(__file__).parent/'data/weapon_sequences.json').read_text(encoding='utf-8'))
    return {key:WeaponProfile(key,tuple(OfficialSlot(**s) for s in value['sequences']))
            for key,value in data['profiles'].items()}


def get_profile(id: str) -> WeaponProfile:
    try:return _profiles()[id]
    except KeyError as error:raise ValueError(f'没有官方序列配置: {id}') from error


@dataclass(frozen=True)
class SourceAnimation:
    name: str
    frame_count: int
    fps: float
    delta: bool = False


@dataclass(frozen=True)
class SourceSequence:
    name: str
    activity: str
    frame_count: int
    fps: float
    loop: bool = False
    animation_references: tuple[SourceAnimation, ...] = ()
    # Label before QC name repair; playback roles must survive a renamed symbol.
    original_name: str | None = None
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedSequence:
    name: str
    activity: str
    source: SourceSequence | None
    fps: float
    numframes: int | None
    placeholder: bool
    official_index: int | None
    inferred_source_activity: str | None = None
    source_layer: bool | None = None
    source_visible: bool | None = None
    source_reference_index: int | None = None
    idle_fallback: bool = False
    # Scale each original reference's effective FPS, independent of its length.
    fps_scale: float | None = None
    # Preserve actual authored reference rates for the six helper semantics.
    preserve_source_fps: bool = False
    # Exact original official timing ratio; fps_scale is a reporting float only.
    fps_scale_ratio: tuple[int, int] | None = None
    # Ordered source clips and native layer helpers for a composed reload.
    reload_parts: tuple[SourceSequence, ...] = ()
    reload_helpers: tuple[str, ...] = ()
    reload_helper: bool = False
    source_sequence_map: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SequencePlan:
    sequences: tuple[PlannedSequence, ...]
    warnings: tuple[str, ...]
    missing_actions: tuple[str, ...]
    excluded_sequences: tuple[str, ...] = ()
    keep_ads: bool = True
    excluded_fire_sequences: tuple[str, ...] = ()


_ACTIONS={
    'IDLE':'idle','DEPLOY':'deploy','PRIMARYATTACK':'fire','SHOOT':'fire',
    'SECONDARYATTACK':'fire_left','RELOAD':'reload','RELOAD_EMPTY':'reload_empty',
    'RELOAD_LOOP':'reload_loop','RELOAD_END':'reload_end','MELEE':'melee',
    'DRYFIRE':'fire_last','DRYFIRE_LEFT':'fire_last_left','IDLE_LOWERED':'dry_idle',
    'HELPINGHAND_EXTEND':'helping_hand_extend','HELPINGHAND_LOOP':'helping_hand_loop',
    'HELPINGHAND_RETRACT':'helping_hand_retract','ITEMPICKUP_EXTEND':'item_extend',
    'ITEMPICKUP_LOOP':'item_loop','ITEMPICKUP_RETRACT':'item_retract',
}

_IDLE_HELPER_ACTIONS=frozenset(('helping_hand_extend','helping_hand_loop','helping_hand_retract',
                               'item_extend','item_loop','item_retract'))


def _helper_idle_motion(source,slot):
    """Borrow one absolute idle animation, rather than the idle blend sequence."""
    if source is None:return None,None
    references=source.animation_references
    if not references:return replace(source,loop=slot.loop),0
    candidates=[i for i,a in enumerate(references) if not a.delta]
    if not candidates:return None,None
    named=[i for i in candidates if re.search(r'(?:^|[/_.\\])idle(?:$|[_.\\0-9])',references[i].name.lower().lstrip('@'))]
    index=(named or candidates)[0];animation=references[index]
    if animation.frame_count<1 or not math.isfinite(animation.fps) or animation.fps<=0:
        raise ValueError('idle 来源动画帧数/FPS 无效: '+animation.name)
    return replace(source,frame_count=animation.frame_count,fps=animation.fps,loop=slot.loop),index


def _misassigned_idle_raw(source, profile):
    # Only a named, officially Activity-free raw idle justifies overriding an
    # explicit ordinary Activity. Position alone cannot distinguish a renamed
    # real idle or an authored alternative; ADS/MIXED keep their own policy.
    if not source.activity.upper().startswith('ACT_VM_'):
        return None
    return next((s for s in profile.slots if s.name.lower()==source.name.lower()
                 and s.semantic=='idle_raw' and not s.activity), None)


def _idle_raw_correction_warnings(sources, profile):
    return [f'修正官方无 Activity 的原始 idle {s.name}: 移除误加的 {s.activity}。'
            for s in sources if _misassigned_idle_raw(s, profile) is not None]


def _classify(source, profile, index):
    if profile.id=='hunting_rifle' and index in (12,13):
        return 'reload_empty',index==13,True
    from .weapon_catalog import MELEE_IDS
    if profile.id in MELEE_IDS:
        from .weapon_melee_sequences import classify_melee
        return classify_melee(source,profile,index)
    raw=_misassigned_idle_raw(source,profile)
    if raw is not None:return raw.semantic,raw.layer,raw.unused
    if not source.activity:
        helper=next((s for s in profile.slots if s.name.lower()==source.name.lower()
                     and s.semantic in _IDLE_HELPER_ACTIONS and not s.activity),None)
        if helper:return helper.semantic,helper.layer,helper.unused
        if index>=len(profile.slots):return '',False,True
        slot=profile.slots[index]
        return slot.semantic,slot.layer,slot.unused
    named=next((s for s in profile.slots if s.name.lower()==source.name.lower()),None)
    if named and named.activity.upper()==source.activity.upper():
        return named.semantic,named.layer,named.unused
    activity=source.activity.upper()
    unused=next((s for s in profile.slots if s.unused and s.activity and s.activity==activity),None)
    if unused:return unused.semantic,unused.layer,True
    layer=activity.endswith('_LAYER')
    ads=re.match(r'^(?:MIXED_)?ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_(.+)$',activity)
    if ads:
        a=ads[1]
        layer=True
    else:a=activity.removeprefix('MIXED_').removeprefix('ACT_VM_').replace('_SNIPER','')
    if a.endswith('_LAYER'):a=a[:-6]
    return _ACTIONS.get(a,''),layer,False


def _extra_activity(activity):
    return activity.upper().startswith(('ACT_PRIMARY_VM_','ACT_SECONDARY_VM_','ACT_TERTIARY_VM_',
                                'ACT_FOURTH_VM_','MIXED_ACT_'))


def _dual_pistol_fire_alternative(source,target,action,layer):
    """Keep surplus shots playable as primary fire in their original state."""
    if action not in ('fire','fire_left'):return None
    activity=source.activity.upper()
    domain=re.match(r'^(?:MIXED_)?ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_',activity)
    if domain:layer=True
    compatible=next((slot for slot in target.slots if slot.semantic=='fire'
                     and slot.layer==layer and not slot.unused),None)
    if compatible is None:return None
    if domain:activity=domain[0]+'PRIMARYATTACK'
    else:activity=('MIXED_' if activity.startswith('MIXED_ACT_VM_') else '')+compatible.activity
    return activity,compatible,layer


def _timing(source,slot):
    if source is None:return slot.fps or 30.0,None
    if source.loop:return source.fps,None
    timed=(slot.end_frame is not None and slot.end_frame>0 and slot.fps is not None
           and not slot.flexible and slot.semantic!='look_poses')
    if not timed:return source.fps,None
    if source.frame_count==1:return source.fps,None
    # End Frame is zero-based; duration is end_frame / FPS, not frame_count / FPS.
    return Fraction(source.frame_count-1)*Fraction(str(slot.fps))/Fraction(str(slot.end_frame)),None


def target_timing_slot(profile,slot):
    # Hunting-rifle empty slots have no independent official duration. When
    # populated with a real donor, use its ordinary reload timer for both slots.
    if profile.id=='hunting_rifle' and slot.index in (12,13):
        timer=next(s for s in profile.slots if s.semantic=='reload' and not s.layer and not s.unused)
        return replace(slot,unused=False,end_frame=timer.end_frame,fps=timer.fps,flexible=timer.flexible)
    return slot


def _static_timing_warning(source,slot):
    if source and source.frame_count==1 and not source.loop and slot.end_frame and slot.end_frame>0 and slot.fps and not slot.flexible and slot.semantic!='look_poses':
        return f'{slot.name} 来源 {source.name} 为单帧，无法用 FPS 表示目标时长；保留单帧和原 FPS。'
    return None


def _timer_slot(slot,profile):
    """A usable paired layer owns visible events; viewer-hidden is not semantics."""
    if slot.layer or slot.index+1>=len(profile.slots):return False
    partner=profile.slots[slot.index+1]
    return partner.layer and (not partner.unused or profile.id=='hunting_rifle' and partner.index==13) and partner.semantic==slot.semantic


def plan_sequences(sources: Iterable[SourceSequence], source_id: str, target_id: str, *, keep_ads=True, fps_match=False, exclude_inspect_fps=False, reload_loop_count=1) -> SequencePlan:
    if type(reload_loop_count) is not int or reload_loop_count<0:
        raise ValueError('换弹 Loop 次数必须为非负整数')
    sources=tuple(sources)
    plan=_plan_sequences(sources,source_id,target_id,keep_ads=keep_ads)
    from .weapon_reload import compose_reload_plan
    plan=compose_reload_plan(plan,sources,source_id,target_id,reload_loop_count)
    source_profile=get_profile(source_id);target_profile=get_profile(target_id)
    indexes={s.name.lower():i for i,s in enumerate(sources)}
    rows=[];warnings=list(plan.warnings)
    for row in plan.sequences:
        slot=target_profile.slots[row.official_index] if row.official_index is not None else None
        source=row.source
        semantic=(slot.semantic if slot else _ACTIONS.get(
            _normalized_timing_activity(row.activity or row.inferred_source_activity or '').removeprefix('ACT_VM_').removesuffix('_LAYER'),''))
        if exclude_inspect_fps and source is not None and semantic in _IDLE_HELPER_ACTIONS:
            rows.append(replace(row,fps=round_fps(source.fps),fps_scale=None,preserve_source_fps=True));continue
        if row.reload_parts or not fps_match or 'LAYER' not in row.activity.upper():
            rows.append(replace(row,fps=round_fps(row.fps)));continue
        reason=None;source=row.source
        if source is None:reason='占位动作'
        elif source.loop:reason='循环动作'
        elif source.frame_count<=1:reason='单帧动作'
        else:
            source_slot=_official_timing_slot(source,source_profile,indexes.get(source.name.lower(),0))
            target_slot=(target_profile.slots[row.official_index] if row.official_index is not None else
                         _official_timing_slot(replace(source,name='__loka_target',original_name=None,
                                                       activity=row.activity),target_profile,0))
            source_time=_official_timer_fraction(source_slot,source_profile)
            target_time=_official_timer_fraction(target_slot,target_profile)
            if source_time is None:reason='缺少可靠的源官方计时基准'
            elif target_time is None:reason='缺少可靠的目标官方计时基准'
            else:
                scale=source_time/target_time
                ratio=(scale.numerator,scale.denominator)
                row=replace(row,fps=scaled_fps(source.fps,*ratio),fps_scale=float(scale),fps_scale_ratio=ratio)
        if reason:warnings.append(f'FPS匹配 {row.name}: {reason}，回退原换算规则。')
        rows.append(replace(row,fps=round_fps(row.fps)))
    return replace(plan,sequences=tuple(rows),warnings=tuple(warnings))


def _normalized_timing_activity(activity):
    activity=activity.upper().removeprefix('MIXED_')
    for prefix in ('ACT_PRIMARY_VM_','ACT_SECONDARY_VM_','ACT_TERTIARY_VM_','ACT_FOURTH_VM_'):
        if activity.startswith(prefix):activity='ACT_VM_'+activity[len(prefix):];break
    return activity.replace('_SNIPER','').replace('ACT_VM_SHOOT','ACT_VM_PRIMARYATTACK')


def _official_timing_slot(source,profile,index):
    labels={source.name.lower(),(source.original_name or source.name).lower()}
    activity=_normalized_timing_activity(source.activity)
    named=next((s for s in profile.slots if s.name.lower() in labels and
                (not activity or _normalized_timing_activity(s.activity)==activity)),None)
    if named:return named
    if not activity:
        return profile.slots[index] if index<len(profile.slots) else None
    candidates=[s for s in profile.slots if _normalized_timing_activity(s.activity)==activity and not s.unused]
    if not candidates:return None
    # Repeated Activity slots are safe only when their official timers agree.
    durations={_official_timer_fraction(s,profile) for s in candidates}
    return candidates[0] if len(durations)==1 and None not in durations else None


def _official_timer_duration(slot,profile):
    duration=_official_timer_fraction(slot,profile)
    return float(duration) if duration is not None else None


def _official_timer_fraction(slot,profile):
    if slot is None:return None
    if slot.layer:
        # The preceding non-layer owns timing, even if a melee pair has different semantics.
        if slot.index<1:return None
        slot=profile.slots[slot.index-1]
        if slot.layer:return None
    if slot.unused or slot.loop or slot.flexible or not slot.end_frame or not slot.fps:return None
    duration=Fraction(str(slot.end_frame))/Fraction(str(slot.fps))
    return duration if math.isfinite(float(duration)) and duration>0 else None


def _plan_sequences(sources: Iterable[SourceSequence], source_id: str, target_id: str, *, keep_ads=True) -> SequencePlan:
    source_profile=get_profile(source_id);target=get_profile(target_id)
    from .weapon_catalog import MELEE_IDS
    if source_id in MELEE_IDS or target_id in MELEE_IDS:
        if source_id not in MELEE_IDS or target_id not in MELEE_IDS:raise ValueError('近战与枪械不能混合规划')
        from .weapon_melee_sequences import plan_melee_sequences
        return plan_melee_sequences(sources,source_profile,target,keep_ads=keep_ads)
    sources=tuple(sources)
    referenced={name.lower() for s in sources for name in s.dependencies}
    official_names={slot.name.lower() for slot in source_profile.slots}
    # Explicitly referenced, Activity-free author symbols are helpers even when
    # inserted among official slots. Known official names keep positional inference.
    helpers={i for i,s in enumerate(sources) if not s.activity and s.name.lower() in referenced
             and s.name.lower() not in official_names}
    excluded=helpers|{i for i,s in enumerate(sources) if not s.activity and i>=len(source_profile.slots)}
    ads_excluded={i for i,s in enumerate(sources) if not keep_ads and is_ads_or_mixed_activity(s.activity)}
    names=set()
    for i,s in enumerate(sources):
        if i in excluded or i in ads_excluded:continue
        if not s.name or any(c in s.name for c in '\r\n\x00"') or s.name.lower() in names:
            raise ValueError(f'无效或重复的 sequence 名称: {s.name!r}')
        if s.frame_count<1 or not math.isfinite(s.fps) or s.fps<=0:
            raise ValueError(f'sequence 帧数/FPS 无效: {s.name}')
        names.add(s.name.lower())
    classified=[('',False,True) if i in helpers else _classify(s,source_profile,i)
                for i,s in enumerate(sources)]
    dual_to_other=source_id=='dual_pistol' and target_id!='dual_pistol'
    if dual_to_other:
        # Explicit last-shot Activities are layers even without a _LAYER suffix;
        # Activity-free official last-shot timers retain their base role.
        timers={slot.name.lower():(slot.semantic,slot.layer,slot.unused) for slot in source_profile.slots
                if slot.semantic in ('fire_last','fire_last_left') and not slot.activity}
        for i,source in enumerate(sources):
            action,layer,unused=classified[i]
            if not source.activity:classified[i]=timers.get(source.name.lower(),classified[i])
            elif action in ('fire_last','fire_last_left'):classified[i]=(action,True,unused)
    used=set();result=[];missing=[]
    warnings=[f'排除无 Activity 的非官方 sequence（来源序号 {i}）: {sources[i].name}'
              for i in sorted(excluded)]
    warnings.extend(_idle_raw_correction_warnings(sources,source_profile))
    official_sources={i for i,s in enumerate(sources) if
        (not s.activity and i<len(source_profile.slots))
        or _misassigned_idle_raw(s,source_profile) is not None or any(
            p.name.lower()==s.name.lower() and p.activity.upper()==s.activity.upper()
            for p in source_profile.slots)}
    def common_motion(action,prefer_layer):
        for layer in (prefer_layer,not prefer_layer):
            candidates=[i for i,(a,l,u) in enumerate(classified) if i not in excluded
                        and i not in ads_excluded and a==action and l==layer and not u
                        and not _extra_activity(sources[i].activity)]
            if candidates:
                return next((i for i in candidates if i in official_sources),candidates[0])
        return None
    dual_reuse=target_id=='dual_pistol' and source_id!='dual_pistol'
    pistol_target=target_id in ('pistol','dual_pistol','pistol_magnum')
    reused={action:common_motion(action,action!='idle') for action in ('fire','idle','reload')}
    fallback={'reload_empty':'reload','fire_last':'fire','fire_left':'fire',
              'fire_last_left':'fire','dry_idle':'idle','idle_raw':'idle'}
    for slot in target.slots:
        source=None;chosen=None;idle_fallback=False;reference_index=None
        reuse_action=('fire' if slot.semantic in ('fire','fire_left','fire_last','fire_last_left')
                      else 'idle' if slot.name in ('dry_right_idle','dry_right_idle_layer') else None) if dual_reuse else None
        # Missing pistol specials share one physical ordinary motion across the
        # timer/layer pair. Authored dedicated specials and extra motions survive.
        if reuse_action is None and ((pistol_target and slot.semantic=='reload_empty')
                                    or (target_id=='pistol' and slot.semantic=='fire_last')):
            if common_motion(slot.semantic,slot.layer) is None:
                reuse_action=fallback[slot.semantic]
        if reuse_action:
            chosen=reused[reuse_action]
            if chosen is not None:
                source=sources[chosen]
                if classified[chosen][:2]==(slot.semantic,slot.layer):used.add(chosen)
                if reuse_action=='idle':
                    idle,reference_index=_helper_idle_motion(source,slot)
                    if idle is not None:source=idle
        elif target_id=='hunting_rifle' and slot.index in (12,13):
            chosen=common_motion('reload_empty',slot.layer)
            if chosen is not None:
                source=sources[chosen];used.add(chosen)
        elif not slot.unused:
            actions=[slot.semantic]
            if slot.semantic in fallback:actions.append(fallback[slot.semantic])
            for action in actions:
                candidates=[i for i,(a,l,u) in enumerate(classified) if i not in ads_excluded and a==action and l==slot.layer and not u
                            and not _extra_activity(sources[i].activity)]
                if not candidates and slot.semantic not in _IDLE_HELPER_ACTIONS:
                    candidates=[i for i,(a,l,u) in enumerate(classified) if i not in ads_excluded and a==action and not u
                                and not _extra_activity(sources[i].activity)]
                if candidates:
                    core=[i for i in candidates if i in official_sources]
                    pool=core or candidates
                    # Repeated official slots reuse a base; authored alternatives stay at the tail.
                    preferred=next((i for i in core if sources[i].name.lower()==slot.name.lower()),pool[0])
                    chosen=preferred;source=sources[chosen]
                    # Borrowing a visible layer for a timer or a different action does not consume it.
                    if classified[chosen][:2]==(slot.semantic,slot.layer):used.add(chosen)
                    break
        if source is None and not slot.unused and slot.semantic in _IDLE_HELPER_ACTIONS:
            chosen=reused['idle']
            source,reference_index=_helper_idle_motion(sources[chosen] if chosen is not None else None,slot)
            idle_fallback=source is not None
            if idle_fallback:warnings.append(f'{slot.name} 缺少辅助动作，复用 {source.name} 的 idle 动画引用 {reference_index}。')
        if source is None and not slot.unused and slot.semantic not in ('look_poses','idle_raw'):
            missing.append(slot.name)
        fps,numframes=_timing(source,target_timing_slot(target,slot) if source is not None else slot)
        notice=_static_timing_warning(source,slot)
        if notice:warnings.append(notice)
        inferred=source_profile.slots[chosen].activity if source is not None and not source.activity and chosen<len(source_profile.slots) else None
        activity=slot.activity
        result.append(PlannedSequence(slot.name,activity,source,fps,numframes,
                                      source is None,slot.index,inferred,
                                      classified[chosen][1] if source is not None else None,
                                      source_reference_index=reference_index if reference_index is not None else 0 if source is not None and reuse_action else None,
                                      idle_fallback=idle_fallback))
    fire_excluded={i for i,(action,layer,unused) in enumerate(classified)
                   if dual_to_other and action in ('fire_last','fire_last_left')
                   and i not in used and i not in ads_excluded
                   and not any(slot.semantic==action and slot.layer==layer and not slot.unused for slot in target.slots)}
    reserved={s.name.lower() for s in result}
    for i,s in enumerate(sources):
        action,layer,unused=classified[i]
        if i in used or unused or i in ads_excluded or i in fire_excluded:continue
        compatible=next((slot for slot in target.slots if slot.semantic==action
                         and slot.layer==layer and not slot.unused),None)
        alternative=(_dual_pistol_fire_alternative(s,target,action,layer)
                     if dual_to_other else None)
        if alternative:activity,compatible,layer=alternative
        # Authored alternatives keep their full motion, even when the corresponding official slot is a timer.
        fps,numframes=_timing(s,replace(compatible,hidden=False)) if compatible else (s.fps,None)
        static_notice=_static_timing_warning(s,compatible) if compatible else None
        if static_notice:warnings.append(static_notice)
        name=s.name
        if name.lower() in reserved:
            stem=name+'_variant';n=1
            while f'{stem}_{n}'.lower() in reserved:n+=1
            name=f'{stem}_{n}'
        reserved.add(name.lower())
        if not alternative:
            activity=compatible.activity if s.activity and compatible and not _extra_activity(s.activity) else s.activity
        if _misassigned_idle_raw(s,source_profile) is not None:activity=''
        inferred=source_profile.slots[i].activity if not s.activity and i<len(source_profile.slots) else None
        result.append(PlannedSequence(name,activity,
                                     s,fps,numframes,False,None,inferred,layer))
        if not compatible and not s.loop:warnings.append(f'额外动作 {s.name} 没有目标官方时间基准，保留原 FPS。')
    if keep_ads and pistol_target:
        # Only authored ADS roots donate motion, within their own Activity domain.
        # Append after every authored alternative so neither native slots nor
        # dedicated special actions are replaced by a generated fallback.
        authored=tuple(result)
        activities={row.activity.upper() for row in authored}
        donors={}
        for row in authored:
            match=re.fullmatch(r'((?:MIXED_)?ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_|MIXED_ACT_VM_)'
                               r'(PRIMARYATTACK(?:_LAYER)?|SHOOT_LAYER|RELOAD)',row.activity.upper())
            if match and row.source is not None:
                domain,action=match.groups()
                donors.setdefault((domain,'reload' if action=='RELOAD' else 'fire'),row)
        for (domain,action),donor in donors.items():
            suffixes=('RELOAD_EMPTY',) if action=='reload' else ('SECONDARYATTACK','DRYFIRE','DRYFIRE_LEFT')
            for suffix in suffixes:
                activity=domain+suffix
                if activity in activities:continue
                role=_classify(replace(donor.source,name='__loka_ads_role',activity=activity),target,0)
                compatible=next((slot for slot in target.slots if
                                 (slot.semantic,slot.layer)==role[:2] and not slot.unused),None)
                if compatible is None:continue
                name=donor.name+'_'+suffix.lower();stem=name;n=1
                while name.lower() in reserved:
                    name=f'{stem}_{n}';n+=1
                reserved.add(name.lower());activities.add(activity)
                # A reload donor has already been timed for RELOAD. Re-evaluate
                # its original motion for RELOAD_EMPTY's distinct target timer.
                fps,numframes=_timing(donor.source,replace(compatible,hidden=False))
                result.append(replace(donor,name=name,activity=activity,fps=fps,numframes=numframes))
    # Only roots that actually survived planning own dependencies. In particular,
    # removing an ADS root must not keep its private pose helpers alive.
    available={s.name.lower():i for i,s in reversed(tuple(enumerate(sources)))}
    represented={row.source.name.lower() for row in result if row.source is not None}
    pending=[row.source for row in result if row.source is not None]
    visited=set();retained_dependencies=set()
    while pending:
        source=pending.pop()
        key=source.name.lower()
        if key in visited:continue
        visited.add(key)
        for dependency in source.dependencies:
            index=available.get(dependency.lower())
            if index is None:raise ValueError(f'sequence {source.name} 引用了不存在的 sequence: {dependency}')
            if index in ads_excluded or index in fire_excluded:continue
            child=sources[index];pending.append(child)
            if child.name.lower() in represented:continue
            if not child.name or any(c in child.name for c in '\r\n\x00"'):
                raise ValueError(f'无效的依赖 sequence 名称: {child.name!r}')
            if child.frame_count<1 or not math.isfinite(child.fps) or child.fps<=0:
                raise ValueError(f'sequence 帧数/FPS 无效: {child.name}')
            name=child.name
            if name.lower() in reserved:
                stem=name+'_variant';n=1
                while f'{stem}_{n}'.lower() in reserved:n+=1
                name=f'{stem}_{n}'
            reserved.add(name.lower());represented.add(child.name.lower())
            retained_dependencies.add(index)
            # A referenced helper is a symbol, never a candidate for an official
            # action or an excuse to infer Activity from its source position.
            result.append(PlannedSequence(name,child.activity,child,child.fps,None,False,None,
                                          source_layer=False))
    if retained_dependencies:
        warnings=[notice for notice in warnings if notice not in {
            f'排除无 Activity 的非官方 sequence（来源序号 {i}）: {sources[i].name}'
            for i in retained_dependencies}]
    return SequencePlan(tuple(result),tuple(warnings),tuple(missing),
                        tuple(sources[i].name for i in sorted(ads_excluded)),keep_ads,
                        tuple(sources[i].name for i in sorted(fire_excluded)))


def validate_sequence_layout(sequences, target_id: str) -> list[str]:
    slots=get_profile(target_id).slots;sequences=tuple(sequences);issues=[]
    if len(sequences)<len(slots):issues.append('缺少官方 sequence 槽位')
    for expected,actual in zip(slots,sequences):
        if expected.name!=actual.name or expected.activity!=actual.activity:
            issues.append(f'第 {expected.index} 个 sequence 应为 {expected.name} / {expected.activity}')
    seen=set()
    for i,s in enumerate(sequences):
        if not math.isfinite(s.fps) or s.fps<=0:issues.append(f'sequence FPS 无效: {s.name}')
        if any(c in s.name for c in '\r\n\x00"'):issues.append(f'sequence 名称无效: {s.name!r}')
        if s.name.lower() in seen:issues.append(f'sequence 名称重复: {s.name}')
        seen.add(s.name.lower())
        if i>=len(slots) and s.name.lower() in {p.name.lower() for p in slots}:
            issues.append(f'随机动作与官方名称冲突: {s.name}')
    return issues
