"""Build animation frame lines, skeleton sections and physics pose transforms.

"""

from __future__ import annotations

import math
from typing import Callable, Final, Optional

from ..ani import (
    SourceAniFrameAnim49,
    SourceMdlAnimation,
    euler_angles_to_quaternion,
    extract_anim_value,
    to_euler_angles,
    vector_yaw_rotate,
)
from ..mdl_data import (
    SourceMdlAnimationDesc49,
    SourceMdlFileData49,
    SourceMdlSequenceDesc,
)
from ..numfmt import fmt_fixed6, fmt_int
from ..vectors import (
    SourceQuaternion,
    SourceVector,
)

__all__ = [
    "AnimationFrameLine",
    "calc_animation",
    "calc_bone_rotation",
    "calc_bone_position",
    "adjust_position_and_rotation",
    "adjust_position_and_rotation_by_piecewise_movement",
    "calculate_first_anim_desc_frame_lines_for_physics",
    "write_skeleton_section_for_animation",
]


def _quaternion_multiply(a: SourceQuaternion, b: SourceQuaternion) -> SourceQuaternion:
    """Hamilton product ``a * b`` (apply ``b`` first, then ``a``).

    Matches Source's ``QuaternionMult( p, q, out )`` = ``p * q`` convention:
    ``QuaternionSM(-1, base, delta)`` in studiomdl's subtract computes
    ``base^-1 * delta``, so the inverse reconstruction composes
    ``base * delta``.
    """
    return SourceQuaternion(
        a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
        a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
        a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
        a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
    )


def _quaternion_conjugate(a: SourceQuaternion) -> SourceQuaternion:
    """Conjugate (= inverse for unit quaternions) of ``a``."""
    return SourceQuaternion(-a.x, -a.y, -a.z, a.w)


def _stored_euler_to_quaternion(rotation: SourceVector) -> SourceQuaternion:
    """Stored-slot rotation triple -> quaternion (the studiomdl convention).

    The MDL stores per-bone rotations as ``RadianEuler`` triples.  studiomdl's
    ``QuaternionSMAngles`` converts them with ``AngleQuaternion`` after the
    engine's cyclic ``RadianEuler`` -> ``QAngle`` remapping, so the
    ``EulerAnglesToQuaternion`` (pitch, yaw, roll) arguments are taken from the
    stored slots in ``(y, z, x)`` order.  Verified against real compiler output:
    with this mapping ``delta = base^-1 * full`` reproduces the stored delta
    triples to < 0.01 degrees on every bone of the regression models (any other
    slot mapping / conversion pair misses by tens of degrees).
    """
    return euler_angles_to_quaternion(
        SourceVector(rotation.y, rotation.z, rotation.x)
    )


def add_subtract_base_pose(
    frame_lines: dict[int, AnimationFrameLine],
    base_frame_lines: dict[int, AnimationFrameLine],
    axis_offset_radians: Optional[tuple[float, float, float]] = None,
) -> None:
    """Undo studiomdl's ``subtract`` on one frame of animation data.

    studiomdl's ``subtractBaseAnimations`` (POST branch, i.e. the QC
    ``subtract`` option) transforms every bone with a nonzero weight into a
    delta relative to the base animation's chosen frame::

        delta_rot = base_rot^-1 * new_rot      delta_pos = new_pos - base_pos

    (``QuaternionSMAngles(-1, src, dest)`` = ``QuaternionSM`` = ``(s*p)*q``
    with ``s = -1``, i.e. ``base^-1 * new``; ``simplify.cpp`` lines 1078-1110.)

    This is the exact inverse, so recompiling the SMD written from the result
    with ``subtract "<idle>" <frame>`` reproduces the original delta data::

        new_rot = base_rot * delta_rot         new_pos = base_pos + delta_pos

    The composition is done on quaternions built from the stored-slot euler
    triples via :func:`_stored_euler_to_quaternion`, and the result goes back
    through ``to_euler_angles`` -- the same conversion the raw48/raw64 paths
    use, so blended frames share one convention with the rest of the file.

    A delta that is exactly ``(0, 0, 0)`` copies the base rotation verbatim:
    no round trip, so bones the compiler left untouched (the common case) come
    out byte-identical to the equivalent non-delta sequence.

    ``axis_offset_radians`` optionally tilts the delta by a user-supplied
    ``(x, y, z)`` rotation (added component-wise to the stored delta euler
    before composition) -- the per-sequence "axis adjustment" knob.

    ``frame_lines`` is one frame's output of :func:`calc_animation` (or the
    FRAMEANIM branch); ``base_frame_lines`` is the base animation's frame, in
    the same pre-``adjust_position_and_rotation`` local-bone space.  Bones that
    only exist in one mapping are skipped (a missing delta means "no change").
    """
    has_offset = axis_offset_radians is not None and any(axis_offset_radians)
    for bone_index, a_frame_line in frame_lines.items():
        a_base_line = base_frame_lines.get(bone_index)
        if a_base_line is None:
            continue

        a_frame_line.position.x += a_base_line.position.x
        a_frame_line.position.y += a_base_line.position.y
        a_frame_line.position.z += a_base_line.position.z

        delta_rotation = a_frame_line.rotation
        if has_offset:
            delta_rotation = SourceVector(
                delta_rotation.x + axis_offset_radians[0],
                delta_rotation.y + axis_offset_radians[1],
                delta_rotation.z + axis_offset_radians[2],
            )

        if (
            not has_offset
            and delta_rotation.x == 0
            and delta_rotation.y == 0
            and delta_rotation.z == 0
        ):
            # Identity delta: the stored data equals the base pose, so copy the
            # base euler verbatim instead of round-tripping through a
            # quaternion (which is lossy for gimbal-locked rotations).
            a_frame_line.rotation.x = a_base_line.rotation.x
            a_frame_line.rotation.y = a_base_line.rotation.y
            a_frame_line.rotation.z = a_base_line.rotation.z
            a_frame_line.rotation.debug_text = a_base_line.rotation.debug_text
            continue

        delta_quat = _stored_euler_to_quaternion(delta_rotation)
        base_quat = _stored_euler_to_quaternion(a_base_line.rotation)
        new_quat = _quaternion_multiply(base_quat, delta_quat)
        new_rotation = to_euler_angles(new_quat)

        a_frame_line.rotation.x = new_rotation.x
        a_frame_line.rotation.y = new_rotation.y
        a_frame_line.rotation.z = new_rotation.z
        a_frame_line.rotationQuat = new_quat


#: ``MathModule`` recomputes these per call; the VB expression order is kept.
_PI: Final = 3.1415926535897931


def _degrees_to_radians(degrees: float) -> float:
    """Implement ``MathModule.DegreesToRadians`` -- ``degrees * pi / 180``."""
    return degrees * _PI / 180


def _radians_to_degrees(radians: float) -> float:
    """Implement ``MathModule.RadiansToDegrees`` -- ``radians * 180 / pi``."""
    return radians * 180 / _PI


def _is_populated(a_list) -> bool:
    """Stand-in for the VB ``IsNot Nothing`` tests on the frame-anim lists.

    The Python port initialises ``theBoneConstantInfos`` /
    ``theBoneFrameDataInfos`` to ``[]`` where VB leaves them ``Nothing``, so an
    empty list and ``Nothing`` have to be treated the same way.
    """
    return a_list is not None and len(a_list) > 0


def _dotnet_double_to_string(value: float) -> str:
    """Approximate .NET Framework ``Double.ToString()`` -- the "G15" format.

    Only used to build the ``debug_text`` of the ``...raw64...`` rotation path,
    which is emitted exclusively by the ``DecompileDebugInfoFilesIsChecked``
    option (off by default, and never present in the golden corpus).
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    text = format(value, ".15G")
    if "E" in text:
        mantissa, exponent = text.split("E")
        digits = exponent.lstrip("+-").rjust(2, "0")
        text = mantissa + "E" + ("-" if exponent.startswith("-") else "+") + digits
    return text


# ===========================================================================
# AnimationFrameLine
# ===========================================================================


class AnimationFrameLine:
    """Implement ``AnimationFrameLine`` (plus the ``boneIndex`` the task asks for).

    VB stores ``position`` / ``rotation`` / ``rotationQuat`` and keys the
    ``SortedList`` that holds the frame lines by bone index; the port also keeps
    the key on the object itself so a frame line is self-describing.  The
    ``debug_text`` fields on the two vectors mirror the parser's ``SourceVector``
    instrumentation (only emitted when the debug-info option is on).
    """

    __slots__ = ("boneIndex", "position", "rotation", "rotationQuat")

    def __init__(self) -> None:
        self.boneIndex: int = 0
        self.position: SourceVector = SourceVector()
        self.rotation: SourceVector = SourceVector()
        self.rotationQuat: SourceQuaternion = SourceQuaternion()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AnimationFrameLine(boneIndex={self.boneIndex!r}, "
            f"position={self.position!r}, rotation={self.rotation!r})"
        )


# ===========================================================================
# CalcBoneRotation / CalcBonePosition
# ===========================================================================


def calc_bone_rotation(
    frame_index: int,
    s: float,
    a_bone,
    an_animation: SourceMdlAnimation,
) -> tuple[SourceVector, SourceQuaternion]:
    """Implement ``CalcBoneRotation`` (line 2849).

    ``s`` is never anything but ``0`` in the live code path (``CalcAnimation``
    initialises ``s = 0`` and never changes it), so the sub-frame interpolation
    branches that the VB comments quote from ``bone_setup.cpp`` are dead here
    too -- the VB body ignores ``s`` entirely.

    ``rotationQuat`` is VB ``ByRef``; it is returned as the second tuple element
    instead of being written through a mutable argument.
    """
    rot = SourceQuaternion()
    angle_vector = SourceVector()
    rotation_quat = SourceQuaternion()

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_RAWROT) > 0:
        rot.x = an_animation.theRot48bits.x
        rot.y = an_animation.theRot48bits.y
        rot.z = an_animation.theRot48bits.z
        rot.w = an_animation.theRot48bits.w
        rotation_quat.x = rot.x
        rotation_quat.y = rot.y
        rotation_quat.z = rot.z
        rotation_quat.w = rot.w
        angle_vector = to_euler_angles(rot)
        angle_vector.debug_text = "raw48"
        return angle_vector, rotation_quat

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_RAWROT2) > 0:
        rot.x = an_animation.theRot64bits.x
        rot.y = an_animation.theRot64bits.y
        rot.z = an_animation.theRot64bits.z
        rot.w = an_animation.theRot64bits.w
        rotation_quat.x = rot.x
        rotation_quat.y = rot.y
        rotation_quat.z = rot.z
        rotation_quat.w = rot.w
        angle_vector = to_euler_angles(rot)
        # NOTE: this mirrors the VB debug text, whose four component values go
        # through the default (culture-dependent) ``Double.ToString()``.
        angle_vector.debug_text = (
            "raw64 ("
            + _dotnet_double_to_string(rot.x)
            + ", "
            + _dotnet_double_to_string(rot.y)
            + ", "
            + _dotnet_double_to_string(rot.z)
            + ", "
            + _dotnet_double_to_string(rot.w)
            + ")"
        )
        return angle_vector, rotation_quat

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_ANIMROT) == 0:
        if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_DELTA) > 0:
            angle_vector.x = 0
            angle_vector.y = 0
            angle_vector.z = 0
            rotation_quat.x = 0
            rotation_quat.y = 0
            rotation_quat.z = 0
            rotation_quat.w = 0
            angle_vector.debug_text = "delta"
        else:
            angle_vector.x = a_bone.rotation.x
            angle_vector.y = a_bone.rotation.y
            angle_vector.z = a_bone.rotation.z
            rotation_quat.x = 0
            rotation_quat.y = 0
            rotation_quat.z = 0
            rotation_quat.w = 0
            angle_vector.debug_text = "bone"
        return angle_vector, rotation_quat

    rot_v = an_animation.theRotV

    if rot_v.animXValueOffset <= 0:
        angle_vector.x = 0
    else:
        angle_vector.x = extract_anim_value(
            frame_index, rot_v.theAnimXValues, a_bone.rotationScale.x
        )
    if rot_v.animYValueOffset <= 0:
        angle_vector.y = 0
    else:
        angle_vector.y = extract_anim_value(
            frame_index, rot_v.theAnimYValues, a_bone.rotationScale.y
        )
    if rot_v.animZValueOffset <= 0:
        angle_vector.z = 0
    else:
        angle_vector.z = extract_anim_value(
            frame_index, rot_v.theAnimZValues, a_bone.rotationScale.z
        )

    angle_vector.debug_text = "anim"

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_DELTA) == 0:
        angle_vector.x += a_bone.rotation.x
        angle_vector.y += a_bone.rotation.y
        angle_vector.z += a_bone.rotation.z
        angle_vector.debug_text += "+bone"

    rotation_quat = euler_angles_to_quaternion(angle_vector)
    return angle_vector, rotation_quat


def calc_bone_position(
    frame_index: int,
    s: float,
    a_bone,
    an_animation: SourceMdlAnimation,
) -> SourceVector:
    """Implement ``CalcBonePosition`` (line 3004).  ``s`` is unused, as in the VB."""
    pos = SourceVector()

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_RAWPOS) > 0:
        pos.x = an_animation.thePos.x
        pos.y = an_animation.thePos.y
        pos.z = an_animation.thePos.z
        pos.debug_text = "raw"
        return pos

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_ANIMPOS) == 0:
        if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_DELTA) > 0:
            pos.x = 0
            pos.y = 0
            pos.z = 0
            pos.debug_text = "delta"
        else:
            pos.x = a_bone.position.x
            pos.y = a_bone.position.y
            pos.z = a_bone.position.z
            pos.debug_text = "bone"
        return pos

    pos_v = an_animation.thePosV

    if pos_v.animXValueOffset <= 0:
        pos.x = 0
    else:
        pos.x = extract_anim_value(
            frame_index, pos_v.theAnimXValues, a_bone.positionScale.x
        )

    if pos_v.animYValueOffset <= 0:
        pos.y = 0
    else:
        pos.y = extract_anim_value(
            frame_index, pos_v.theAnimYValues, a_bone.positionScale.y
        )

    if pos_v.animZValueOffset <= 0:
        pos.z = 0
    else:
        pos.z = extract_anim_value(
            frame_index, pos_v.theAnimZValues, a_bone.positionScale.z
        )

    pos.debug_text = "anim"

    if (an_animation.flags & SourceMdlAnimation.STUDIO_ANIM_DELTA) == 0:
        pos.x += a_bone.position.x
        pos.y += a_bone.position.y
        pos.z += a_bone.position.z
        pos.debug_text += "+bone"

    return pos


# ===========================================================================
# CalcAnimation
# ===========================================================================


def calc_animation(
    mdl_file_data: SourceMdlFileData49,
    a_sequence_desc: Optional[SourceMdlSequenceDesc],
    an_animation_desc: SourceMdlAnimationDesc49,
    frame_index: int,
    only_write_corrective_animation_root_bones: bool = False,
) -> dict[int, AnimationFrameLine]:
    """Implement ``CalcAnimation`` (line 2421).

    Returns the per-bone frame lines that ``WriteSkeletonSectionForAnimation``
    would have found in ``Me.theAnimationFrameLines`` -- the equivalent of the
    VB ``SortedList(Of Integer, AnimationFrameLine)`` (walk it with
    ``sorted(...)``, which is what a ``SortedList`` indexer does).

    ``a_sequence_desc`` may be ``None``: that is what
    ``WriteBoneAnimationSmdFiles`` and ``CalculateFirstAnimDescFrameLinesForPhysics``
    both pass, and it makes every bone weight ``1``.

    ``only_write_corrective_animation_root_bones`` is the VB optional parameter of
    the same name (``Optional ... As Boolean = False``).
    """
    s = 0.0

    anim_index = 0

    # sectionIndex / sectionFrameIndex select the animation section this frame
    # lives in.  VB: CInt(Math.Truncate(frameIndex / anAnimationDesc.sectionFrameCount)).
    if an_animation_desc.sectionFrameCount == 0:
        section_index = 0
        section_frame_index = frame_index
    else:
        section_index = int(
            math.trunc(frame_index / an_animation_desc.sectionFrameCount)
        )
        section_frame_index = frame_index - (
            section_index * an_animation_desc.sectionFrameCount
        )

    an_animation: Optional[SourceMdlAnimation] = None
    a_section_of_animation: Optional[list[SourceMdlAnimation]] = None
    if an_animation_desc.theSectionsOfAnimations is not None:
        a_section_of_animation = an_animation_desc.theSectionsOfAnimations[section_index]
        if 0 <= anim_index < len(a_section_of_animation):
            an_animation = a_section_of_animation[anim_index]

    the_animation_frame_lines: dict[int, AnimationFrameLine] = {}

    bones = mdl_file_data.theBones
    if bones is None:
        return the_animation_frame_lines

    for bone_index in range(len(bones)):
        a_bone = bones[bone_index]

        if (
            only_write_corrective_animation_root_bones
            and a_bone.parentBoneIndex != -1
        ):
            continue

        if a_sequence_desc is not None:
            a_weight = a_sequence_desc.theBoneWeights[bone_index]
        else:
            # NOTE: This should only be needed for a delta $animation.  Arbitrarily
            #       assign 1 so that the following code will add frame lines for
            #       this $animation.
            a_weight = 1

        if an_animation is not None and an_animation.boneIndex == bone_index:
            if a_weight > 0:
                a_frame_line = _get_or_add_frame_line(
                    the_animation_frame_lines, bone_index
                )

                a_frame_line.rotationQuat = SourceQuaternion()
                rot, rotation_quat = calc_bone_rotation(
                    section_frame_index, s, a_bone, an_animation
                )
                a_frame_line.rotationQuat = rotation_quat
                a_frame_line.rotation = SourceVector()
                a_frame_line.rotation.x = rot.x
                a_frame_line.rotation.y = rot.y
                a_frame_line.rotation.z = rot.z
                a_frame_line.rotation.debug_text = rot.debug_text

                pos = calc_bone_position(section_frame_index, s, a_bone, an_animation)
                a_frame_line.position = SourceVector()
                a_frame_line.position.x = pos.x
                a_frame_line.position.y = pos.y
                a_frame_line.position.z = pos.z
                a_frame_line.position.debug_text = pos.debug_text

            anim_index += 1
            if anim_index >= len(a_section_of_animation):
                an_animation = None
            else:
                an_animation = a_section_of_animation[anim_index]

        elif a_weight > 0:
            a_frame_line = _get_or_add_frame_line(the_animation_frame_lines, bone_index)

            # NOTE: Changed in v0.25 -- the delta test is on the *animdesc* flags.
            if (an_animation_desc.flags & SourceMdlAnimationDesc49.STUDIO_DELTA) > 0:
                a_frame_line.rotation = SourceVector()
                a_frame_line.rotation.x = 0
                a_frame_line.rotation.y = 0
                a_frame_line.rotation.z = 0
                a_frame_line.rotation.debug_text = "desc_delta"

                a_frame_line.position = SourceVector()
                a_frame_line.position.x = 0
                a_frame_line.position.y = 0
                a_frame_line.position.z = 0
                a_frame_line.position.debug_text = "desc_delta"
            else:
                a_frame_line.rotation = SourceVector()
                a_frame_line.rotation.x = a_bone.rotation.x
                a_frame_line.rotation.y = a_bone.rotation.y
                a_frame_line.rotation.z = a_bone.rotation.z
                a_frame_line.rotation.debug_text = "desc_bone"

                a_frame_line.position = SourceVector()
                a_frame_line.position.x = a_bone.position.x
                a_frame_line.position.y = a_bone.position.y
                a_frame_line.position.z = a_bone.position.z
                a_frame_line.position.debug_text = "desc_bone"

    return the_animation_frame_lines


def _get_or_add_frame_line(
    the_animation_frame_lines: dict[int, AnimationFrameLine], bone_index: int
) -> AnimationFrameLine:
    """Implement the ``ContainsKey`` / ``Add`` pair used all over ``CalcAnimation``."""
    a_frame_line = the_animation_frame_lines.get(bone_index)
    if a_frame_line is None:
        a_frame_line = AnimationFrameLine()
        a_frame_line.boneIndex = bone_index
        the_animation_frame_lines[bone_index] = a_frame_line
    return a_frame_line


# ===========================================================================
# Frame-line adjustment (bone pose -> SMD pose)
# ===========================================================================


def adjust_position_and_rotation(
    mdl_file_data: SourceMdlFileData49,
    bone_index: int,
    i_position: SourceVector,
    i_rotation: SourceVector,
    this_is_for_first_sequence: bool,
) -> tuple[SourceVector, SourceVector]:
    """Implement ``AdjustPositionAndRotation`` (line 983).

    VB's two ``ByRef`` outputs become the returned tuple.  Both branches assign
    all three components of both vectors, so the caller never sees a partial
    value.
    """
    up_axis_y_command_was_used = bool(
        getattr(mdl_file_data, "theUpAxisYCommandWasUsed", False)
    )

    a_bone = mdl_file_data.theBones[bone_index]

    o_position = SourceVector()
    o_rotation = SourceVector()

    if a_bone.parentBoneIndex == -1:
        if this_is_for_first_sequence and up_axis_y_command_was_used:
            o_position.x = i_position.y
            o_position.y = i_position.z
            o_position.z = i_position.x
        else:
            o_position.x = i_position.y
            o_position.y = -i_position.x
            o_position.z = i_position.z
    else:
        o_position.x = i_position.x
        o_position.y = i_position.y
        o_position.z = i_position.z

    if a_bone.parentBoneIndex == -1:
        if this_is_for_first_sequence and up_axis_y_command_was_used:
            o_rotation.x = i_rotation.x + _degrees_to_radians(-90)
            o_rotation.y = i_rotation.y
            o_rotation.z = i_rotation.z + _degrees_to_radians(-90)
        else:
            o_rotation.x = i_rotation.x
            o_rotation.y = i_rotation.y
            o_rotation.z = i_rotation.z + _degrees_to_radians(-90)
    else:
        o_rotation.x = i_rotation.x
        o_rotation.y = i_rotation.y
        o_rotation.z = i_rotation.z

    return o_position, o_rotation


def adjust_position_and_rotation_by_piecewise_movement(
    mdl_file_data: SourceMdlFileData49,
    frame_index: int,
    bone_index: int,
    movements,
    i_position: SourceVector,
    i_rotation: SourceVector,
) -> tuple[SourceVector, SourceVector]:
    """Implement ``AdjustPositionAndRotationByPiecewiseMovement`` (line 1460).

    Only root bones are touched, and only from frame 1 onwards.  The live path is
    the last one in the VB source (everything above it is commented out): the
    motion's ``d`` scale is applied to the movement *vector*, the accumulated
    movement position is added, and the input position is yaw-rotated by the
    movement angle before the translation is added.  ``oRotation.z`` gains the
    movement angle in degrees (round-tripped through degrees, exactly as the VB
    does).

    ``movements`` may be ``None`` (``anAnimationDesc.theMovements`` is only
    populated when ``movementCount > 0``).
    """
    a_bone = mdl_file_data.theBones[bone_index]

    o_position = i_position.copy()
    o_position.debug_text = i_position.debug_text
    o_rotation = i_rotation.copy()
    o_rotation.debug_text = i_rotation.debug_text

    if a_bone.parentBoneIndex != -1:
        return o_position, o_rotation

    if movements is None or frame_index <= 0:
        return o_position, o_rotation

    previous_end_frame_index = 0
    vec_pos = SourceVector()
    vec_angle = SourceVector()

    for a_movement in movements:
        if frame_index <= a_movement.endframeIndex:
            f = (frame_index - previous_end_frame_index) / (
                a_movement.endframeIndex - previous_end_frame_index
            )
            d = a_movement.v0 * f + 0.5 * (a_movement.v1 - a_movement.v0) * f * f
            vec_pos.x = vec_pos.x + d * a_movement.vector.x
            vec_pos.y = vec_pos.y + d * a_movement.vector.y
            vec_pos.z = vec_pos.z + d * a_movement.vector.z
            vec_angle.y = vec_angle.y * (1 - f) + a_movement.angle * f
            break
        else:
            previous_end_frame_index = a_movement.endframeIndex
            vec_pos.x = a_movement.position.x
            vec_pos.y = a_movement.position.y
            vec_pos.z = a_movement.position.z
            vec_angle.y = a_movement.angle

    o_rotation.z = _degrees_to_radians(_radians_to_degrees(i_rotation.z) + vec_angle.y)
    tmp = vector_yaw_rotate(i_position, _degrees_to_radians(vec_angle.y))
    o_position.x = tmp.x + vec_pos.x
    o_position.y = tmp.y + vec_pos.y
    o_position.z = tmp.z + vec_pos.z

    return o_position, o_rotation


# ===========================================================================
# Physics
# ===========================================================================


def calculate_first_anim_desc_frame_lines_for_physics(
    mdl_file_data: SourceMdlFileData49,
) -> AnimationFrameLine:
    """Implement ``CalculateFirstAnimDescFrameLinesForPhysics`` (line 1674).

    Runs ``CalcAnimation(Nothing, theAnimationDescs(0), 0)`` and returns the frame
    line of its **first** entry -- i.e. the lowest bone index, which a
    ``SortedList`` indexer yields.  ``loka.core.phy`` feeds the result to
    the single-solid world-to-pose transform.
    """
    an_animation_desc = mdl_file_data.theAnimationDescs[0]

    the_animation_frame_lines = calc_animation(
        mdl_file_data, None, an_animation_desc, 0
    )

    a_frame_line = the_animation_frame_lines[sorted(the_animation_frame_lines)[0]]

    a_first_animation_desc_frame_line = AnimationFrameLine()
    a_first_animation_desc_frame_line.rotation = SourceVector()
    a_first_animation_desc_frame_line.position = SourceVector()

    a_first_animation_desc_frame_line.rotation.x = a_frame_line.rotation.x
    a_first_animation_desc_frame_line.rotation.y = a_frame_line.rotation.y
    a_first_animation_desc_frame_line.rotation.z = a_frame_line.rotation.z

    a_first_animation_desc_frame_line.position.x = a_frame_line.position.x
    a_first_animation_desc_frame_line.position.y = a_frame_line.position.y
    a_first_animation_desc_frame_line.position.z = a_frame_line.position.z

    return a_first_animation_desc_frame_line


# ===========================================================================
# WriteSkeletonSectionForAnimation
# ===========================================================================


def write_skeleton_section_for_animation(
    write_line: Callable[[str], None],
    mdl_file_data: SourceMdlFileData49,
    a_sequence_desc: Optional[SourceMdlSequenceDesc],
    an_animation_desc: SourceMdlAnimationDesc49,
    only_write_corrective_animation_root_bones: bool = False,
    *,
    stricter_format: bool = False,
    write_debug_info: bool = False,
    subtract_base_frame_lines: Optional[dict[int, AnimationFrameLine]] = None,
    subtract_axis_offset_radians: Optional[tuple[float, float, float]] = None,
) -> None:
    """Implement ``WriteSkeletonSectionForAnimation`` (line 387).

    Writes the ``skeleton`` / ``time <n>`` / per-bone ``position rotation``
    section of a bone-animation SMD.  ``write_line(text)`` receives one line
    without a line terminator; the caller owns encoding and line endings (see
    :class:`loka.core.writers.smd.SmdFile49`).

    ``only_write_corrective_animation_root_bones`` is the VB ``Optional``
    parameter: the corrective-animation SMDs are written with it ``True``, which
    forces a single frame and zeroes the positions.  ``a_sequence_desc`` is
    ``None`` for every bone-animation SMD (and for the corrective ones);
    ``theSequenceDescs`` is normally only set for a sequence pass.

    ``subtract_base_frame_lines`` is the subtract-correction extension: when
    given (the base idle animation's frame 0, from :func:`calc_animation`),
    every frame line gets that pose added back on top of the stored delta via
    :func:`add_subtract_base_pose`, so the SMD holds the full animation and the
    QC's ``subtract "<idle>" 0`` re-applies the delta at compile time.
    """
    sequence_descs = getattr(mdl_file_data, "theSequenceDescs", None)

    # VB: (anAnimationDesc.theName = "" OrElse anAnimationDesc.theName(0) = "@")
    #     AndAlso anAnimationDesc.theAnimIsLinkedToSequence
    #     AndAlso (anAnimationDesc.theLinkedSequences(0) Is theSequenceDescs(0))
    # ``AndAlso`` short-circuits, and the linked-sequence fields are only ever set
    # by the unported ReadSequenceDescs, so this stays False for the whole corpus.
    this_is_for_first_sequence = False
    name = an_animation_desc.theName
    if (name == "" or name[0] == "@") and an_animation_desc.theAnimIsLinkedToSequence:
        this_is_for_first_sequence = (
            an_animation_desc.theLinkedSequences is not None
            and sequence_descs is not None
            and len(an_animation_desc.theLinkedSequences) > 0
            and len(sequence_descs) > 0
            and an_animation_desc.theLinkedSequences[0] is sequence_descs[0]
        )

    # 'skeleton
    write_line("skeleton")

    if mdl_file_data.theBones is not None:
        frame_count = an_animation_desc.frameCount
        if only_write_corrective_animation_root_bones:
            frame_count = 1

        for frame_index in range(frame_count):
            # Me.theAnimationFrameLines.Clear() -- a fresh mapping per frame.
            the_animation_frame_lines: dict[int, AnimationFrameLine] = {}

            if (an_animation_desc.flags & SourceMdlAnimationDesc49.STUDIO_FRAMEANIM) != 0:
                if an_animation_desc.sectionFrameCount == 0:
                    section_index = 0
                    section_frame_index = frame_index
                else:
                    section_index = int(
                        math.trunc(frame_index / an_animation_desc.sectionFrameCount)
                    )
                    section_frame_index = frame_index - (
                        section_index * an_animation_desc.sectionFrameCount
                    )
                a_section_of_animation: SourceAniFrameAnim49 = (
                    an_animation_desc.theSectionsOfFrameAnim[section_index]
                )

                for bone_index in range(len(mdl_file_data.theBones)):
                    a_bone = mdl_file_data.theBones[bone_index]

                    if (
                        only_write_corrective_animation_root_bones
                        and a_bone.parentBoneIndex != -1
                    ):
                        continue

                    a_frame_line = AnimationFrameLine()
                    a_frame_line.boneIndex = bone_index
                    the_animation_frame_lines[bone_index] = a_frame_line

                    a_frame_line.position = SourceVector()
                    a_frame_line.rotation = SourceVector()

                    # NOTE: The ZERO path works for L4D2 v_huntingrifle,
                    #       v_snip_awp, v_snip_scout; the BONE path works for L4D2
                    #       common_male_carexit.  The DELTA test picks between them.
                    if (an_animation_desc.flags & SourceMdlAnimationDesc49.STUDIO_DELTA) > 0:
                        a_frame_line.position.x = 0
                        a_frame_line.position.y = 0
                        a_frame_line.position.z = 0
                        a_frame_line.position.debug_text = "ZERO"
                        a_frame_line.rotation.x = 0
                        a_frame_line.rotation.y = 0
                        a_frame_line.rotation.z = 0
                        a_frame_line.rotation.debug_text = "ZERO"
                    else:
                        a_frame_line.position.x = a_bone.position.x
                        a_frame_line.position.y = a_bone.position.y
                        a_frame_line.position.z = a_bone.position.z
                        a_frame_line.position.debug_text = "BONE"
                        a_frame_line.rotation.x = a_bone.rotation.x
                        a_frame_line.rotation.y = a_bone.rotation.y
                        a_frame_line.rotation.z = a_bone.rotation.z
                        a_frame_line.rotation.debug_text = "BONE"

                    bone_flag = a_section_of_animation.theBoneFlags[bone_index]

                    if _is_populated(a_section_of_animation.theBoneConstantInfos):
                        a_bone_constant_info = (
                            a_section_of_animation.theBoneConstantInfos[bone_index]
                        )
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_RAWROT
                        ) > 0:
                            a_frame_line.rotation = to_euler_angles(
                                a_bone_constant_info.theConstantRawRot.quaternion
                            )
                            a_frame_line.rotation.debug_text = "RAWROT"
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_RAWPOS
                        ) > 0:
                            a_frame_line.position.x = (
                                a_bone_constant_info.theConstantRawPos.x
                            )
                            a_frame_line.position.y = (
                                a_bone_constant_info.theConstantRawPos.y
                            )
                            a_frame_line.position.z = (
                                a_bone_constant_info.theConstantRawPos.z
                            )
                            a_frame_line.position.debug_text = "RAWPOS"
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_CONST_POS2
                        ) > 0:
                            a_frame_line.position.x = (
                                a_bone_constant_info.theConstantPosition2.x
                            )
                            a_frame_line.position.y = (
                                a_bone_constant_info.theConstantPosition2.y
                            )
                            a_frame_line.position.z = (
                                a_bone_constant_info.theConstantPosition2.z
                            )
                            a_frame_line.position.debug_text = "FRAME_CONST_POS2"
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_CONST_ROT2
                        ) > 0:
                            a_frame_line.rotation = to_euler_angles(
                                a_bone_constant_info.theConstantRotation2.quaternion
                            )
                            a_frame_line.rotation.debug_text = "FRAME_CONST_ROT2"

                    if _is_populated(a_section_of_animation.theBoneFrameDataInfos):
                        a_bone_frame_data_info = (
                            a_section_of_animation.theBoneFrameDataInfos[
                                section_frame_index
                            ][bone_index]
                        )
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_ANIMROT
                        ) > 0:
                            a_frame_line.rotation = to_euler_angles(
                                a_bone_frame_data_info.theAnimRotation.quaternion
                            )
                            a_frame_line.rotation.debug_text = "ANIMROT"
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_ANIMPOS
                        ) > 0:
                            a_frame_line.position.x = (
                                a_bone_frame_data_info.theAnimPosition.x
                            )
                            a_frame_line.position.y = (
                                a_bone_frame_data_info.theAnimPosition.y
                            )
                            a_frame_line.position.z = (
                                a_bone_frame_data_info.theAnimPosition.z
                            )
                            a_frame_line.position.debug_text = "ANIMPOS"
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_FULLANIMPOS
                        ) > 0:
                            a_frame_line.position.x = (
                                a_bone_frame_data_info.theFullAnimPosition.x
                            )
                            a_frame_line.position.y = (
                                a_bone_frame_data_info.theFullAnimPosition.y
                            )
                            a_frame_line.position.z = (
                                a_bone_frame_data_info.theFullAnimPosition.z
                            )
                            a_frame_line.position.debug_text = "FULLANIMPOS"
                        # NOTE: (boneFlag And &H20) > 0 is tested by the VB but its
                        #       body only assigns an unused debug local.
                        if (
                            bone_flag & SourceAniFrameAnim49.STUDIO_FRAME_ANIM_ROT2
                        ) > 0:
                            a_frame_line.rotation = to_euler_angles(
                                a_bone_frame_data_info.theAnimRotationUnknown.quaternion
                            )
                            a_frame_line.rotation.debug_text = "FRAME_ANIM_ROT2"
            else:
                the_animation_frame_lines = calc_animation(
                    mdl_file_data,
                    a_sequence_desc,
                    an_animation_desc,
                    frame_index,
                    only_write_corrective_animation_root_bones,
                )

            if subtract_base_frame_lines:
                add_subtract_base_pose(
                    the_animation_frame_lines,
                    subtract_base_frame_lines,
                    axis_offset_radians=subtract_axis_offset_radians,
                )

            if stricter_format:
                write_line("time " + fmt_int(frame_index))
            else:
                write_line("  time " + fmt_int(frame_index))

            for bone_index in sorted(the_animation_frame_lines):
                a_frame_line = the_animation_frame_lines[bone_index]

                adjusted_position, adjusted_rotation = (
                    adjust_position_and_rotation_by_piecewise_movement(
                        mdl_file_data,
                        frame_index,
                        bone_index,
                        an_animation_desc.theMovements,
                        a_frame_line.position,
                        a_frame_line.rotation,
                    )
                )
                position, rotation = adjust_position_and_rotation(
                    mdl_file_data,
                    bone_index,
                    adjusted_position,
                    adjusted_rotation,
                    this_is_for_first_sequence,
                )

                if only_write_corrective_animation_root_bones:
                    position.x = 0
                    position.y = 0
                    position.z = 0

                line = "    "
                line += fmt_int(bone_index)

                line += " "
                line += fmt_fixed6(position.x)
                line += " "
                line += fmt_fixed6(position.y)
                line += " "
                line += fmt_fixed6(position.z)

                line += " "
                line += fmt_fixed6(rotation.x)
                line += " "
                line += fmt_fixed6(rotation.y)
                line += " "
                line += fmt_fixed6(rotation.z)

                if write_debug_info:
                    line += "   # "
                    line += "pos: "
                    line += a_frame_line.position.debug_text
                    line += "   "
                    line += "rot: "
                    line += a_frame_line.rotation.debug_text

                write_line(line)

    write_line("end")
