"""Melee to firearm mapping: attack groups, reload substitution, ADS remapping.

This module only decides which melee motion fills which firearm slot, and which
of the three allowed source edits (sequence order/name, fps, activity) is
applied. Everything else in the source QC stays authored.
"""
from __future__ import annotations
from dataclasses import dataclass,replace
import math
import re

from .weapon_sequences import _classify,_extra_activity,_timing

#
# HITCENTER > SHOOT_LAYER > HITLEFT > HITRIGHT > SWINGHARD.
_ATTACK_PRIORITY={'attack_center':1,'attack':2,'attack_left':3,'attack_right':4,'attack_hard':5}
_ATTACK_SEMANTICS=tuple(_ATTACK_PRIORITY)
# Attack playback layers always keep source FPS. Other layers follow official
# duration by default; the optional reload preservation is applied by the caller.
_PLAYBACK_ACTIVITIES=frozenset(('ACT_VM_PRIMARYATTACK_LAYER','ACT_VM_SHOOT_SNIPER_LAYER',
                                'ACT_VM_SECONDARYATTACK_LAYER'))
# A spared melee alternative may not keep its own Activity: the firearm family
#
_EXTRA_SEMANTICS={'attack':'fire','attack_center':'fire','attack_left':'fire',
                  'attack_right':'fire','attack_hard':'fire','push':'melee','deploy':'deploy',
                  # A push carried by the group override already speaks firearm roles.
                  'melee':'melee'}
# Roles the firearm profile cannot host a playback layer for: the official idle
#
_EXTRA_BLANK=frozenset(('idle','fidget'))
_EXTRA_HANDLED=frozenset(_EXTRA_SEMANTICS)|_EXTRA_BLANK
_ADS_PREFIX=re.compile(r'^(MIXED_ACT_VM_|(?:MIXED_)?ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_)'
                       r'([A-Z0-9_]+)$')
_ADS_ATTACK={'HITCENTER':'PRIMARYATTACK','HITLEFT':'PRIMARYATTACK','HITRIGHT':'PRIMARYATTACK',
             'SHOOT_LAYER':'PRIMARYATTACK','SECONDARYATTACK':'MELEE'}
_ADS_COLLISION_ORDER={'HITCENTER':1,'HITLEFT':2,'HITRIGHT':3,'SHOOT_LAYER':4}


@dataclass(frozen=True)
class MeleeFirearmMap:
    """Chosen melee motions per firearm role, as source indexes."""
    fire_timer: int | None = None
    fire_playback: int | None = None
    push_timer: int | None = None
    push_playback: int | None = None
    warnings: tuple[str, ...] = ()

    @property
    def overrides(self) -> dict[int, tuple[str, bool, bool]]:
        """Rewrite the chosen roles into firearm semantics and layer polarity."""
        result = {}
        if self.fire_timer is not None: result[self.fire_timer] = ('fire', False, False)
        if self.fire_playback is not None: result[self.fire_playback] = ('fire', True, False)
        if self.push_timer is not None: result[self.push_timer] = ('melee', False, False)
        if self.push_playback is not None: result[self.push_playback] = ('melee', True, False)
        return result


def is_attack_playback(slot) -> bool:
    """A layer slot whose official activity is an attack playback."""
    return bool(slot is not None and slot.layer
                and (slot.activity or '').upper() in _PLAYBACK_ACTIVITIES)


def extra_activity(role, target_profile):
    """Activity a spared melee action is rewritten to, or None to keep the default rule.

    An authored alternative is never an official slot, so it must borrow the
    target playback layer instead of the timer slot's Activity: the timer name
    (ACT_VM_DEPLOY_SNIPER, ACT_VM_PRIMARYATTACK) would claim the official
    timeline for motion that never was that action. An empty string means the
    target has no playback layer for this role, so the row keeps no Activity.
    """
    if role not in _EXTRA_HANDLED: return None
    if role in _EXTRA_BLANK: return ''
    semantic=_EXTRA_SEMANTICS[role]
    for slot in target_profile.slots:
        if slot.unused or not slot.layer or slot.semantic != semantic: continue
        activity=(slot.activity or '').strip()
        if activity: return activity
    return ''


def reload_source_index(classified, layer, *, eligible=None):
    """Index of the item pickup loop filling a reload slot, layer first."""
    for wanted in ((layer, not layer) if layer else (layer,)):
        for index, (action, item_layer, unused) in enumerate(classified):
            if eligible is not None and index not in eligible:continue
            if action == 'item_loop' and item_layer == wanted and not unused:
                return index
    return None


def reload_motion(source):
    """Reload substitutes reuse the loop's animation reference, never its loop flag."""
    return replace(source, loop=False)


def borrow_animation(source):
    """Keep one absolute animation reference so no authored option/event survives."""
    references = source.animation_references
    if not references:
        return replace(source, loop=False), 0
    for index, animation in enumerate(references):
        if animation.delta: continue
        if animation.frame_count < 1 or not math.isfinite(animation.fps) or animation.fps <= 0:
            raise ValueError('换弹来源动画帧数/FPS 无效: ' + animation.name)
        return replace(source, frame_count=animation.frame_count, fps=animation.fps, loop=False), index
    return replace(source, loop=False), 0


def build_map(sources, source_profile, *, eligible=None) -> MeleeFirearmMap:
    """Pick the melee attack group and push motion for a firearm target."""
    from .weapon_melee_sequences import _role
    roles = [_role(source, source_profile, index) for index, source in enumerate(sources)]
    plain = {index for index, source in enumerate(sources) if not _extra_activity(source.activity)
             and (eligible is None or index in eligible)}
    def usable(index):
        return (plain and index in plain and not roles[index].unused
                and sources[index].frame_count >= 2)
    timers = {index: roles[index] for index in range(len(sources))
              if usable(index) and roles[index].semantic in _ATTACK_PRIORITY
              and not roles[index].layer}
    layers = {index: roles[index] for index in range(len(sources))
              if usable(index) and roles[index].semantic in _ATTACK_PRIORITY
              and roles[index].layer}
    warnings = []
    groups = []
    paired = set()
    for index, role in timers.items():
        partner = next((i for i, candidate in layers.items()
                        if i not in paired and candidate.group == role.group), None)
        if partner is not None: paired.add(partner)
        groups.append((_ATTACK_PRIORITY[role.semantic], index, partner))
    for index, role in layers.items():
        # A lone SHOOT_LAYER without a timing partner still competes as fire.
        if index in paired or 'SHOOT' not in sources[index].activity.upper(): continue
        groups.append((_ATTACK_PRIORITY['attack'], index, None))
    groups.sort(key=lambda group: (group[0], group[1]))
    if groups:
        _, fire_timer, fire_playback = groups[0]
        warnings.append(f'近战攻击组：计时层 {sources[fire_timer].name}'
                        + (f'，播放层 {sources[fire_playback].name}。' if fire_playback is not None else '。'))
    else:
        fire_timer = fire_playback = None
        warnings.append('近战来源没有可用攻击动作（HIT*/SHOOT_LAYER/SWINGHARD），无法填充目标 fire 槽。')
    push = [index for index in range(len(sources))
            if usable(index) and roles[index].semantic == 'push']
    push_timer = next((index for index in push if not roles[index].layer), None)
    push_playback = next((index for index in push if roles[index].layer), None)
    if push_timer is None and push_playback is not None:
        push_timer, push_playback = push_playback, None
    if push_timer is not None:
        warnings.append(f'近战推击：{sources[push_timer].name} 填入目标 melee 槽。')
    else:
        warnings.append('近战来源没有推击动作，melee 槽复用击打组。')
    return MeleeFirearmMap(fire_timer, fire_playback, push_timer, push_playback, tuple(warnings))


def remap_ads_rows(rows, target_profile, reserved, warnings):
    """Rewrite melee ADS attack suffixes and donate ADS reload motion."""
    rows = list(rows)
    activities = {row.activity.upper() for row in rows if row.activity}
    collision = {}
    for index, row in enumerate(rows):
        match = _ADS_PREFIX.match((row.activity or '').upper())
        if not match: continue
        mapped = _ADS_ATTACK.get(match[2])
        if mapped is None: continue
        collision.setdefault(match[1] + mapped, []).append((index, match[2], row))
    for activity, candidates in collision.items():
        if activity in activities: continue
        candidates.sort(key=lambda item: (_ADS_COLLISION_ORDER.get(item[1], 9),
                                          -(item[2].source.frame_count if item[2].source else 1),
                                          item[0]))
        winner = candidates[0][0]
        rows[winner] = replace(rows[winner], activity=activity)
        activities.add(activity)
        for _, _, row in candidates[1:]:
            warnings.append(f'ADS 主攻击备用 {row.name}：保留原 Activity {row.activity}'
                            f'（{activity} 已由 {rows[winner].name} 占用）。')
    reload_empty = any(slot.semantic == 'reload_empty' and not slot.unused
                       for slot in target_profile.slots)
    for row in tuple(rows):
        match = _ADS_PREFIX.match((row.activity or '').upper())
        if not match or match[2] != 'INSPECT' or row.source is None: continue
        for suffix, needed in (('RELOAD', True), ('RELOAD_EMPTY', reload_empty)):
            if not needed: continue
            activity = match[1] + suffix
            if activity in activities: continue
            role = _classify(replace(row.source, name='__loka_ads_role', activity=activity),
                             target_profile, 0)
            compatible = next((slot for slot in target_profile.slots
                               if (slot.semantic, slot.layer) == role[:2] and not slot.unused), None)
            if compatible is None: continue
            name = row.name + '_' + suffix.lower()
            stem = name
            counter = 1
            while name.lower() in reserved:
                name = f'{stem}_{counter}'
                counter += 1
            reserved.add(name.lower())
            activities.add(activity)
            fps, numframes = _timing(row.source, replace(compatible, hidden=False))
            rows.append(replace(row, name=name, activity=activity, fps=fps, numframes=numframes,
                                preserve_source_fps=False, fps_scale=None, fps_scale_ratio=None,
                                source_layer=None, source_visible=None, reload_parts=(),
                                reload_helpers=(), reload_helper=False, source_sequence_map=()))
            warnings.append(f'ADS 换弹：{row.name} 另生成 {name}（{activity}）。')
    return tuple(rows)
