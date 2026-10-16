"""Official firearm slots and Activity-based planning; source SMDs stay unchanged."""
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import math
import re
from pathlib import Path
from typing import Iterable
from .weapon_ads import is_ads_or_mixed_activity


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


@dataclass(frozen=True)
class SequencePlan:
    sequences: tuple[PlannedSequence, ...]
    warnings: tuple[str, ...]
    missing_actions: tuple[str, ...]
    excluded_sequences: tuple[str, ...] = ()
    keep_ads: bool = True


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
    return activity.startswith(('ACT_PRIMARY_VM_','ACT_SECONDARY_VM_','ACT_TERTIARY_VM_',
                                'ACT_FOURTH_VM_','MIXED_ACT_'))


def _timing(source,slot):
    if source is None:return slot.fps or 30.0,None
    if source.loop:return source.fps,None
    timed=(slot.end_frame is not None and slot.end_frame>0 and slot.fps is not None
           and not slot.flexible and slot.semantic!='look_poses')
    if not timed:return source.fps,None
    if source.frame_count==1:return source.fps,None
    # End Frame is zero-based; duration is end_frame / FPS, not frame_count / FPS.
    return (source.frame_count-1)*slot.fps/slot.end_frame,None


def _static_timing_warning(source,slot):
    if source and source.frame_count==1 and not source.loop and slot.end_frame and slot.end_frame>0 and slot.fps and not slot.flexible and slot.semantic!='look_poses':
        return f'{slot.name} 来源 {source.name} 为单帧，无法用 FPS 表示目标时长；保留单帧和原 FPS。'
    return None


def _timer_slot(slot,profile):
    """A usable paired layer owns visible events; viewer-hidden is not semantics."""
    if slot.layer or slot.index+1>=len(profile.slots):return False
    partner=profile.slots[slot.index+1]
    return partner.layer and not partner.unused and partner.semantic==slot.semantic


def plan_sequences(sources: Iterable[SourceSequence], source_id: str, target_id: str, *, keep_ads=True) -> SequencePlan:
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
        fps,numframes=_timing(source,slot)
        notice=_static_timing_warning(source,slot)
        if notice:warnings.append(notice)
        inferred=source_profile.slots[chosen].activity if source is not None and not source.activity and chosen<len(source_profile.slots) else None
        activity=slot.activity
        result.append(PlannedSequence(slot.name,activity,source,fps,numframes,
                                      source is None,slot.index,inferred,
                                      classified[chosen][1] if source is not None else None,
                                      source_reference_index=reference_index if idle_fallback else 0 if source is not None and reuse_action else None,
                                      idle_fallback=idle_fallback))
    reserved={s.name.lower() for s in result}
    for i,s in enumerate(sources):
        action,layer,unused=classified[i]
        if i in used or unused or i in ads_excluded:continue
        compatible=next((slot for slot in target.slots if slot.semantic==action
                         and slot.layer==layer and not slot.unused),None)
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
        activity=compatible.activity if s.activity and compatible and not _extra_activity(s.activity) else s.activity
        if _misassigned_idle_raw(s,source_profile) is not None:activity=''
        inferred=source_profile.slots[i].activity if not s.activity and i<len(source_profile.slots) else None
        result.append(PlannedSequence(name,activity,
                                     s,fps,numframes,False,None,inferred,layer))
        if not compatible and not s.loop:warnings.append(f'额外动作 {s.name} 没有目标官方时间基准，保留原 FPS。')
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
            if index in ads_excluded:continue
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
                        tuple(sources[i].name for i in sorted(ads_excluded)),keep_ads)


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
