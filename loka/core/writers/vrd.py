"""Write procedural bone definitions in VRD format."""

from __future__ import annotations

import math
from typing import Optional

from ..mdl_data import SourceMdlAttachment, SourceMdlBone, SourceMdlFileData49
from ..vectors import SourceVector, radians_to_degrees
from .text import (
    DEFAULT_PRODUCT_NAME,
    DEFAULT_VERSION,
    WriteLine,
    fmt_trim,
    join_fields,
    write_header_comment as _write_header_comment,
)

__all__ = [
    "STUDIO_PROC_AXISINTERP",
    "STUDIO_PROC_QUATINTERP",
    "STUDIO_PROC_AIMATBONE",
    "STUDIO_PROC_AIMATATTACH",
    "STUDIO_PROC_JIGGLE",
    "FLT_EPSILON",
    "SourceVrdFile49",
    "remove_upto_and_including_first_dot_character_from_string",
    "to_euler_angles",
    "eul_from_quat",
    "eul_from_h_matrix",
    "get_vrd_file_name",
]

#: ``SourceMdlBone.STUDIO_PROC_*``.
STUDIO_PROC_AXISINTERP = SourceMdlBone.STUDIO_PROC_AXISINTERP
STUDIO_PROC_QUATINTERP = SourceMdlBone.STUDIO_PROC_QUATINTERP
STUDIO_PROC_AIMATBONE = SourceMdlBone.STUDIO_PROC_AIMATBONE
STUDIO_PROC_AIMATATTACH = SourceMdlBone.STUDIO_PROC_AIMATATTACH
STUDIO_PROC_JIGGLE = SourceMdlBone.STUDIO_PROC_JIGGLE

#: ``Private Const FLT_EPSILON As Double = 0.00001``.
FLT_EPSILON = 0.00001

#: ``EulerParity.Even`` / ``EulerRepeat.No`` / ``EulerFrame.S`` -- the only
#: combination ``ToEulerAngles`` ever requests.
_EULER_PARITY_EVEN = 0
_EULER_PARITY_ODD = 1
_EULER_FRAME_S = 0
_EULER_FRAME_R = 1
_EULER_REPEAT_NO = 0
_EULER_REPEAT_YES = 1


# ---------------------------------------------------------------------------
# MathModule ports used by the writer
# ---------------------------------------------------------------------------

def remove_upto_and_including_first_dot_character_from_string(s: str) -> str:
    """Implement ``StringClass.RemoveUptoAndIncludingFirstDotCharacterFromString``.

    ``"ValveBiped.Bip01_L_Elbow"`` -> ``"Bip01_L_Elbow"``; a name without a dot is
    returned unchanged.  Line 52 of ``SourceVrdFile49`` documents the rule:
    *"A bone name in a VRD file must have its characters up to and including the
    first dot removed."*
    """
    position_of_first_dot = s.find(".")
    if position_of_first_dot >= 0:
        return s[position_of_first_dot + 1 :]
    return s


def eul_from_h_matrix(
    matrix: list[list[float]],
    i: int,
    j: int,
    k: int,
    h: int,  # noqa: ARG001 - kept for signature parity with the VB original
    parity: int = _EULER_PARITY_EVEN,
    repeat: int = _EULER_REPEAT_NO,
    frame: int = _EULER_FRAME_S,
) -> SourceVector:
    """Implement ``MathModule.Eul_FromHMatrix``.

    ``h`` is unused in the VB original too (it is a leftover of the Ken Shoemake
    ``Eul_FromHMatrix`` it was copied from).
    """
    ea = SourceVector()

    if repeat == _EULER_REPEAT_YES:
        sy = math.sqrt(matrix[i][j] * matrix[i][j] + matrix[i][k] * matrix[i][k])
        if sy > 16 * FLT_EPSILON:
            ea.x = math.atan2(matrix[i][j], matrix[i][k])
            ea.y = math.atan2(sy, matrix[i][i])
            ea.z = math.atan2(matrix[j][i], -matrix[k][i])
        else:
            ea.x = math.atan2(-matrix[j][k], matrix[j][j])
            ea.y = math.atan2(sy, matrix[i][i])
            ea.z = 0.0
    else:
        cy = math.sqrt(matrix[i][i] * matrix[i][i] + matrix[j][i] * matrix[j][i])
        if cy > 16 * FLT_EPSILON:
            ea.x = math.atan2(matrix[k][j], matrix[k][k])
            ea.y = math.atan2(-matrix[k][i], cy)
            ea.z = math.atan2(matrix[j][i], matrix[i][i])
        else:
            ea.x = math.atan2(-matrix[j][k], matrix[j][j])
            ea.y = math.atan2(-matrix[k][i], cy)
            ea.z = 0.0

    if parity == _EULER_PARITY_ODD:
        ea.x = -ea.x
        ea.y = -ea.y
        ea.z = -ea.z

    if frame == _EULER_FRAME_R:
        ea.x, ea.z = ea.z, ea.x

    # ``ea.w = order`` is commented out in the VB original.
    return ea


def eul_from_quat(
    q: SourceVector,  # any object exposing .x/.y/.z/.w
    i: int = 0,
    j: int = 1,
    k: int = 2,
    h: int = 0,
    parity: int = _EULER_PARITY_EVEN,
    repeat: int = _EULER_REPEAT_NO,
    frame: int = _EULER_FRAME_S,
) -> SourceVector:
    """Implement ``MathModule.Eul_FromQuat``.

    The return value's ``w`` is always ``0`` because only ``x``, ``y`` and ``z``
    are assigned -- matching the VB original.
    """
    nq = q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w
    s = 2.0 / nq if nq > 0 else 0.0

    xs = q.x * s
    ys = q.y * s
    zs = q.z * s

    wx = q.w * xs
    wy = q.w * ys
    wz = q.w * zs
    xx = q.x * xs
    xy = q.x * ys
    xz = q.x * zs
    yy = q.y * ys
    yz = q.y * zs
    zz = q.z * zs

    # Dim M(,) As Double = {{0,0,0,0}, ...} -- a 4x4 zero matrix, row major.
    matrix = [[0.0] * 4 for _ in range(4)]
    matrix[0][0] = 1.0 - (yy + zz)
    matrix[0][1] = xy - wz
    matrix[0][2] = xz + wy
    matrix[1][0] = xy + wz
    matrix[1][1] = 1.0 - (xx + zz)
    matrix[1][2] = yz - wx
    matrix[2][0] = xz - wy
    matrix[2][1] = yz + wx
    matrix[2][2] = 1.0 - (xx + yy)
    matrix[3][3] = 1.0

    return eul_from_h_matrix(matrix, i, j, k, h, parity, repeat, frame)


def to_euler_angles(q: SourceVector) -> SourceVector:
    """Implement ``MathModule.ToEulerAngles`` -- ``Eul_FromQuat(q, 0, 1, 2, 0, Even, No, S)``.

    Returns Euler angles in **radians**; the caller converts with
    :func:`~loka.core.vectors.radians_to_degrees`.
    """
    return eul_from_quat(
        q,
        0,
        1,
        2,
        0,
        _EULER_PARITY_EVEN,
        _EULER_REPEAT_NO,
        _EULER_FRAME_S,
    )


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def get_vrd_file_name(model_name: str) -> str:
    """Implement ``SourceFileNamesModule.GetVrdFileName`` -- ``<model>.vrd``."""
    return model_name + ".vrd"


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

class SourceVrdFile49:
    """Implement ``SourceVrdFile49``.

    ``write_line`` is the parser's ``StreamWriter.WriteLine`` -- pass a
    :class:`~loka.core.writers.text.TextWriter` (or any object with a
    ``write_line(str)`` method / any ``str -> None`` callable).

    ``stricter_format`` mirrors ``TheApp.Settings.DecompileStricterFormatIsChecked``;
    it only suppresses the header comment, nothing else.
    """

    def __init__(
        self,
        write_line: WriteLine,
        mdl_file_data: SourceMdlFileData49,
        *,
        stricter_format: bool = False,
        product_name: Optional[str] = None,
        version: Optional[tuple[int, ...]] = None,
    ) -> None:
        if not callable(write_line):
            # Accept a TextWriter-like object as a convenience.
            write_line = write_line.write_line
        self.theOutputFileStreamWriter: WriteLine = write_line
        self.theMdlFileData = mdl_file_data
        self.theStricterFormatIsChecked = stricter_format
        self.theProductName = product_name
        self.theVersion = version

    # -- header --------------------------------------------------------------
    def write_header_comment(self) -> None:
        """Implement ``SourceVrdFile49.WriteHeaderComment`` (line 16-18).

        Delegates to ``Common.WriteHeaderComment`` -- there is deliberately no
        ``version`` line after it.
        """
        _write_header_comment(
            self.theOutputFileStreamWriter,
            self.theStricterFormatIsChecked,
            self.theProductName if self.theProductName is not None else DEFAULT_PRODUCT_NAME,
            self.theVersion if self.theVersion is not None else DEFAULT_VERSION,
        )

    # -- commands ------------------------------------------------------------
    def write_commands(self) -> None:
        """Implement ``SourceVrdFile49.WriteCommands`` (lines 20-209)."""
        mdl_file_data = self.theMdlFileData
        if mdl_file_data.theBones is None:
            return

        # ``For i As Integer = 0 To Me.theMdlFileData.theBones.Count - 1``
        for i in range(len(mdl_file_data.theBones)):
            a_bone = mdl_file_data.theBones[i]

            if a_bone.proceduralRuleOffset == 0:
                continue

            if a_bone.proceduralRuleType == STUDIO_PROC_AXISINTERP:
                # Line 39: the VB ``If`` branch is empty -- the axis-interp text
                # "seems to be in a different file than the VRD file".
                pass
            elif a_bone.proceduralRuleType == STUDIO_PROC_QUATINTERP:
                self._write_quat_interp_bone(a_bone)
            elif a_bone.proceduralRuleType in (STUDIO_PROC_AIMATBONE, STUDIO_PROC_AIMATATTACH):
                self._write_aim_at_bone(a_bone)

    # -- private methods (named after the VB inline blocks) ------------------
    def _write_quat_interp_bone(self, a_bone: SourceMdlBone) -> None:
        """Lines 41-152: blank line, ``<helper>``, ``<basepos> 0 0 0``, N x ``<trigger>``."""
        bones = self.theMdlFileData.theBones
        assert bones is not None  # guarded by write_commands

        a_parent_bone = bones[a_bone.parentBoneIndex]
        a_control_bone = bones[a_bone.theQuatInterpBone.controlBoneIndex]
        a_parent_control_bone = bones[a_control_bone.parentBoneIndex]

        a_bone_name = remove_upto_and_including_first_dot_character_from_string(a_bone.theName)
        a_parent_bone_name = remove_upto_and_including_first_dot_character_from_string(
            a_parent_bone.theName
        )
        a_parent_control_bone_name = remove_upto_and_including_first_dot_character_from_string(
            a_parent_control_bone.theName
        )
        a_control_bone_name = remove_upto_and_including_first_dot_character_from_string(
            a_control_bone.theName
        )

        # Line 62: WriteLine() with no argument -- the blank line that precedes
        # every <helper> block.  NOTE the order of the four names: bone, parent
        # bone, parent *of the control* bone, then the control bone itself.
        self.theOutputFileStreamWriter("")
        self.theOutputFileStreamWriter(
            join_fields(
                "<helper>",
                (
                    a_bone_name,
                    a_parent_bone_name,
                    a_parent_control_bone_name,
                    a_control_bone_name,
                ),
            )
        )

        # Lines 75-88 (the "<display>" block) are commented out in the VB original,
        # so it is deliberately not emitted.
        self.theOutputFileStreamWriter("<basepos> 0 0 0")

        for trigger_index in range(len(a_bone.theQuatInterpBone.theTriggers)):
            a_trigger = a_bone.theQuatInterpBone.theTriggers[trigger_index]

            a_trigger_trigger = to_euler_angles(a_trigger.trigger)
            a_trigger_quat = to_euler_angles(a_trigger.quat)

            # Ten numbers: tolerance, trigger Euler xyz, quat Euler xyz, position xyz.
            # ``1 / inverseToleranceAngle`` can be an infinity (tolerance 0.0), which
            # is why fmt_trim reproduces the en-US "∞" symbol.
            self.theOutputFileStreamWriter(
                join_fields(
                    "<trigger>",
                    (
                        fmt_trim(radians_to_degrees(1 / a_trigger.inverseToleranceAngle)),
                        fmt_trim(radians_to_degrees(a_trigger_trigger.x)),
                        fmt_trim(radians_to_degrees(a_trigger_trigger.y)),
                        fmt_trim(radians_to_degrees(a_trigger_trigger.z)),
                        fmt_trim(radians_to_degrees(a_trigger_quat.x)),
                        fmt_trim(radians_to_degrees(a_trigger_quat.y)),
                        fmt_trim(radians_to_degrees(a_trigger_quat.z)),
                        fmt_trim(a_trigger.pos.x),
                        fmt_trim(a_trigger.pos.y),
                        fmt_trim(a_trigger.pos.z),
                    ),
                )
            )

    def _write_aim_at_bone(self, a_bone: SourceMdlBone) -> None:
        """Lines 153-205: ``<aimconstraint>``, ``<aimvector>``, ``<upvector>``, ``<basepos>``.

        No blank line precedes this block (unlike ``<helper>``).
        """
        bones = self.theMdlFileData.theBones
        assert bones is not None  # guarded by write_commands

        a_bone_name = remove_upto_and_including_first_dot_character_from_string(a_bone.theName)

        a_parent_bone = bones[a_bone.theAimAtBone.parentBoneIndex]
        a_parent_bone_name = remove_upto_and_including_first_dot_character_from_string(
            a_parent_bone.theName
        )

        if a_bone.proceduralRuleType == STUDIO_PROC_AIMATBONE:
            an_aim_bone = bones[a_bone.theAimAtBone.aimBoneOrAttachmentIndex]
            an_aim_name = remove_upto_and_including_first_dot_character_from_string(
                an_aim_bone.theName
            )
        else:
            an_aim_attachment: SourceMdlAttachment = self.theMdlFileData.theAttachments[
                a_bone.theAimAtBone.aimBoneOrAttachmentIndex
            ]
            an_aim_name = remove_upto_and_including_first_dot_character_from_string(
                an_aim_attachment.theName
            )

        self.theOutputFileStreamWriter(
            join_fields("<aimconstraint>", (a_bone_name, a_parent_bone_name, an_aim_name))
        )
        self.theOutputFileStreamWriter(
            join_fields(
                "<aimvector>",
                (
                    fmt_trim(a_bone.theAimAtBone.aim.x),
                    fmt_trim(a_bone.theAimAtBone.aim.y),
                    fmt_trim(a_bone.theAimAtBone.aim.z),
                ),
            )
        )
        self.theOutputFileStreamWriter(
            join_fields(
                "<upvector>",
                (
                    fmt_trim(a_bone.theAimAtBone.up.x),
                    fmt_trim(a_bone.theAimAtBone.up.y),
                    fmt_trim(a_bone.theAimAtBone.up.z),
                ),
            )
        )
        self.theOutputFileStreamWriter(
            join_fields(
                "<basepos>",
                (
                    fmt_trim(a_bone.theAimAtBone.basePos.x),
                    fmt_trim(a_bone.theAimAtBone.basePos.y),
                    fmt_trim(a_bone.theAimAtBone.basePos.z),
                ),
            )
        )
