"""Shared logic for the "subtract correction" decompile feature.

the parser's decompiler cannot know which animation a delta sequence was originally
built against (``subtract <anim> <frame>`` in the source QC), so it invents an
empty ``*_corrective_animation`` target and points the delta sequence's
``subtract`` option at it.  The subtract-correction feature instead picks a
*real* idle animation as the subtract base, adds that idle's frame 0
pose back into the decompiled animation SMDs (the exact inverse of
studiomdl's ``subtractBaseAnimations`` POST branch), and writes
``subtract "<idle>" 0`` in the QC so recompiling reproduces the same delta.

This module holds the parts that both ``decompiler.py`` and
``core/writers/qc.py`` need, so the duplicated ``SetUpCorrectiveSubtract
AnimationBlocks`` ports stay in sync.
"""

from __future__ import annotations

from typing import Any, Optional
import math
import re

from .constants import STUDIO_DELTA, STUDIO_LOOPING
from .ani import extract_anim_value
from .naming import create_corrective_animation_name
from .vectors import SourceVector
from .writers.animation import (
    add_subtract_base_pose,
    calc_animation,
    _quaternion_conjugate,
    _quaternion_multiply,
    _stored_euler_to_quaternion,
)

__all__ = [
    "find_idle_candidates",
    "resolve_idle_desc_for_item",
    "is_subtract_correction_active",
    "apply_corrective_subtract_setup",
]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return default if value is None else value


def is_subtract_correction_active(options: Any) -> bool:
    """True when the caller enabled the subtract-correction feature."""
    return bool(getattr(options, "subtract_correction", False))


def find_idle_candidates(
    mdl_file_data: Any,
    subtract_anim_descs: Optional[list] = None,
) -> list:
    """Return the animation descs eligible as a subtract base ("idle" pool).

    A candidate is a *named* animation (a real ``$animation`` in the QC, not an
    ``@``-implied one) that:

    * is not itself a delta/subtract animation (``STUDIO_DELTA`` clear),
    * has "idle" in its name (case-insensitive), and
    * is not one of the animations that the delta sequences turn into
      corrective subtracts (``subtract_anim_descs``).

    Returns them in MDL animation order.
    """
    animation_descs = _get(mdl_file_data, "theAnimationDescs", []) or []
    excluded = set()
    if subtract_anim_descs:
        excluded = {id(desc) for desc in subtract_anim_descs}

    candidates: list = []
    for an_animation_desc in animation_descs:
        name = _get(an_animation_desc, "theName", "")
        if name == "" or name[0] == "@":
            continue
        flags = _get(an_animation_desc, "flags", 0)
        if (flags & STUDIO_DELTA) != 0:
            continue
        if "idle" not in name.lower():
            continue
        if id(an_animation_desc) in excluded:
            continue
        candidates.append(an_animation_desc)
    return candidates


def resolve_idle_desc_for_item(
    item_name: str,
    candidates: list,
    selected_name: str = "",
    prefer_plain: bool = False,
    *, candidate_roles: Optional[dict] = None,
) -> Optional[Any]:
    """Pick the subtract base for one delta animation/sequence.

    ``selected_name`` (a user choice from the GUI) wins whenever a candidate of
    that exact name exists.  Otherwise the default name matching applies: items
    containing "ads" match a candidate whose name also contains "ads",
    plain items match a plain idle.  ``prefer_plain`` (set for ads transition
    sequences such as ads_in/ads_out, per the reference QC) overrides the
    "ads" name signal so those items match a plain idle.  If the preferred
    group is empty the result is ``None``; automatic choices never silently
    cross between plain and ADS poses.
    """
    if not candidates:
        return None

    if selected_name:
        for a_candidate in candidates:
            if _get(a_candidate, "theName", "") == selected_name:
                return a_candidate
        # Fall through to the automatic matching when the picked name does not
        # exist in this model.

    wanted = "ads" in item_name.lower() and not prefer_plain
    ads_candidates = []
    plain_candidates = []
    for a_candidate in candidates:
        name = _get(a_candidate, "theName", "")
        roles=(candidate_roles or {}).get(id(a_candidate),{'ads' in name.lower()})
        if True in roles:ads_candidates.append(a_candidate)
        if False in roles:plain_candidates.append(a_candidate)

    preferred = ads_candidates if wanted else plain_candidates
    if preferred:
        return preferred[0]
    return None


_REAL_IDLE_ACTIVITY = re.compile(
    r"^(?:MIXED_)?ACT(?:_[A-Z0-9]+)*_VM_IDLE(?:_[A-Z0-9]+)*$"
)


def _is_real_idle_activity(activity: str) -> bool:
    activity = activity.upper()
    return "_TO_" not in activity and bool(_REAL_IDLE_ACTIVITY.fullmatch(activity))


def _ads_idle_role(sequence: Any, animation: Any) -> bool:
    activity=str(_get(sequence,'theActivityName','')).upper()
    return ('ads' in (_get(sequence,'theName','')+' '+_get(animation,'theName','')).lower()
            or activity.startswith('MIXED_')
            or any('_'+role+'_' in activity for role in ('PRIMARY','SECONDARY','TERTIARY','FOURTH')))


def _semantic_idle_candidates(mdl_file_data: Any, excluded_descs: list) -> list:
    """Return ``(sequence, descriptor)`` pairs proven by idle activities."""
    animations = _get(mdl_file_data, "theAnimationDescs", []) or []
    excluded = {id(desc) for desc in excluded_descs}
    seen: set[int] = set()
    candidates = []
    for sequence in (_get(mdl_file_data, "theSequenceDescs", []) or []):
        flags = _get(sequence, "flags", 0)
        if (flags & STUDIO_DELTA) != 0:
            continue
        if not _is_real_idle_activity(str(_get(sequence, "theActivityName", ""))):
            continue
        for index in (_get(sequence, "theAnimDescIndexes", []) or []):
            if not isinstance(index, int) or not 0 <= index < len(animations):
                continue
            animation = animations[index]
            animation_flags = _get(animation, "flags", 0)
            if (animation_flags & STUDIO_DELTA or
                    id(animation) in excluded or id(animation) in seen):
                continue
            seen.add(id(animation))
            candidates.append((sequence, animation))
    return candidates


def _unit_quaternion(value: Any, context: str):
    components = [float(_get(value, axis, 0.0)) for axis in "xyzw"]
    norm = math.sqrt(sum(component * component for component in components))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError(context + ": nonfinite/zero quaternion")
    return type(value)(*(component / norm for component in components))


def _rotate_vector(rotation: Any, position: tuple[float, float, float]):
    vector = type(rotation)(position[0], position[1], position[2], 0.0)
    result = _quaternion_multiply(
        _quaternion_multiply(rotation, vector), _quaternion_conjugate(rotation)
    )
    return (result.x, result.y, result.z)


def _global_transforms(mdl_file_data: Any, poses: dict, context: str) -> dict:
    bones = _get(mdl_file_data, "theBones", []) or []
    result = {}
    visiting: set[int] = set()

    def transform(index: int):
        if index in result:
            return result[index]
        if not 0 <= index < len(bones) or index not in poses or index in visiting:
            raise ValueError(context + ": invalid touch bone hierarchy")
        visiting.add(index)
        line = poses[index]
        position = tuple(float(_get(line.position, axis, 0.0)) for axis in "xyz")
        rotation = _unit_quaternion(_stored_euler_to_quaternion(line.rotation), context)
        parent = _get(bones[index], "parentBoneIndex", -1)
        if parent >= 0:
            parent_position, parent_rotation = transform(parent)
            offset = _rotate_vector(parent_rotation, position)
            position = tuple(a + b for a, b in zip(parent_position, offset))
            rotation = _unit_quaternion(
                _quaternion_multiply(parent_rotation, rotation), context
            )
        visiting.remove(index)
        if not all(math.isfinite(value) for value in (*position, *rotation.as_tuple())):
            raise ValueError(context + ": nonfinite touch FK")
        result[index] = (position, rotation)
        return result[index]

    for bone_index in poses:
        transform(bone_index)
    return result


def _strict_ik_value(animation: Any, rule: Any, channel_index: int,
                     frame_index: int, wrapped: bool, context: str) -> tuple[float, float]:
    error = _get(rule, "theCompressedIkError", None)
    if _get(rule, "ikErrorOffset", 0) or error is None:
        raise ValueError(context + ": compressed touch IK data unavailable")
    scales = list(_get(error, "scale", []) or [])
    offsets = list(_get(error, "offset", []) or [])
    channels = list(_get(error, "theAnimValues", []) or [])
    if len(scales) != 6 or len(offsets) != 6 or len(channels) != 6:
        raise ValueError(context + ": invalid compressed touch IK channel table")
    if not all(math.isfinite(value) for value in scales):
        raise ValueError(context + ": nonfinite compressed touch IK scale")
    local_frame = frame_index - int(_get(rule, "ikErrorIndexStart", 0))
    if local_frame < 0 and wrapped:
        local_frame += max(int(_get(animation, "frameCount", 0)) - 1, 1)
    if local_frame < 0:
        raise ValueError(context + ": active touch frame precedes compressed IK data")
    channel = channels[channel_index]
    if channel is None and not offsets[channel_index]:
        return 0.0, abs(float(scales[channel_index]))
    remaining = local_frame
    cursor = 0
    while True:
        if not channel or cursor >= len(channel):
            raise ValueError(context + ": compressed touch IK RLE unavailable")
        span = channel[cursor]
        valid = int(_get(span, "valid", 0)); total = int(_get(span, "total", 0))
        if not 0 < valid <= total or cursor + valid >= len(channel):
            raise ValueError(context + ": invalid compressed touch IK RLE")
        if remaining < total:
            break
        remaining -= total
        cursor += valid + 1
    value = extract_anim_value(local_frame, channel, scales[channel_index])
    if not math.isfinite(value):
        raise ValueError(context + ": nonfinite compressed touch IK value")
    return value, abs(float(scales[channel_index]))


def _quaternion_angle(first: Any, second: Any) -> float:
    dot = sum(a * b for a, b in zip(first.as_tuple(), second.as_tuple()))
    return 2 * math.acos(min(1.0, abs(dot)))


def _candidate_touch_evidence(mdl_file_data: Any, animation: Any,
                              candidate: Any, context: str) -> tuple[float, list]:
    try:
        base_pose = calc_animation(mdl_file_data, None, candidate, 0)
    except MemoryError:
        raise
    except Exception as error:
        raise ValueError(context + ": subtract base pose cannot be decoded") from error
    rules = [rule for rule in (_get(animation, "theIkRules", []) or [])
             if _get(rule, "type", 0) == 1]
    frame_count = int(_get(animation, "frameCount", 0))
    if frame_count < 1:
        raise ValueError(context + ": touch animation has no frames")
    chains = _get(mdl_file_data, "theIkChains", []) or []
    predictions = []
    worst = 0.0
    pose_cache = {}
    for rule in rules:
        fake_position_value = getattr(rule, "pos", None)
        fake_rotation_value = getattr(rule, "q", None)
        if fake_position_value is None or fake_rotation_value is None:
            raise ValueError(context + ": touch fake transform is unavailable")
        fake_position = [float(getattr(fake_position_value, axis)) for axis in "xyz"]
        fake_rotation = [float(getattr(fake_rotation_value, axis)) for axis in "xyzw"]
        if not all(math.isfinite(value) for value in (*fake_position, *fake_rotation)):
            raise ValueError(context + ": nonfinite touch fake transform")
        identity = (all(value == 0 for value in fake_rotation[:3]) and
                    abs(fake_rotation[3]) == 1)
        if any(value != 0 for value in fake_position) or (any(fake_rotation) and not identity):
            raise ValueError(context + ": nonzero touch fake transform is unsupported")
        chain_index = int(_get(rule, "chain", -1)); target_index = int(_get(rule, "bone", -1))
        if not 0 <= chain_index < len(chains):
            raise ValueError(context + ": invalid touch chain")
        links = _get(chains[chain_index], "theLinks", []) or []
        if not links:
            raise ValueError(context + ": touch chain has no effector")
        effector_index = int(_get(links[-1], "boneIndex", -1))
        start = float(_get(rule, "influenceStart", 0.0)); end = float(_get(rule, "influenceEnd", 0.0))
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= 1 or not 0 <= end <= 1:
            raise ValueError(context + ": invalid touch active range")
        wrapped = start > end
        sampled = False
        for frame_index in range(frame_count):
            phase = frame_index / max(frame_count - 1, 1)
            if not ((start <= phase <= end) if not wrapped else (phase >= start or phase <= end)):
                continue
            if not wrapped and frame_index < int(_get(rule, "ikErrorIndexStart", 0)):
                continue
            values, scales = [], []
            for channel_index in range(6):
                value, scale = _strict_ik_value(
                    animation, rule, channel_index, frame_index, wrapped, context
                )
                values.append(value); scales.append(scale)
            if frame_index not in pose_cache:
                try:
                    pose = calc_animation(mdl_file_data, None, animation, frame_index)
                    add_subtract_base_pose(pose, base_pose)
                    pose_cache[frame_index] = _global_transforms(mdl_file_data, pose, context)
                except ValueError:
                    raise
                except MemoryError:
                    raise
                except Exception as error:
                    raise ValueError(context + ": touch FK cannot be evaluated") from error
            transforms = pose_cache[frame_index]
            if effector_index not in transforms or target_index not in transforms:
                raise ValueError(context + ": touch effector/target pose is missing")
            effector_position, effector_rotation = transforms[effector_index]
            target_position, target_rotation = transforms[target_index]
            inverse_target = _quaternion_conjugate(target_rotation)
            relative_position = _rotate_vector(
                inverse_target,
                tuple(a - b for a, b in zip(effector_position, target_position)),
            )
            relative_rotation = _unit_quaternion(
                _quaternion_multiply(inverse_target, effector_rotation), context
            )
            expected_rotation = _unit_quaternion(
                _stored_euler_to_quaternion(SourceVector(*values[3:])), context
            )
            for index, (actual, expected) in enumerate(zip(relative_position, values[:3])):
                tolerance = max(0.025, 4 * scales[index])
                worst = max(worst, abs(actual - expected) / tolerance)
            angle = _quaternion_angle(relative_rotation, expected_rotation)
            rotation_tolerance = max(0.001, 4 * sum(scales[3:]))
            worst = max(worst, angle / rotation_tolerance)
            predictions.append((relative_position, relative_rotation))
            sampled = True
        if not sampled:
            raise ValueError(context + ": touch rule has no covered active frames")
    if not math.isfinite(worst):
        raise ValueError(context + ": nonfinite touch fidelity score")
    return worst, predictions


def _equivalent_touch_predictions(first: list, second: list) -> bool:
    if len(first) != len(second):
        return False
    for (first_position, first_rotation), (second_position, second_rotation) in zip(first, second):
        if any(abs(a - b) > 1e-6 for a, b in zip(first_position, second_position)):
            return False
        if _quaternion_angle(first_rotation, second_rotation) > 1e-6:
            return False
    return True


def _equivalent_full_pose_predictions(first: list, second: list) -> bool:
    """Use the same positional/angular budget as the independent witness."""
    return len(first) == len(second) and all(
        max(abs(a-b) for a,b in zip(p,q)) <= .025 and _quaternion_angle(r,t) <= .001
        for (p,r),(q,t) in zip(first,second))


def _require_decoded_pose_path(desc: Any, bone_count: int, frame_count: int,
                               context: str) -> None:
    """Admit only resident RAW/RLE records handled by calc_animation.

    A complete dictionary of default bone poses is not decoding evidence.
    FRAMEANIM has a separate writer/decoder and cannot prove this fallback.
    """
    if desc.flags & 0x40:
        raise ValueError(context + ": unsupported FRAMEANIM full-pose evidence")
    if _get(desc, "animBlock", 0) or _get(desc, "animblockIkRuleOffset", 0):
        raise ValueError(context + ": external full-pose evidence")
    sections = _get(desc, "theSectionsOfAnimations", None)
    section_size = _get(desc, "sectionFrameCount", 0)
    if not isinstance(section_size, int) or section_size < 0 or _get(desc, "animOffset", 0) <= 0:
        raise ValueError(context + ": unavailable full-pose record offsets")
    last_section = (frame_count - 1) // section_size if section_size else 0
    table = _get(desc, "theSections", None)
    if section_size and (not table or _get(desc, "sectionOffset", 0) <= 0):
        raise ValueError(context + ": missing full-pose section table")
    for index in range(last_section + 1):
        if table:
            if index >= len(table) or table[index].animBlock or table[index].animOffset <= 0:
                raise ValueError(context + ": external/unavailable full-pose section")
        if not sections or index >= len(sections) or not sections[index]:
            raise ValueError(context + ": missing decoded full-pose section")
        records = sections[index]
        indexes = [record.boneIndex for record in records]
        if indexes != sorted(set(indexes)) or any(not 0 <= i < bone_count for i in indexes):
            raise ValueError(context + ": unsupported full-pose bone records")
        needed = min(section_size, frame_count-index*section_size) if section_size else frame_count
        for record in records:
            if record.flags & ~0x3f:
                raise ValueError(context + ": unsupported full-pose record encoding")
            for flag, name in ((1, "thePos"), (2, "theRot48bits"), (32, "theRot64bits")):
                if record.flags & flag and _get(record, name, None) is None:
                    raise ValueError(context + ": missing full-pose RAW data")
            for flag, name in ((4, "thePosV"), (8, "theRotV")):
                if not record.flags & flag:
                    continue
                pointer = _get(record, name, None)
                if pointer is None:
                    raise ValueError(context + ": missing full-pose RLE pointer")
                for axis in "XYZ":
                    offset = getattr(pointer, "anim" + axis + "ValueOffset")
                    values = getattr(pointer, "theAnim" + axis + "Values")
                    if offset == 0 and not values:
                        continue
                    if offset <= 0 or not values:
                        raise ValueError(context + ": missing full-pose RLE channel")
                    total = cursor = 0
                    while total < needed:
                        if cursor >= len(values):
                            raise ValueError(context + ": unavailable full-pose RLE frames")
                        span = values[cursor]
                        if not 0 < span.valid <= span.total or cursor + span.valid >= len(values):
                            raise ValueError(context + ": unsupported full-pose RLE span")
                        total += span.total
                        cursor += span.valid + 1


def _candidate_full_pose_evidence(mdl_file_data: Any, animation: Any,
                                  candidate: Any, context: str) -> tuple[bool, list]:
    """Independent evidence: every local bone at every frame of a full pose."""
    bones = _get(mdl_file_data, "theBones", []) or []
    frames = animation.frameCount
    witnesses = [a for a in (_get(mdl_file_data, "theAnimationDescs", []) or [])
                 if a is not animation and not a.flags & STUDIO_DELTA
                 and a.frameCount == frames and a.fps == animation.fps]
    if not bones or not witnesses:
        return False, []

    def trajectory(desc, base=None):
        _require_decoded_pose_path(desc, len(bones), frames, context)
        result = []
        for frame in range(frames):
            pose = calc_animation(mdl_file_data, None, desc, frame)
            if set(pose) != set(range(len(bones))):
                raise ValueError(context + ": incomplete full-pose evidence")
            if base is not None:
                add_subtract_base_pose(pose, base)
            for index in range(len(bones)):
                line = pose[index]
                position = tuple(getattr(line.position, axis) for axis in "xyz")
                if not all(math.isfinite(v) for v in position):
                    raise ValueError(context + ": nonfinite full-pose evidence")
                rotation = _unit_quaternion(_stored_euler_to_quaternion(line.rotation), context)
                result.append((position, rotation))
        return result

    _require_decoded_pose_path(candidate, len(bones), 1, context)
    base = calc_animation(mdl_file_data, None, candidate, 0)
    if set(base) != set(range(len(bones))):
        raise ValueError(context + ": incomplete full-pose base")
    predicted = trajectory(animation, base)
    supported = False
    for witness in witnesses:
        observed = trajectory(witness)
        if all(max(abs(x-y) for x,y in zip(p,q)) <= .025 and
               _quaternion_angle(r,t) <= .001
               for (p,r),(q,t) in zip(predicted,observed)):
            supported = True
    return supported, predicted


def _select_semantic_base(mdl_file_data: Any, sequence: Any, animation: Any,
                          candidates: list, selected_name: str,
                          old_choice: Any, *, validate_errors: bool = True) -> Any:
    context = (f'{_get(mdl_file_data,"theModelName","")}: '
               f'{_get(animation,"theName","")} / {_get(sequence,"theName","")}')
    eligible = [candidate for _source, candidate in candidates]
    if selected_name:
        eligible = [candidate for candidate in eligible
                    if _get(candidate, "theName", "") == selected_name]
        if len(eligible) != 1:
            raise ValueError(context + f": explicit real idle base {selected_name!r} is unknown/ambiguous")
    if not eligible:
        raise ValueError(context + ": touch IK requires a real idle activity base")
    for candidate in eligible:
        fps = _get(candidate, "fps", 0); frames = _get(candidate, "frameCount", 0)
        if (not isinstance(frames, int) or frames < 1 or
                not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0):
            raise ValueError(context + f': invalid real idle base {_get(candidate,"theName","")!r}')
    evidence = []; failures = []
    for candidate in eligible:
        try:
            score, predictions = _candidate_touch_evidence(
                mdl_file_data, animation, candidate,
                context + f' base={_get(candidate,"theName","")!r}',
            )
        except ValueError as error:
            failures.append(str(error));continue
        evidence.append((candidate, score, predictions))
    faithful = [item for item in evidence if item[1] <= 1 + 1e-9]
    if not evidence:
        raise ValueError(context+': no real idle base has readable touch evidence; '+
                         (failures[0] if failures else 'no candidates'))
    if not validate_errors:
        # Keep the closest readable base without a fidelity threshold or a
        # full-pose witness requirement. Equal scores retain the prior preference.
        best=min(evidence,key=lambda item:item[1])
        return next((candidate for candidate,score,_ in evidence
                     if candidate is old_choice and math.isclose(score,best[1],rel_tol=1e-6,abs_tol=1e-9)),best[0])
    if not faithful:
        supported = []
        for candidate in eligible:
            try:
                matches, trajectory = _candidate_full_pose_evidence(
                    mdl_file_data, animation, candidate, context)
            except ValueError as error:
                failures.append(str(error));continue
            if matches:
                supported.append((candidate, trajectory))
        if not supported:
            best=min(evidence,key=lambda item:item[1]) if evidence else None
            diagnostic=(f"; best candidate {_get(best[0],'theName','')!r}, normalized residual {best[1]:.6g} (limit 1)"
                        if best else '; no candidate has readable touch evidence')
            if failures:diagnostic += '; '+failures[0]
            raise ValueError(context + ": no real idle base matches original touch IK curves or full-pose witness"+diagnostic)
        if any(not _equivalent_full_pose_predictions(supported[0][1], item[1])
               for item in supported[1:]):
            raise ValueError(context + ": ambiguous real idle bases with distinct full-pose trajectories")
        animation.thePreserveSourceTouchCurves = True
        return next((candidate for candidate, _ in supported if candidate is old_choice), supported[0][0])
    if selected_name:
        return faithful[0][0]
    faithful.sort(key=lambda item: item[1])
    best = faithful[0]
    equivalent = [item for item in faithful
                  if _equivalent_touch_predictions(best[2], item[2])]
    competitors = [item for item in faithful if item not in equivalent]
    if competitors and math.isclose(competitors[0][1], best[1], rel_tol=1e-6, abs_tol=1e-9):
        raise ValueError(context + ": ambiguous real idle bases with distinct touch trajectories")
    if old_choice is not None:
        for candidate, _score, _predictions in equivalent:
            if candidate is old_choice:
                return candidate
    return equivalent[0][0]


def collect_subtract_anim_descs(mdl_file_data: Any) -> list:
    """Implement the collection half of ``SetUpCorrectiveSubtractAnimationBlocks``.

    Walks every ``STUDIO_DELTA`` sequence and gathers the animation descs it
    references -- those are the ones the parser turns into corrective (subtract)
    animations.  Returns ``(sequence_desc, animation_desc)`` pairs, deduped on
    the animation desc (a desc shared by several delta sequences appears once).
    """
    sequence_descs = _get(mdl_file_data, "theSequenceDescs", None)
    animation_descs = _get(mdl_file_data, "theAnimationDescs", []) or []
    pairs: list = []
    seen: set[int] = set()

    if sequence_descs is None:
        return pairs

    for a_sequence_desc in sequence_descs:
        if (_get(a_sequence_desc, "flags", 0) & STUDIO_DELTA) == 0:
            continue
        anim_desc_indexes = _get(a_sequence_desc, "theAnimDescIndexes", None)
        if anim_desc_indexes is None or len(anim_desc_indexes) <= 0:
            continue
        for index in anim_desc_indexes:
            try:
                an_animation_desc = animation_descs[index]
            except (IndexError, TypeError):
                continue
            if id(an_animation_desc) in seen:
                continue
            seen.add(id(an_animation_desc))
            pairs.append((a_sequence_desc, an_animation_desc))
    return pairs


def apply_corrective_subtract_setup(mdl_file_data: Any, options: Any) -> list:
    """Shared body of ``SetUpCorrectiveSubtractAnimationBlocks`` (both ports).

    With the subtract-correction feature off this is exactly the parser's port:
    every delta sequence's animation desc gets a ``*_corrective_animation``
    name, and the descs are collected into ``theCorrectiveAnimationDescs``.

    With the feature on, each subtract target instead resolves to a real
    idle animation (see :func:`resolve_idle_desc_for_item`): the
    corrective name becomes the idle's name and the desc is *not* added to
    ``theCorrectiveAnimationDescs`` (no empty corrective ``$animation`` blocks,
    no corrective SMDs).  The resolved base desc is stored on the animation as
    ``theSubtractCorrectionBaseDesc`` for the SMD writer's additive blend.

    Returns the new ``theCorrectiveAnimationDescs`` list (and assigns it).
    """
    corrective_animation_descs: list = []
    setattr(mdl_file_data, "theCorrectiveAnimationDescs", corrective_animation_descs)

    # Both the SMD and QC writer call setup; decisions must not leak from an
    # earlier policy into a fresh export (including sequence-owned @ refs).
    for item in (*(_get(mdl_file_data, "theSequenceDescs", []) or []),
                 *(_get(mdl_file_data, "theAnimationDescs", []) or [])):
        item.theCorrectiveAnimationName = ""
        for attribute in ("theSubtractCorrectionBaseDesc", "theRequiresSemanticBase",
                          "theSemanticSubtractName", "theSemanticAnimationName",
                          "thePreserveSourceTouchCurves", "theCorrectiveAnimationSymbol"):
            if hasattr(item, attribute):
                delattr(item, attribute)

    subtract_pairs = collect_subtract_anim_descs(mdl_file_data)
    preserve = bool(getattr(options, "preserve_animation_semantics", False))
    if preserve:
        seen = {id(d) for _,d in subtract_pairs}
        for sequence in (_get(mdl_file_data,"theSequenceDescs",[]) or []):
            for index in (_get(sequence,"theAnimDescIndexes",[]) or []):
                desc = mdl_file_data.theAnimationDescs[index]
                if _get(desc,"flags",0)&STUDIO_DELTA and id(desc) not in seen:
                    subtract_pairs.append((sequence,desc));seen.add(id(desc))
    if not subtract_pairs:
        return corrective_animation_descs

    active = is_subtract_correction_active(options)
    selected_name = ""
    if active:
        selected_name = str(getattr(options, "subtract_correction_idle", "") or "")
    per_item_bases = getattr(options, "subtract_bases", None) or {}
    candidates = find_idle_candidates(mdl_file_data,
        [d for _s,d in subtract_pairs if not preserve or _get(d,"flags",0)&STUDIO_DELTA]) if active or preserve else []
    semantic_candidates = _semantic_idle_candidates(
        mdl_file_data, [d for _s, d in subtract_pairs if _get(d, "flags", 0) & STUDIO_DELTA]
    ) if preserve else []
    if active and preserve:
        for _source,candidate in semantic_candidates:
            if all(candidate is not existing for existing in candidates):
                candidates.append(candidate)
    candidate_roles={}
    animations=_get(mdl_file_data,'theAnimationDescs',[]) or []
    for source in (_get(mdl_file_data,'theSequenceDescs',[]) or []):
        if not _is_real_idle_activity(str(_get(source,'theActivityName',''))):continue
        for index in (_get(source,'theAnimDescIndexes',[]) or []):
            if isinstance(index,int) and 0<=index<len(animations):
                base=animations[index]
                candidate_roles.setdefault(id(base),set()).add(_ads_idle_role(source,base))
    reserved={_get(item,'theName','').casefold() for item in (
        *(_get(mdl_file_data,'theAnimationDescs',[]) or []),
        *(_get(mdl_file_data,'theSequenceDescs',[]) or []))}

    def base_symbol(animation):
        name=_get(animation,'theName','')
        if not preserve or (name and not name.startswith('@')):
            return name
        alias=_get(animation,'theSemanticAnimationName','')
        if not alias:
            index=next(i for i,item in enumerate(animations) if item is animation)
            alias=f'loka_semantic_ref_{index}'
            while alias.casefold() in reserved:
                alias+='_'
            reserved.add(alias.casefold())
            animation.theSemanticAnimationName=alias
        return alias

    # Declare the whole idle blend explicitly: leaving one implied reference
    # would apply its sequence-local IK/options to the named base as well.
    for _source,candidate in semantic_candidates:
        base_symbol(candidate)

    def assign_subtract(sequence,animation,target_name):
        name=_get(animation,"theName","")
        if preserve and (not name or name.startswith("@")):
            animation.theSemanticSubtractName=target_name
        elif not name or name.startswith("@"):
            sequence.theCorrectiveAnimationName=target_name
        else:
            animation.theCorrectiveAnimationName=target_name

    for a_sequence_desc, an_animation_desc in subtract_pairs:
        name = _get(an_animation_desc, "theName", "")

        if preserve and not _get(an_animation_desc,"flags",0)&STUDIO_DELTA:
            continue
        required = preserve and getattr(options,"ik_rule_handling","default") == "default" and any(
            _get(rule,"type",0)==1 for rule in (_get(an_animation_desc,"theIkRules",[]) or []))

        if required:
            match_name = name.lstrip("@") or _get(a_sequence_desc,"theName","")
            selected = per_item_bases.get(match_name, selected_name)
            activity = _get(a_sequence_desc,"theActivityName","")
            wanted_ads = _ads_idle_role(a_sequence_desc,an_animation_desc) and not (
                "IDLE_TO_LOWERED" in activity or "LOWERED_TO_IDLE" in activity)
            old_choice = resolve_idle_desc_for_item(
                ('ads_' if wanted_ads else '')+match_name, candidates, "", prefer_plain=not wanted_ads,
                candidate_roles=candidate_roles
            )
            base = _select_semantic_base(
                mdl_file_data, a_sequence_desc, an_animation_desc,
                [(source, candidate) for source,candidate in semantic_candidates
                 if selected or wanted_ads in candidate_roles.get(id(candidate),{_ads_idle_role(source,candidate)})], selected, old_choice,
                validate_errors=bool(getattr(options,'validate_ik_errors',True)),
            )
            assign_subtract(a_sequence_desc,an_animation_desc,base_symbol(base))
            an_animation_desc.theSubtractCorrectionBaseDesc = base
            an_animation_desc.theRequiresSemanticBase = True
            continue

        if active:
            # Prefer the animation's own name for matching; an @-implied anim
            # falls back to its sequence's name ("@" itself carries no signal).
            match_name = name if name and not name.startswith("@") else _get(
                a_sequence_desc, "theName", ""
            )
            # A per-item base chosen in the GUI (keyed by the listed name) wins
            # over the global idle selection; an unknown name falls back to the
            # automatic matching inside resolve_idle_desc_for_item.
            item_selected = per_item_bases.get(match_name.lstrip("@"), selected_name)
            # Transition sequences (ads_in/ads_out: *IDLE_TO_LOWERED/*LOWERED_TO_IDLE)
            # subtract against the plain idle, not the ads idle -- per the
            # reference QC v_rifle_true.qc.
            activity = _get(a_sequence_desc, "theActivityName", "")
            prefer_plain = (
                "IDLE_TO_LOWERED" in activity or "LOWERED_TO_IDLE" in activity
            )
            if _ads_idle_role(a_sequence_desc,an_animation_desc) and not prefer_plain:
                match_name='ads_'+match_name
            base_desc = resolve_idle_desc_for_item(
                match_name, candidates, item_selected, prefer_plain=prefer_plain,
                candidate_roles=candidate_roles
            )
            if base_desc is not None:
                idle_name = base_symbol(base_desc)
                assign_subtract(a_sequence_desc,an_animation_desc,idle_name)
                setattr(an_animation_desc, "theSubtractCorrectionBaseDesc", base_desc)
                continue

        # the parser's default behaviour (also the fallback when no idle could be
        # resolved for this item).
        preferred_name = create_corrective_animation_name(name)
        corrective_name=preferred_name;number=2
        while corrective_name.casefold() in reserved:
            corrective_name=f'{preferred_name}_{number}';number+=1
        reserved.add(corrective_name.casefold())
        an_animation_desc.theCorrectiveAnimationSymbol=corrective_name
        assign_subtract(a_sequence_desc,an_animation_desc,corrective_name)
        try:
            delattr(an_animation_desc, "theSubtractCorrectionBaseDesc")
        except AttributeError:
            pass
        if an_animation_desc not in corrective_animation_descs:
            corrective_animation_descs.append(an_animation_desc)

    if preserve:
        animations=mdl_file_data.theAnimationDescs
        for index,animation in enumerate(animations):
            target=getattr(animation,"theSemanticSubtractName","")
            if not target:continue
            users=[sequence for sequence in (mdl_file_data.theSequenceDescs or [])
                   for reference in (_get(sequence,"theAnimDescIndexes",[]) or []) if reference==index]
            if len(users)==1 and len(users[0].theAnimDescIndexes)==1:
                # A lone direct reference can carry subtract on its own sequence,
                # independently of that sequence's DELTA/POST classification.
                users[0].theCorrectiveAnimationName=target
            else:
                # Shared implied descriptors require one declaration to preserve
                # identity; mixed refs require it to isolate their operation.
                alias=f'loka_semantic_ref_{index}'
                while alias.casefold() in reserved:
                    alias += "_"
                reserved.add(alias.casefold())
                animation.theSemanticAnimationName=alias
                animation.theCorrectiveAnimationName=target

    return corrective_animation_descs
