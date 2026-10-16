"""Melee slot policy: visible HIT attacks, paired timers and recorded reuse."""
from dataclasses import dataclass,replace
import math,random
from .weapon_sequences import (PlannedSequence,SequencePlan,_timing,_extra_activity,_ACTIONS,
                               _misassigned_idle_raw,_idle_raw_correction_warnings)
from .weapon_sequences import _static_timing_warning
from .weapon_sequences import _IDLE_HELPER_ACTIONS,_helper_idle_motion
from .weapon_ads import is_ads_or_mixed_activity

_NORMAL_ATTACKS=frozenset(('attack','attack_center','attack_left','attack_right'))


@dataclass(frozen=True)
class _Role:
    semantic: str
    layer: bool
    unused: bool
    visible: bool
    official: bool
    group: int
    inferred: str | None = None


def _group(slots,index):
    if slots[index].layer and index and not slots[index-1].layer:return index-1
    return index


def _role(source,profile,index):
    slot=None
    labels={source.name.lower(),(source.original_name or source.name).lower()}
    if not source.activity:
        if index>=len(profile.slots):return _Role('',False,True,False,False,index)
        slot=next((s for s in profile.slots if s.name.lower() in labels
                   and s.semantic in _IDLE_HELPER_ACTIONS and not s.activity),profile.slots[index])
    else:
        slot=_misassigned_idle_raw(source,profile) or next(
            (s for s in profile.slots if s.name.lower() in labels
             and s.activity.upper()==source.activity.upper()),None)
    if slot:
        return _Role(slot.semantic,slot.layer,slot.unused,not slot.hidden and not slot.unused,
                     True,_group(profile.slots,slot.index),slot.activity if not source.activity else None)
    activity=source.activity.upper()
    # ADS/MIXED variants retain their Activity and do not fill ordinary slots.
    action=activity.removeprefix('MIXED_')
    for prefix in ('ACT_PRIMARY_VM_','ACT_SECONDARY_VM_','ACT_TERTIARY_VM_','ACT_FOURTH_VM_'):
        if action.startswith(prefix):action='ACT_VM_'+action[len(prefix):];break
    action=action.removeprefix('ACT_VM_');layer=action.endswith('_LAYER');action=action.removesuffix('_LAYER')
    semantic={'DRAW':'deploy','PRIMARYATTACK':'attack','SHOOT':'attack','HITCENTER':'attack_center',
        'HITLEFT':'attack_left','HITRIGHT':'attack_right','SWINGHARD':'attack_hard',
        'SECONDARYATTACK':'push','FIDGET':'fidget'}.get(action,_ACTIONS.get(action,''))
    unused=layer and action in ('HITCENTER','HITLEFT','HITRIGHT')
    hidden=action in ('PRIMARYATTACK','SECONDARYATTACK') and not layer
    activity_slots=[s for s in profile.slots if s.activity.upper()==activity]
    if activity_slots:
        unused=all(s.unused for s in activity_slots)
        hidden=all(s.hidden for s in activity_slots)
    if _extra_activity(activity):unused=False
    return _Role(semantic,layer,unused,not hidden and not unused,False,len(profile.slots)+index)


def classify_melee(source,profile,index):
    role=_role(source,profile,index)
    return role.semantic,role.layer,role.unused


def plan_melee_sequences(sources,source_profile,target_profile,*,keep_ads=True,rng=None):
    sources=tuple(sources);rng=rng or random.SystemRandom();roles=[_role(s,source_profile,i) for i,s in enumerate(sources)]
    excluded={i for i,s in enumerate(sources) if not keep_ads and is_ads_or_mixed_activity(s.activity)}
    names=set();warnings=_idle_raw_correction_warnings(sources,source_profile)
    for i,source in enumerate(sources):
        if i in excluded:continue
        if not source.activity and i>=len(source_profile.slots):
            warnings.append(f'排除无 Activity 的非官方 sequence（来源序号 {i}）: {source.name}');continue
        if not source.name or any(c in source.name for c in '\r\n\x00"') or source.name.lower() in names:
            raise ValueError('无效或重复的 sequence 名称: '+source.name)
        if source.frame_count<1 or not math.isfinite(source.fps) or source.fps<=0:raise ValueError('sequence 帧数/FPS 无效: '+source.name)
        names.add(source.name.lower())
    eligible=[i for i,r in enumerate(roles) if i not in excluded and not r.unused and not _extra_activity(sources[i].activity)]
    used_groups=set();mapped_sources=set();selected={};rows=[];missing=[]

    # Reserve exact direction matches across the whole target before random
    # allocation can consume the only left/right/center motion needed later.
    attack_slots={_group(target_profile.slots,s.index):s for s in reversed(target_profile.slots)
                  if not s.unused and s.semantic.startswith('attack') and s.semantic!='attack_hard'}
    attack_slots=dict(sorted(attack_slots.items()))
    pool=[i for i in eligible if roles[i].visible and roles[i].semantic.startswith('attack') and roles[i].semantic!='attack_hard']
    core=[i for i in pool if roles[i].official]
    core_semantics={roles[i].semantic for i in core}
    pool=core+[i for i in pool if not roles[i].official and roles[i].semantic not in core_semantics]
    for group,slot in attack_slots.items():
        matching=[i for i in pool if roles[i].group not in used_groups and roles[i].semantic==slot.semantic]
        if slot.semantic=='attack' or not matching:continue
        chosen=next((i for i in matching if sources[i].name.lower()==slot.name.lower()),matching[0])
        selected[group]=chosen;used_groups.add(roles[chosen].group)
    for group,slot in attack_slots.items():
        if group in selected:continue
        available=[i for i in pool if roles[i].group not in used_groups]
        matching=[i for i in available if roles[i].semantic==slot.semantic
                  or roles[i].semantic=='attack' or slot.semantic=='attack']
        if matching:chosen=matching[0]
        elif available:
            chosen=rng.choice(available)
            warnings.append(f'{slot.name} 无对应方向，随机复用源攻击: {sources[chosen].name}')
        elif pool:
            chosen=rng.choice(pool)
            warnings.append(f'{slot.name} 随机复用源攻击: {sources[chosen].name}')
        else:chosen=None
        selected[group]=chosen
        if chosen is not None:used_groups.add(roles[chosen].group)

    def choose(slot):
        pool=[i for i in eligible if roles[i].semantic==slot.semantic]
        visible=[i for i in pool if roles[i].visible]
        needs_visible=slot.semantic in ('idle','deploy','push') or any(
            _group(target_profile.slots,s.index)==_group(target_profile.slots,slot.index)
            and not s.hidden and not s.unused for s in target_profile.slots)
        pool=visible if needs_visible else visible or pool
        if not pool and slot.semantic=='attack_hard':
            # Some weapons have no separate heavy attack. Target heavy slots
            # still need a playable pose, rather than a one-frame placeholder.
            pool=[i for i in eligible if roles[i].visible and roles[i].semantic.startswith('attack')
                  and roles[i].semantic!='attack_hard']
            if pool:warnings.append(f'{slot.name} 缺少重击动作，复用可播放的普通攻击。')
        if not pool and slot.semantic=='idle_raw':pool=[i for i in eligible if roles[i].semantic=='idle']
        core=[i for i in pool if roles[i].official];pool=core or pool
        if not pool:return None
        return next((i for i in pool if sources[i].name.lower()==slot.name.lower()),pool[0])

    for slot in target_profile.slots:
        chosen=None;group=_group(target_profile.slots,slot.index);idle_fallback=False;reference_index=None
        if not slot.unused:
            if slot.semantic in _IDLE_HELPER_ACTIONS:
                pool=[i for i in eligible if roles[i].semantic==slot.semantic and roles[i].layer==slot.layer]
                core=[i for i in pool if roles[i].official];pool=core or pool
                chosen=next((i for i in pool if sources[i].name.lower()==slot.name.lower()),pool[0] if pool else None)
            else:
                if group not in selected:selected[group]=choose(slot)
                chosen=selected[group]
        source=sources[chosen] if chosen is not None else None
        if source is None and not slot.unused and slot.semantic in _IDLE_HELPER_ACTIONS:
            idle_slot=next((s for s in target_profile.slots if s.semantic=='idle' and not s.unused),None)
            chosen=choose(idle_slot) if idle_slot else None
            source,reference_index=_helper_idle_motion(sources[chosen] if chosen is not None else None,slot)
            idle_fallback=source is not None
            if idle_fallback:warnings.append(f'{slot.name} 缺少辅助动作，复用 {source.name} 的 idle 动画引用 {reference_index}。')
        if source is None and not slot.unused:
            required=slot.semantic in ('idle','deploy','push') or (slot.semantic.startswith('attack') and slot.semantic!='attack_hard')
            if required:missing.append(slot.name)
            else:warnings.append(f'{slot.name} 缺少可选动作，保留官方静态占位槽。')
        if chosen is not None:
            used_groups.add(roles[chosen].group)
            # A full source sequence already represented by a target slot is
            # not an unused author variant, even when its label is non-stock.
            # Borrowing one idle reference for a helper does not consume it.
            if not idle_fallback:mapped_sources.add(chosen)
        fps,numframes=_timing(source,slot);role=roles[chosen] if chosen is not None else None
        notice=_static_timing_warning(source,slot)
        if notice:warnings.append(notice)
        rows.append(PlannedSequence(slot.name,slot.activity,
            source,fps,numframes,source is None,slot.index,role.inferred if role else None,
            role.layer if role else None,role.visible if role else None,
            source_reference_index=reference_index,idle_fallback=idle_fallback))
    reserved={r.name.lower() for r in rows}
    for i,source in enumerate(sources):
        role=roles[i]
        if i in excluded or i in mapped_sources or role.unused or (role.official and role.group in used_groups):continue
        compatible=next((s for s in target_profile.slots if s.semantic==role.semantic and s.layer==role.layer and not s.unused),None)
        if role.visible and role.semantic in _NORMAL_ATTACKS:
            playable=[s for s in target_profile.slots if s.semantic in _NORMAL_ATTACKS and not s.hidden and not s.unused]
            compatible=next((s for s in playable if s.semantic==role.semantic),playable[0] if playable else None)
        fps,numframes=_timing(source,replace(compatible,hidden=False)) if compatible else (source.fps,None)
        static_notice=_static_timing_warning(source,compatible) if compatible else None
        if static_notice:warnings.append(static_notice)
        name=source.name;counter=1
        while name.lower() in reserved:name=source.name+'_variant_'+str(counter);counter+=1
        reserved.add(name.lower())
        activity=compatible.activity if compatible and source.activity and not _extra_activity(source.activity) else source.activity
        if _misassigned_idle_raw(source,source_profile) is not None:activity=''
        rows.append(PlannedSequence(name,activity,source,fps,numframes,False,None,role.inferred,role.layer,role.visible))
        if compatible is None and not source.loop:warnings.append(f'额外动作 {source.name} 没有目标官方时间基准，保留原 FPS。')
    return SequencePlan(tuple(rows),tuple(warnings),tuple(missing),tuple(sources[i].name for i in sorted(excluded)),keep_ads)
