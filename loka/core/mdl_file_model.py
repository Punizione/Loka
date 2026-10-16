"""Read Source model bones, body parts, meshes and animation metadata."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional

from .constants import TEXT_SCAL_VERSION44_VINDICTUS
from .mdl_data import (
    SourceMdlBodygroupPreset,
    SourceMdlEyeball,
    SourceMdlFlex,
    SourceMdlFlexController,
    SourceMdlFlexControllerUi,
    SourceMdlFlexDesc,
    SourceMdlFlexRule,
    SourceMdlIkChain,
    SourceMdlIkLink,
    SourceMdlIkLock,
    SourceMdlMesh,
    SourceMdlModel,
    SourceMdlModelGroup,
    SourceMdlMouth,
    SourceMdlPoseParamDesc,
    SourceMdlVertAnim,
)
from .numfmt import fmt_trim6
from .reader import SEEK_BEGIN
from .vectors import SourceQuaternion, SourceVector
from .writers.text import half_to_float

__all__ = [
    "SourceMdlFile49ModelMixin",
    "SourceMdlBoneTransform",
    "SourceMdlFlexOp",
    "SourceMdlLinearBone",
    "SourceMdlVertAnimWrinkle",
    "IntermediateExpression",
    "get_flex_rule",
    "vector_i_rotate",
]


# ---------------------------------------------------------------------------
# Structures missing from mdl_data.py
# ---------------------------------------------------------------------------

class SourceMdlFlexOp:
    """``SourceMdlFlexOp`` -- one ``mstudioflexop_t``.

    Implement ``SourceMdlFlexOp``.  ``op`` is one of
    the ``STUDIO_*`` operators below; the union field is ``value`` when
    ``op == STUDIO_CONST`` and ``index`` otherwise.
    """

    STUDIO_CONST: int = 1
    STUDIO_FETCH1: int = 2
    STUDIO_FETCH2: int = 3
    STUDIO_ADD: int = 4
    STUDIO_SUB: int = 5
    STUDIO_MUL: int = 6
    STUDIO_DIV: int = 7
    STUDIO_NEG: int = 8
    STUDIO_EXP: int = 9
    STUDIO_OPEN: int = 10
    STUDIO_CLOSE: int = 11
    STUDIO_COMMA: int = 12
    STUDIO_MAX: int = 13
    STUDIO_MIN: int = 14
    STUDIO_2WAY_0: int = 15
    STUDIO_2WAY_1: int = 16
    STUDIO_NWAY: int = 17
    STUDIO_COMBO: int = 18
    STUDIO_DOMINATE: int = 19
    STUDIO_DME_LOWER_EYELID: int = 20
    STUDIO_DME_UPPER_EYELID: int = 21

    __slots__ = ("op", "index", "value")

    def __init__(self) -> None:
        self.op: int = 0
        self.index: int = 0
        self.value: float = 0.0


class SourceMdlVertAnimWrinkle(SourceMdlVertAnim):
    """``SourceMdlVertAnimWrinkle`` -- ``mstudiovertanim_wrinkle_t``.

    Instantiated by ``ReadVertAnims`` whenever
    ``flex.vertAnimType == STUDIO_VERT_ANIM_WRINKLE``; the only extra field is the
    ``short`` that follows the two delta triples.
    """

    __slots__ = ("wrinkleDelta",)

    def __init__(self) -> None:
        super().__init__()
        self.wrinkleDelta: int = 0


class SourceMdlBoneTransform:
    """``SourceMdlBoneTransform`` -- one ``mstudiosrcbonetransform_t``.

    Implement ``SourceMdlBoneTransform``: a name
    offset plus a ``matrix3x4_t`` pre-transform and a ``matrix3x4_t``
    post-transform.  The operation is ``pretransform * bone transform *
    posttransform``.
    """

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.preTransformColumn0: SourceVector = SourceVector()
        self.preTransformColumn1: SourceVector = SourceVector()
        self.preTransformColumn2: SourceVector = SourceVector()
        self.preTransformColumn3: SourceVector = SourceVector()
        self.postTransformColumn0: SourceVector = SourceVector()
        self.postTransformColumn1: SourceVector = SourceVector()
        self.postTransformColumn2: SourceVector = SourceVector()
        self.postTransformColumn3: SourceVector = SourceVector()
        self.theName: Optional[str] = None


class SourceMdlLinearBone:
    """``SourceMdlLinearBone`` -- ``mstudiolinearbone_t`` plus its nine arrays.

    Implement ``SourceMdlLinearBone``.  Every array
    offset in the header is relative to the start of this structure, and each block
    is 4-byte aligned, which is why the reader seeks to each offset instead of
    reading the blocks back to back.
    """

    def __init__(self) -> None:
        self.boneCount: int = 0
        self.flagsOffset: int = 0
        self.parentOffset: int = 0
        self.posOffset: int = 0
        self.quatOffset: int = 0
        self.rotOffset: int = 0
        self.poseToBoneOffset: int = 0
        self.posScaleOffset: int = 0
        self.rotScaleOffset: int = 0
        self.qAlignmentOffset: int = 0
        self.unused: list[int] = [0] * 6

        self.theFlags: list[int] = []
        self.theParents: list[int] = []
        self.thePositions: list[SourceVector] = []
        self.theQuaternions: list[SourceQuaternion] = []
        self.theRotations: list[SourceVector] = []
        self.thePoseToBoneDataColumn0s: list[SourceVector] = []
        self.thePoseToBoneDataColumn1s: list[SourceVector] = []
        self.thePoseToBoneDataColumn2s: list[SourceVector] = []
        self.thePoseToBoneDataColumn3s: list[SourceVector] = []
        self.thePositionScales: list[SourceVector] = []
        self.theRotationScales: list[SourceVector] = []
        self.theQAlignments: list[SourceQuaternion] = []


class IntermediateExpression:
    """``IntermediateExpression`` -- one entry of the infix-conversion stack."""

    __slots__ = ("theExpression", "thePrecedence")

    def __init__(self, the_expression: str, the_precedence: int) -> None:
        self.theExpression: str = the_expression
        self.thePrecedence: int = the_precedence


# ---------------------------------------------------------------------------
# Shared math helpers
# ---------------------------------------------------------------------------

def vector_i_rotate(
    input_vector: SourceVector,
    matrix_column0: SourceVector,
    matrix_column1: SourceVector,
    matrix_column2: SourceVector,
    matrix_column3: SourceVector,
) -> SourceVector:
    """Implement ``MathModule.VectorIRotate`` -- rotate by the inverse matrix.

    ``matrixColumn3`` is accepted (as in the VB signature) but never used.
    """
    output = SourceVector()
    output.x = (
        input_vector.x * matrix_column0.x
        + input_vector.y * matrix_column0.y
        + input_vector.z * matrix_column0.z
    )
    output.y = (
        input_vector.x * matrix_column1.x
        + input_vector.y * matrix_column1.y
        + input_vector.z * matrix_column1.z
    )
    output.z = (
        input_vector.x * matrix_column2.x
        + input_vector.y * matrix_column2.y
        + input_vector.z * matrix_column2.z
    )
    return output


def _math_round(value: float, digits: int) -> float:
    """Implement the ``Math.Round(Double, Int32)`` overload the parser calls.

    .NET's ``Math.Round`` defaults to ``MidpointRounding.ToEven`` -- **banker's**
    rounding, unlike the ``ToString`` formatting path which rounds half away from
    zero.  ``decimal`` on the exact binary value reproduces it.
    """
    if math.isnan(value) or math.isinf(value):
        return value
    rounded = Decimal(value).quantize(
        Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN
    )
    return float(rounded)


# ---------------------------------------------------------------------------
# Flex-rule equation assembly (``Common.GetFlexRule``)
# ---------------------------------------------------------------------------

def get_flex_rule(
    flex_descs: list[SourceMdlFlexDesc],
    flex_controllers: list[SourceMdlFlexController],
    flex_rule: SourceMdlFlexRule,
) -> str:
    """Implement ``Common.GetFlexRule``.

    Converts the reverse-polish ``theFlexOps`` list into the infix equation the QC
    writer emits as a ``%<flexdesc> = <expression>`` line.  The precedence rules are
    reproduced literally: ``*`` and ``/`` push precedence 2 and parenthesise
    operands below 2, ``+``/``-`` push 1 without parenthesising, ``min()``/``max()``
    and the DMX-only operators push 5, constants and leaves push 10.

    NOTE: this helper lives in the parser's ``Common`` module rather than in
    ``SourceMdlFile``; it is ported here because it is the authoritative consumer
    of the ``read_flex_ops`` representation.  The QC writer may re-export it.
    """
    flex_rule_equation = "\t"
    flex_rule_equation += "%"
    flex_rule_equation += flex_descs[flex_rule.flexIndex].theName
    flex_rule_equation += " = "

    if flex_rule.theFlexOps:
        stack: list[IntermediateExpression] = []
        dmx_flex_op_was_used = False

        for a_flex_op in flex_rule.theFlexOps:
            if a_flex_op.op == SourceMdlFlexOp.STUDIO_CONST:
                stack.append(
                    IntermediateExpression(
                        fmt_trim6(_math_round(a_flex_op.value, 6)), 10
                    )
                )
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_FETCH1:
                # int m = pFlexcontroller( (LocalFlexController_t)pops->d.index)->localToGlobal;
                stack.append(
                    IntermediateExpression(flex_controllers[a_flex_op.index].theName, 10)
                )
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_FETCH2:
                stack.append(
                    IntermediateExpression("%" + flex_descs[a_flex_op.index].theName, 10)
                )
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_ADD:
                right_intermediate = stack.pop()
                left_intermediate = stack.pop()

                new_expr = (
                    left_intermediate.theExpression
                    + " + "
                    + right_intermediate.theExpression
                )
                stack.append(IntermediateExpression(new_expr, 1))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_SUB:
                # NOTE: the model compiler stores STUDIO_SUB instead of STUDIO_NEG
                # when '-' starts a term, so a one-element stack means unary negation.
                if len(stack) >= 2:
                    right_intermediate = stack.pop()
                    left_intermediate = stack.pop()

                    new_expr = (
                        left_intermediate.theExpression
                        + " - "
                        + right_intermediate.theExpression
                    )
                    stack.append(IntermediateExpression(new_expr, 1))
                else:
                    right_intermediate = stack.pop()

                    new_expr = "-(" + right_intermediate.theExpression + ")"
                    stack.append(IntermediateExpression(new_expr, 10))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_MUL:
                right_intermediate = stack.pop()
                if right_intermediate.thePrecedence < 2:
                    right_expr = "(" + right_intermediate.theExpression + ")"
                else:
                    right_expr = right_intermediate.theExpression

                left_intermediate = stack.pop()
                if left_intermediate.thePrecedence < 2:
                    left_expr = "(" + left_intermediate.theExpression + ")"
                else:
                    left_expr = left_intermediate.theExpression

                new_expr = left_expr + " * " + right_expr
                stack.append(IntermediateExpression(new_expr, 2))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_DIV:
                right_intermediate = stack.pop()
                if right_intermediate.thePrecedence < 2:
                    right_expr = "(" + right_intermediate.theExpression + ")"
                else:
                    right_expr = right_intermediate.theExpression

                left_intermediate = stack.pop()
                if left_intermediate.thePrecedence < 2:
                    left_expr = "(" + left_intermediate.theExpression + ")"
                else:
                    left_expr = left_intermediate.theExpression

                new_expr = left_expr + " / " + right_expr
                stack.append(IntermediateExpression(new_expr, 2))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_NEG:
                right_intermediate = stack.pop()

                new_expr = "-(" + right_intermediate.theExpression + ")"
                stack.append(IntermediateExpression(new_expr, 10))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_EXP:
                # "ignore this op because it is a mistake to be here"
                pass
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_OPEN:
                pass
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_CLOSE:
                pass
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_COMMA:
                pass
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_MAX:
                right_intermediate = stack.pop()
                if right_intermediate.thePrecedence < 5:
                    right_expr = "(" + right_intermediate.theExpression + ")"
                else:
                    right_expr = right_intermediate.theExpression

                left_intermediate = stack.pop()
                if left_intermediate.thePrecedence < 5:
                    left_expr = "(" + left_intermediate.theExpression + ")"
                else:
                    left_expr = left_intermediate.theExpression

                new_expr = "max(" + left_expr + ", " + right_expr + ")"
                stack.append(IntermediateExpression(new_expr, 5))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_MIN:
                right_intermediate = stack.pop()
                if right_intermediate.thePrecedence < 5:
                    right_expr = "(" + right_intermediate.theExpression + ")"
                else:
                    right_expr = right_intermediate.theExpression

                left_intermediate = stack.pop()
                if left_intermediate.thePrecedence < 5:
                    left_expr = "(" + left_intermediate.theExpression + ")"
                else:
                    left_expr = left_intermediate.theExpression

                new_expr = "min(" + left_expr + ", " + right_expr + ")"
                stack.append(IntermediateExpression(new_expr, 5))
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_2WAY_0:
                # RemapValClamped( src[m], -1.0f, 0.0f, 1.0f, 0.0f )
                new_expression = (
                    "(1 - (min(max("
                    + flex_controllers[a_flex_op.index].theName
                    + " + 1, 0), 1)))"
                )
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_2WAY_1:
                # RemapValClamped( src[m], 0.0f, 1.0f, 0.0f, 1.0f )
                new_expression = (
                    "(min(max(" + flex_controllers[a_flex_op.index].theName + ", 0), 1))"
                )
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_NWAY:
                a_value_controller = flex_controllers[a_flex_op.index]

                value_controller_index = stack.pop()
                fl_value = flex_controllers[int(value_controller_index.theExpression)].theName

                filter_ramp_w = stack.pop()
                filter_ramp_z = stack.pop()
                filter_ramp_y = stack.pop()
                filter_ramp_x = stack.pop()

                greater_than_x = "min(1, (-min(0, (" + filter_ramp_x.theExpression + " - " + fl_value + "))))"
                less_than_y = "min(1, (-min(0, (" + fl_value + " - " + filter_ramp_y.theExpression + "))))"
                remap_x = (
                    "min(max(("
                    + fl_value
                    + " - "
                    + filter_ramp_x.theExpression
                    + ") / ("
                    + filter_ramp_y.theExpression
                    + " - "
                    + filter_ramp_x.theExpression
                    + "), 0), 1)"
                )
                greater_than_equal_y = "-(min(1, (-min(0, (" + fl_value + " - " + filter_ramp_y.theExpression + ")))) - 1)"
                less_than_equal_z = "-(min(1, (-min(0, (" + filter_ramp_z.theExpression + " - " + fl_value + ")))) - 1)"
                greater_than_z = "min(1, (-min(0, (" + filter_ramp_z.theExpression + " - " + fl_value + "))))"
                less_than_w = "min(1, (-min(0, (" + fl_value + " - " + filter_ramp_w.theExpression + "))))"
                remap_z = (
                    "(1 - (min(max(("
                    + fl_value
                    + " - "
                    + filter_ramp_z.theExpression
                    + ") / ("
                    + filter_ramp_w.theExpression
                    + " - "
                    + filter_ramp_z.theExpression
                    + "), 0), 1)))"
                )

                fl_value = (
                    "(("
                    + greater_than_x
                    + " * "
                    + less_than_y
                    + ") * "
                    + remap_x
                    + ") + ("
                    + greater_than_equal_y
                    + " * "
                    + less_than_equal_z
                    + ") + (("
                    + greater_than_z
                    + " * "
                    + less_than_w
                    + ") * "
                    + remap_z
                    + ")"
                )

                new_expression = "((" + fl_value + ") * (" + a_value_controller.theName + "))"
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_COMBO:
                count = a_flex_op.index
                new_expression = ""
                intermediate_exp = stack.pop()
                new_expression += intermediate_exp.theExpression
                for _ in range(2, count + 1):
                    intermediate_exp = stack.pop()
                    new_expression += " * " + intermediate_exp.theExpression
                new_expression = "(" + new_expression + ")"
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_DOMINATE:
                count = a_flex_op.index
                new_expression = ""
                intermediate_exp = stack.pop()
                new_expression += intermediate_exp.theExpression
                for _ in range(2, count + 1):
                    intermediate_exp = stack.pop()
                    new_expression += " * " + intermediate_exp.theExpression
                intermediate_exp = stack.pop()
                new_expression = (
                    intermediate_exp.theExpression + " * (1 - " + new_expression + ")"
                )
                new_expression = "(" + new_expression + ")"
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_DME_LOWER_EYELID:
                p_close_lid_v = flex_controllers[a_flex_op.index]
                fl_close_lid_v_min = fmt_trim6(_math_round(p_close_lid_v.min, 6))
                fl_close_lid_v_max = fmt_trim6(_math_round(p_close_lid_v.max, 6))
                fl_close_lid_v = (
                    "(min(max(("
                    + p_close_lid_v.theName
                    + " - "
                    + fl_close_lid_v_min
                    + ") / ("
                    + fl_close_lid_v_max
                    + " - "
                    + fl_close_lid_v_min
                    + "), 0), 1))"
                )

                close_lid_index = stack.pop()
                p_close_lid = flex_controllers[int(close_lid_index.theExpression)]
                fl_close_lid_min = fmt_trim6(_math_round(p_close_lid.min, 6))
                fl_close_lid_max = fmt_trim6(_math_round(p_close_lid.max, 6))
                fl_close_lid = (
                    "(min(max(("
                    + p_close_lid.theName
                    + " - "
                    + fl_close_lid_min
                    + ") / ("
                    + fl_close_lid_max
                    + " - "
                    + fl_close_lid_min
                    + "), 0), 1))"
                )

                # Unused, but need to pop it off the stack.
                stack.pop()

                eye_up_down_index = stack.pop()
                p_eye_up_down = flex_controllers[int(eye_up_down_index.theExpression)]
                fl_eye_up_down_min = fmt_trim6(_math_round(p_eye_up_down.min, 6))
                fl_eye_up_down_max = fmt_trim6(_math_round(p_eye_up_down.max, 6))
                fl_eye_up_down = (
                    "(-1 + 2 * (min(max(("
                    + p_eye_up_down.theName
                    + " - "
                    + fl_eye_up_down_min
                    + ") / ("
                    + fl_eye_up_down_max
                    + " - "
                    + fl_eye_up_down_min
                    + "), 0), 1)))"
                )

                new_expression = (
                    "(min(1, (1 - "
                    + fl_eye_up_down
                    + ")) * (1 - "
                    + fl_close_lid_v
                    + ") * "
                    + fl_close_lid
                    + ")"
                )
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            elif a_flex_op.op == SourceMdlFlexOp.STUDIO_DME_UPPER_EYELID:
                p_close_lid_v = flex_controllers[a_flex_op.index]
                fl_close_lid_v_min = fmt_trim6(_math_round(p_close_lid_v.min, 6))
                fl_close_lid_v_max = fmt_trim6(_math_round(p_close_lid_v.max, 6))
                fl_close_lid_v = (
                    "(min(max(("
                    + p_close_lid_v.theName
                    + " - "
                    + fl_close_lid_v_min
                    + ") / ("
                    + fl_close_lid_v_max
                    + " - "
                    + fl_close_lid_v_min
                    + "), 0), 1))"
                )

                close_lid_index = stack.pop()
                p_close_lid = flex_controllers[int(close_lid_index.theExpression)]
                fl_close_lid_min = fmt_trim6(_math_round(p_close_lid.min, 6))
                fl_close_lid_max = fmt_trim6(_math_round(p_close_lid.max, 6))
                fl_close_lid = (
                    "(min(max(("
                    + p_close_lid.theName
                    + " - "
                    + fl_close_lid_min
                    + ") / ("
                    + fl_close_lid_max
                    + " - "
                    + fl_close_lid_min
                    + "), 0), 1))"
                )

                # Unused, but need to pop it off the stack.
                stack.pop()

                eye_up_down_index = stack.pop()
                p_eye_up_down = flex_controllers[int(eye_up_down_index.theExpression)]
                fl_eye_up_down_min = fmt_trim6(_math_round(p_eye_up_down.min, 6))
                fl_eye_up_down_max = fmt_trim6(_math_round(p_eye_up_down.max, 6))
                fl_eye_up_down = (
                    "(-1 + 2 * (min(max(("
                    + p_eye_up_down.theName
                    + " - "
                    + fl_eye_up_down_min
                    + ") / ("
                    + fl_eye_up_down_max
                    + " - "
                    + fl_eye_up_down_min
                    + "), 0), 1)))"
                )

                new_expression = (
                    "(min(1, (1 + "
                    + fl_eye_up_down
                    + ")) * "
                    + fl_close_lid_v
                    + " * "
                    + fl_close_lid
                    + ")"
                )
                stack.append(IntermediateExpression(new_expression, 5))
                dmx_flex_op_was_used = True
            else:
                stack.clear()
                break

        # The loop above leaves the final expression on the top of the stack.
        if dmx_flex_op_was_used:
            flex_rule_equation += (
                stack[-1].theExpression
                + " // WARNING: Expression is an approximation of what can only be done via DMX file."
            )
        elif len(stack) == 1:
            flex_rule_equation += stack[-1].theExpression
        elif len(stack) == 0 or len(stack) > 1:
            # NOTE: reproduced literally, including the ``Peek`` that raises when the
            # stack was cleared by an unknown operator.
            flex_rule_equation = (
                "// "
                + flex_rule_equation
                + stack[-1].theExpression
                + " // ERROR: Unknown flex operation."
            )
        else:  # pragma: no cover - unreachable; kept for 1:1 readability
            flex_rule_equation = "// [Empty flex rule found and ignored.]"

    return flex_rule_equation


# ---------------------------------------------------------------------------
# The mixin
# ---------------------------------------------------------------------------

class SourceMdlFile49ModelMixin:
    """Ported from ``SourceMdlFile49``.  Operates on ``self.theInputFileReader``
    and ``self.theMdlFileData``, exactly like the VB class, so it can be mixed into
    ``loka.core.mdl_file.SourceMdlFile49``.
    """

    # ------------------------------------------------------------------
    # Eyeballs
    # ------------------------------------------------------------------
    def read_eyeballs(
        self, model_input_file_stream_position: int, a_model: SourceMdlModel
    ) -> None:
        """Implement ``ReadEyeballs`` (2900-3017).

        ``ReadModels`` calls this *before* ``ReadMeshes`` so that the meshes can fill
        in ``theEyeballs[n].theTextureIndex`` (they look for ``materialType == 1``).

        NOTE: the VB original has no ``Try``/``Catch`` here.  It also touches the
        model's first eyeball to detect a ``$upaxis Y`` model, which is the only
        place ``theUpAxisYCommandWasUsed`` is ever set.
        """
        data = self.theMdlFileData
        # ``SourceMdlFileDataBase`` declares this; the Python data class does not.
        if not hasattr(data, "theUpAxisYCommandWasUsed"):
            data.theUpAxisYCommandWasUsed = False

        if not (a_model.eyeballCount > 0 and a_model.eyeballOffset != 0):
            return

        reader = self.theInputFileReader
        a_model.theEyeballs = []
        reader.seek(model_input_file_stream_position + a_model.eyeballOffset, SEEK_BEGIN)

        for _ in range(a_model.eyeballCount):
            eyeball_input_file_stream_position = reader.position
            an_eyeball = SourceMdlEyeball()

            an_eyeball.nameOffset = reader.read_int32()
            an_eyeball.boneIndex = reader.read_int32()
            an_eyeball.org = SourceVector()
            an_eyeball.org.x = reader.read_single()
            an_eyeball.org.y = reader.read_single()
            an_eyeball.org.z = reader.read_single()
            an_eyeball.zOffset = reader.read_single()
            an_eyeball.radius = reader.read_single()
            an_eyeball.up = SourceVector()
            an_eyeball.up.x = reader.read_single()
            an_eyeball.up.y = reader.read_single()
            an_eyeball.up.z = reader.read_single()
            an_eyeball.forward = SourceVector()
            an_eyeball.forward.x = reader.read_single()
            an_eyeball.forward.y = reader.read_single()
            an_eyeball.forward.z = reader.read_single()
            an_eyeball.texture = reader.read_int32()

            an_eyeball.unused1 = reader.read_int32()
            an_eyeball.irisScale = reader.read_single()
            an_eyeball.unused2 = reader.read_int32()

            # NOTE: the C struct is ``int upperflexdesc[3]`` / ``int lowerflexdesc[3]``
            # / ``float uppertarget[3]`` / ``float lowertarget[3]``; mdl_data.py sizes
            # these lists to two, so the full three-element lists are installed here.
            an_eyeball.upperFlexDesc = [reader.read_int32() for _ in range(3)]
            an_eyeball.lowerFlexDesc = [reader.read_int32() for _ in range(3)]
            an_eyeball.upperTarget = [reader.read_single() for _ in range(3)]
            an_eyeball.lowerTarget = [reader.read_single() for _ in range(3)]

            an_eyeball.upperLidFlexDesc = reader.read_int32()
            an_eyeball.lowerLidFlexDesc = reader.read_int32()

            an_eyeball.unused = [reader.read_int32() for _ in range(4)]

            an_eyeball.eyeballIsNonFacs = reader.read_byte()

            # ``char unused3[3]``; the Python field is a plain string.
            an_eyeball.unused3 = reader.read_chars(3)
            an_eyeball.unused4 = [reader.read_int32() for _ in range(7)]

            a_model.theEyeballs.append(an_eyeball)

            # NOTE: set the default value to -1 to distinguish it from the value
            # assigned to it by ReadMeshes().
            an_eyeball.theTextureIndex = -1

            input_file_stream_position = reader.position

            # NOTE: the MDL file doesn't appear to store the eyeball name; studiomdl
            # only uses it internally with eyelids.
            if an_eyeball.nameOffset != 0:
                reader.seek(
                    eyeball_input_file_stream_position + an_eyeball.nameOffset,
                    SEEK_BEGIN,
                )
                an_eyeball.theName = reader.read_null_terminated_string()
            else:
                an_eyeball.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

        if len(a_model.theEyeballs) > 0:
            # Detect if $upaxis Y was used.
            # FROM: [48] studiomdl.cpp Option_Eyeball():
            #   AngleMatrix( g_defaultrotation, vtmp );
            #   VectorIRotate( Vector( 0, 0, 1 ), vtmp, tmp );
            #   VectorIRotate( tmp, pmodel->source->boneToPose[eyeball->bone], eyeball->up );
            an_eyeball = a_model.theEyeballs[0]
            a_bone = data.theBones[an_eyeball.boneIndex]
            tmp = vector_i_rotate(
                an_eyeball.up,
                a_bone.poseToBoneColumn0,
                a_bone.poseToBoneColumn1,
                a_bone.poseToBoneColumn2,
                a_bone.poseToBoneColumn3,
            )
            # ``tmp.y > 0.99 AndAlso tmp.y < 1.01``
            if 0.99 < tmp.y < 1.01:
                data.theUpAxisYCommandWasUsed = True

    # ------------------------------------------------------------------
    # Flexes / vertex animations
    # ------------------------------------------------------------------
    def read_flexes(
        self, mesh_input_file_stream_position: int, a_mesh: SourceMdlMesh
    ) -> None:
        """Implement ``ReadFlexes`` (3019-3073).

        Replaces the stub of the same name in ``mdl_file.py``; ``ReadMeshes`` guards
        the call with ``flexCount > 0 AndAlso flexOffset <> 0``.
        """
        a_mesh.theFlexes = []

        reader = self.theInputFileReader
        reader.seek(mesh_input_file_stream_position + a_mesh.flexOffset, SEEK_BEGIN)

        for _ in range(a_mesh.flexCount):
            flex_input_file_stream_position = reader.position
            a_flex = SourceMdlFlex()

            a_flex.flexDescIndex = reader.read_int32()

            a_flex.target0 = reader.read_single()
            a_flex.target1 = reader.read_single()
            a_flex.target2 = reader.read_single()
            a_flex.target3 = reader.read_single()

            a_flex.vertCount = reader.read_int32()
            a_flex.vertOffset = reader.read_int32()

            a_flex.flexDescPartnerIndex = reader.read_int32()
            a_flex.vertAnimType = reader.read_byte()
            # ``unsigned char unusedchar[3]``; the Python field is a plain string.
            a_flex.unusedChar = reader.read_chars(3)
            a_flex.unused = [reader.read_int32() for _ in range(6)]

            a_mesh.theFlexes.append(a_flex)

            input_file_stream_position = reader.position

            if a_flex.vertCount > 0 and a_flex.vertOffset != 0:
                self.read_vert_anims(flex_input_file_stream_position, a_flex)

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_vert_anims(
        self, flex_input_file_stream_position: int, a_flex: SourceMdlFlex
    ) -> None:
        """Implement ``ReadVertAnims`` (3075-3124).

        ``theDelta`` / ``theNDelta`` receive the **decoded** floats (see
        ``writers/text.py::half_to_float``), which is what ``writers/vta.py`` adds to
        the bind-pose vertex.  The ``aFlex.theVertAnims.Sort`` call is commented out
        in the VB original, so on-disk order is preserved.
        """
        a_flex.theVertAnims = []

        reader = self.theInputFileReader
        reader.seek(flex_input_file_stream_position + a_flex.vertOffset, SEEK_BEGIN)

        for _ in range(a_flex.vertCount):
            if a_flex.vertAnimType == SourceMdlVertAnim.STUDIO_VERT_ANIM_WRINKLE:
                a_vert_anim: SourceMdlVertAnim = SourceMdlVertAnimWrinkle()
            else:
                a_vert_anim = SourceMdlVertAnim()

            a_vert_anim.index = reader.read_uint16()
            a_vert_anim.speed = reader.read_byte()
            a_vert_anim.side = reader.read_byte()

            a_vert_anim.theDelta = [half_to_float(reader.read_uint16()) for _ in range(3)]
            a_vert_anim.theNDelta = [half_to_float(reader.read_uint16()) for _ in range(3)]

            if a_flex.vertAnimType == SourceMdlVertAnim.STUDIO_VERT_ANIM_WRINKLE:
                a_vert_anim.wrinkleDelta = reader.read_int16()  # type: ignore[attr-defined]

            a_flex.theVertAnims.append(a_vert_anim)

    # ------------------------------------------------------------------
    # Flex descriptors / controllers / rules
    # ------------------------------------------------------------------
    def read_flex_descs(self) -> None:
        """Implement ``ReadFlexDescs`` (3130-3171)."""
        data = self.theMdlFileData
        if data.flexDescCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.flexDescOffset, SEEK_BEGIN)

        data.theFlexDescs = []
        for _ in range(data.flexDescCount):
            flex_desc_input_file_stream_position = reader.position
            a_flex_desc = SourceMdlFlexDesc()
            a_flex_desc.nameOffset = reader.read_int32()
            data.theFlexDescs.append(a_flex_desc)

            input_file_stream_position = reader.position

            if a_flex_desc.nameOffset != 0:
                reader.seek(
                    flex_desc_input_file_stream_position + a_flex_desc.nameOffset,
                    SEEK_BEGIN,
                )
                a_flex_desc.theName = reader.read_null_terminated_string()
            else:
                a_flex_desc.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_flex_controllers(self) -> None:
        """Implement ``ReadFlexControllers`` (3173-3255).

        The inner loop is the VB "clean flex controller names for QC" pass: QC does
        not allow ``-`` in a name (DMX does), and duplicates get a ``2``, ``3``, ...
        suffix.  It re-runs over *every already-read* controller each time a name is
        read, and it reproduces the original's quirk of appending the suffix to the
        **original** (uncleaned) name.
        """
        data = self.theMdlFileData
        if data.flexControllerCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.flexControllerOffset, SEEK_BEGIN)

        data.theFlexControllers = []
        for _ in range(data.flexControllerCount):
            flex_controller_input_file_stream_position = reader.position
            a_flex_controller = SourceMdlFlexController()
            a_flex_controller.typeOffset = reader.read_int32()
            a_flex_controller.nameOffset = reader.read_int32()
            a_flex_controller.localToGlobal = reader.read_int32()
            a_flex_controller.min = reader.read_single()
            a_flex_controller.max = reader.read_single()
            data.theFlexControllers.append(a_flex_controller)

            input_file_stream_position = reader.position

            if a_flex_controller.typeOffset != 0:
                reader.seek(
                    flex_controller_input_file_stream_position
                    + a_flex_controller.typeOffset,
                    SEEK_BEGIN,
                )
                a_flex_controller.theType = reader.read_null_terminated_string()
            else:
                a_flex_controller.theType = ""

            if a_flex_controller.nameOffset != 0:
                reader.seek(
                    flex_controller_input_file_stream_position
                    + a_flex_controller.nameOffset,
                    SEEK_BEGIN,
                )
                a_flex_controller.theName = reader.read_null_terminated_string()

                # Clean flex controller names for QC.
                clean_flex_controller_names: list[str] = []
                for flex_controller_index in range(len(data.theFlexControllers)):
                    a_flex_controller = data.theFlexControllers[flex_controller_index]
                    new_name = a_flex_controller.theName

                    new_name = new_name.replace("-", "")
                    # NOTE: starting this at 1 means the name will not have a number
                    # and the second name will have a 2.
                    name_number = 1
                    while new_name in clean_flex_controller_names:
                        name_number += 1
                        new_name = a_flex_controller.theName + str(name_number)

                    clean_flex_controller_names.append(new_name)
                    a_flex_controller.theName = new_name
            else:
                a_flex_controller.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_flex_rules(self) -> None:
        """Implement ``ReadFlexRules`` (3257-3296).

        NOTE: this must run *after* ``read_flex_descs`` -- it flags
        ``theDescIsUsedByFlexRule`` on each rule's target desc, and that flag is what
        the QC writer turns into the ``localvar`` lines.
        """
        data = self.theMdlFileData
        if data.flexRuleCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.flexRuleOffset, SEEK_BEGIN)

        data.theFlexRules = []
        for _ in range(data.flexRuleCount):
            flex_rule_input_file_stream_position = reader.position
            a_flex_rule = SourceMdlFlexRule()
            a_flex_rule.flexIndex = reader.read_int32()
            op_count = reader.read_int32()
            op_offset = reader.read_int32()
            # The VB fields are ``opCount`` / ``opOffset``; mdl_data.py declares
            # ``flexOpCount`` / ``flexOpOffset``.  Write both.
            a_flex_rule.opCount = op_count
            a_flex_rule.flexOpCount = op_count
            a_flex_rule.opOffset = op_offset
            a_flex_rule.flexOpOffset = op_offset
            data.theFlexRules.append(a_flex_rule)

            input_file_stream_position = reader.position

            # NOTE: if flexIndex is out of range this raises, exactly like the VB.
            data.theFlexDescs[a_flex_rule.flexIndex].theDescIsUsedByFlexRule = True

            if a_flex_rule.opCount > 0 and a_flex_rule.opOffset != 0:
                self.read_flex_ops(flex_rule_input_file_stream_position, a_flex_rule)

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_flex_ops(
        self, flex_rule_input_file_stream_position: int, a_flex_rule: SourceMdlFlexRule
    ) -> None:
        """Implement ``ReadFlexOps`` (3298-3331).

        The ops are stored as :class:`SourceMdlFlexOp` objects (VB
        ``List(Of SourceMdlFlexOp)``); ``SourceMdlFlexRule.theFlexOps`` is only
        loosely typed in ``mdl_data.py``.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(
            flex_rule_input_file_stream_position + a_flex_rule.opOffset, SEEK_BEGIN
        )

        a_flex_rule.theFlexOps = []
        for _ in range(a_flex_rule.opCount):
            a_flex_op = SourceMdlFlexOp()
            a_flex_op.op = reader.read_int32()
            if a_flex_op.op == SourceMdlFlexOp.STUDIO_CONST:
                a_flex_op.value = reader.read_single()
            else:
                a_flex_op.index = reader.read_int32()
                if a_flex_op.op == SourceMdlFlexOp.STUDIO_FETCH2:
                    data.theFlexDescs[a_flex_op.index].theDescIsUsedByFlexRule = True
            a_flex_rule.theFlexOps.append(a_flex_op)

    # ------------------------------------------------------------------
    # IK chains / locks
    # ------------------------------------------------------------------
    def read_ik_chains(self) -> None:
        """Implement ``ReadIkChains`` (3333-3378)."""
        data = self.theMdlFileData
        if data.ikChainCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.ikChainOffset, SEEK_BEGIN)

        data.theIkChains = []
        for _ in range(data.ikChainCount):
            ik_chain_input_file_stream_position = reader.position
            an_ik_chain = SourceMdlIkChain()
            an_ik_chain.nameOffset = reader.read_int32()
            an_ik_chain.linkType = reader.read_int32()
            an_ik_chain.linkCount = reader.read_int32()
            an_ik_chain.linkOffset = reader.read_int32()
            data.theIkChains.append(an_ik_chain)

            input_file_stream_position = reader.position

            if an_ik_chain.nameOffset != 0:
                reader.seek(
                    ik_chain_input_file_stream_position + an_ik_chain.nameOffset,
                    SEEK_BEGIN,
                )
                an_ik_chain.theName = reader.read_null_terminated_string()
            else:
                an_ik_chain.theName = ""

            self.read_ik_links(ik_chain_input_file_stream_position, an_ik_chain)

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_ik_links(
        self, ik_chain_input_file_stream_position: int, an_ik_chain: SourceMdlIkChain
    ) -> None:
        """Implement ``ReadIkLinks`` (3380-3413).

        The ``knee`` vector written by ``$ikchain`` is
        ``theLinks[0].idealBendingDirection``; ``unused0`` is read only to keep the
        28-byte stride.  The Python data class calls the pair ``kneeDir`` /
        ``kneeDirX/Y/Z``, so those are filled in as well.
        """
        if an_ik_chain.linkCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(
            ik_chain_input_file_stream_position + an_ik_chain.linkOffset, SEEK_BEGIN
        )

        an_ik_chain.theLinks = []
        for _ in range(an_ik_chain.linkCount):
            an_ik_link = SourceMdlIkLink()
            an_ik_link.boneIndex = reader.read_int32()
            an_ik_link.idealBendingDirection = SourceVector()
            an_ik_link.idealBendingDirection.x = reader.read_single()
            an_ik_link.idealBendingDirection.y = reader.read_single()
            an_ik_link.idealBendingDirection.z = reader.read_single()
            an_ik_link.unused0 = SourceVector()
            an_ik_link.unused0.x = reader.read_single()
            an_ik_link.unused0.y = reader.read_single()
            an_ik_link.unused0.z = reader.read_single()

            # Mirror onto the names mdl_data.py declares.
            an_ik_link.kneeDir = an_ik_link.idealBendingDirection
            an_ik_link.kneeDirX = an_ik_link.idealBendingDirection.x
            an_ik_link.kneeDirY = an_ik_link.idealBendingDirection.y
            an_ik_link.kneeDirZ = an_ik_link.idealBendingDirection.z

            an_ik_chain.theLinks.append(an_ik_link)

    def read_ik_locks(self) -> None:
        """Implement ``ReadIkLocks`` (3415-3448) -- ``$ikautoplaylock`` data."""
        data = self.theMdlFileData
        if data.localIkAutoPlayLockCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.localIkAutoPlayLockOffset, SEEK_BEGIN)

        data.theIkLocks = []
        for _ in range(data.localIkAutoPlayLockCount):
            an_ik_lock = SourceMdlIkLock()
            an_ik_lock.chainIndex = reader.read_int32()
            pos_weight = reader.read_single()
            local_q_weight = reader.read_single()
            flags = reader.read_int32()
            # The VB fields are ``posWeight`` / ``localQWeight`` / ``flags``;
            # mdl_data.py declares ``posLock`` / ``rotLock``.  Write both.
            an_ik_lock.posWeight = pos_weight
            an_ik_lock.localQWeight = local_q_weight
            an_ik_lock.flags = flags
            an_ik_lock.posLock = pos_weight
            an_ik_lock.rotLock = local_q_weight
            an_ik_lock.unused = [reader.read_int32() for _ in range(4)]
            data.theIkLocks.append(an_ik_lock)

    # ------------------------------------------------------------------
    # Mouths / pose parameters / model groups
    # ------------------------------------------------------------------
    def read_mouths(self) -> None:
        """Implement ``ReadMouths`` (3450-3489)."""
        data = self.theMdlFileData
        if data.mouthCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.mouthOffset, SEEK_BEGIN)

        data.theMouths = []
        for _ in range(data.mouthCount):
            a_mouth = SourceMdlMouth()

            a_mouth.boneIndex = reader.read_int32()
            a_mouth.forward = SourceVector()
            a_mouth.forward.x = reader.read_single()
            a_mouth.forward.y = reader.read_single()
            a_mouth.forward.z = reader.read_single()
            a_mouth.flexDescIndex = reader.read_int32()

            data.theMouths.append(a_mouth)

        if len(data.theMouths) > 0:
            # Seems like any $model can have these lines, so simply assign them to
            # the first one.
            data.theBodyParts[0].theModelCommandIsUsed = True

    def read_pose_param_descs(self) -> None:
        """Implement ``ReadPoseParamDescs`` (3491-3536) -- ``$poseparameter`` data."""
        data = self.theMdlFileData
        if data.localPoseParamaterCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.localPoseParameterOffset, SEEK_BEGIN)

        data.thePoseParamDescs = []
        for _ in range(data.localPoseParamaterCount):
            pose_input_file_stream_position = reader.position
            a_pose_param_desc = SourceMdlPoseParamDesc()
            a_pose_param_desc.nameOffset = reader.read_int32()
            a_pose_param_desc.flags = reader.read_int32()
            a_pose_param_desc.startingValue = reader.read_single()
            a_pose_param_desc.endingValue = reader.read_single()
            a_pose_param_desc.loopingRange = reader.read_single()
            data.thePoseParamDescs.append(a_pose_param_desc)

            input_file_stream_position = reader.position

            if a_pose_param_desc.nameOffset != 0:
                reader.seek(
                    pose_input_file_stream_position + a_pose_param_desc.nameOffset,
                    SEEK_BEGIN,
                )
                a_pose_param_desc.theName = reader.read_null_terminated_string()
            else:
                a_pose_param_desc.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_model_groups(self) -> None:
        """Implement ``ReadModelGroups`` (3712-3767) -- ``$includemodel`` data."""
        data = self.theMdlFileData
        if data.includeModelCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.includeModelOffset, SEEK_BEGIN)

        data.theModelGroups = []
        for _ in range(data.includeModelCount):
            include_model_input_file_stream_position = reader.position
            a_model_group = SourceMdlModelGroup()
            a_model_group.labelOffset = reader.read_int32()
            a_model_group.fileNameOffset = reader.read_int32()
            data.theModelGroups.append(a_model_group)

            input_file_stream_position = reader.position

            if a_model_group.labelOffset != 0:
                reader.seek(
                    include_model_input_file_stream_position + a_model_group.labelOffset,
                    SEEK_BEGIN,
                )
                a_model_group.theLabel = reader.read_null_terminated_string()
            else:
                a_model_group.theLabel = ""

            if a_model_group.fileNameOffset != 0:
                reader.seek(
                    include_model_input_file_stream_position
                    + a_model_group.fileNameOffset,
                    SEEK_BEGIN,
                )
                a_model_group.theFileName = reader.read_null_terminated_string()
            else:
                a_model_group.theFileName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    # ------------------------------------------------------------------
    # Flex controller UIs / key values / bone tables / bodygroup presets
    # ------------------------------------------------------------------
    def read_flex_controller_uis(self) -> None:
        """Implement ``ReadFlexControllerUis`` (3769-3818).

        The offset is skipped when it holds the Vindictus v44 magic
        (``text_SCAL_VERSION44Vindictus``) instead of a real file offset.
        """
        data = self.theMdlFileData
        if not (
            data.flexControllerUiCount > 0
            and data.flexControllerUiOffset != TEXT_SCAL_VERSION44_VINDICTUS
        ):
            return

        reader = self.theInputFileReader
        reader.seek(data.flexControllerUiOffset, SEEK_BEGIN)

        data.theFlexControllerUis = []
        for _ in range(data.flexControllerUiCount):
            flex_controller_ui_input_file_stream_position = reader.position
            a_flex_controller_ui = SourceMdlFlexControllerUi()
            a_flex_controller_ui.nameOffset = reader.read_int32()
            a_flex_controller_ui.config0 = reader.read_int32()
            a_flex_controller_ui.config1 = reader.read_int32()
            a_flex_controller_ui.config2 = reader.read_int32()
            a_flex_controller_ui.remapType = reader.read_byte()
            a_flex_controller_ui.controlIsStereo = reader.read_byte()
            a_flex_controller_ui.unused = [reader.read_byte() for _ in range(2)]
            data.theFlexControllerUis.append(a_flex_controller_ui)

            input_file_stream_position = reader.position

            if a_flex_controller_ui.nameOffset != 0:
                reader.seek(
                    flex_controller_ui_input_file_stream_position
                    + a_flex_controller_ui.nameOffset,
                    SEEK_BEGIN,
                )
                a_flex_controller_ui.theName = reader.read_null_terminated_string()
            else:
                a_flex_controller_ui.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_key_values(self) -> None:
        """Implement ``ReadKeyValues`` (3820-3839).

        ``keyValueSize`` includes the terminating NUL, so the text is
        ``keyValueSize - 1`` characters followed by one terminator character.  The
        result is the raw ``$keyvalues`` block; the QC writer re-indents it.
        """
        data = self.theMdlFileData
        if data.keyValueSize <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.keyValueOffset, SEEK_BEGIN)

        # NOTE: use -1 to drop the null terminator character.
        data.theKeyValuesText = reader.read_chars(data.keyValueSize - 1)
        # NOTE: read the null terminator character.
        reader.read_char()

    def read_bone_transforms(self) -> None:
        """Implement ``ReadBoneTransforms`` (3900-3979).

        One ``mstudiosrcbonetransform_t`` per realigned bone: a name offset, a
        ``matrix3x4_t`` pre-transform and a ``matrix3x4_t`` post-transform, both
        stored column-major as ``.x`` of all four columns, then ``.y``, then ``.z``.
        """
        data = self.theMdlFileData
        if data.sourceBoneTransformCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.sourceBoneTransformOffset, SEEK_BEGIN)

        data.theBoneTransforms = []
        for _ in range(data.sourceBoneTransformCount):
            bone_input_file_stream_position = reader.position
            a_bone_transform = SourceMdlBoneTransform()

            a_bone_transform.nameOffset = reader.read_int32()

            a_bone_transform.preTransformColumn0 = SourceVector()
            a_bone_transform.preTransformColumn1 = SourceVector()
            a_bone_transform.preTransformColumn2 = SourceVector()
            a_bone_transform.preTransformColumn3 = SourceVector()
            a_bone_transform.preTransformColumn0.x = reader.read_single()
            a_bone_transform.preTransformColumn1.x = reader.read_single()
            a_bone_transform.preTransformColumn2.x = reader.read_single()
            a_bone_transform.preTransformColumn3.x = reader.read_single()
            a_bone_transform.preTransformColumn0.y = reader.read_single()
            a_bone_transform.preTransformColumn1.y = reader.read_single()
            a_bone_transform.preTransformColumn2.y = reader.read_single()
            a_bone_transform.preTransformColumn3.y = reader.read_single()
            a_bone_transform.preTransformColumn0.z = reader.read_single()
            a_bone_transform.preTransformColumn1.z = reader.read_single()
            a_bone_transform.preTransformColumn2.z = reader.read_single()
            a_bone_transform.preTransformColumn3.z = reader.read_single()

            a_bone_transform.postTransformColumn0 = SourceVector()
            a_bone_transform.postTransformColumn1 = SourceVector()
            a_bone_transform.postTransformColumn2 = SourceVector()
            a_bone_transform.postTransformColumn3 = SourceVector()
            a_bone_transform.postTransformColumn0.x = reader.read_single()
            a_bone_transform.postTransformColumn1.x = reader.read_single()
            a_bone_transform.postTransformColumn2.x = reader.read_single()
            a_bone_transform.postTransformColumn3.x = reader.read_single()
            a_bone_transform.postTransformColumn0.y = reader.read_single()
            a_bone_transform.postTransformColumn1.y = reader.read_single()
            a_bone_transform.postTransformColumn2.y = reader.read_single()
            a_bone_transform.postTransformColumn3.y = reader.read_single()
            a_bone_transform.postTransformColumn0.z = reader.read_single()
            a_bone_transform.postTransformColumn1.z = reader.read_single()
            a_bone_transform.postTransformColumn2.z = reader.read_single()
            a_bone_transform.postTransformColumn3.z = reader.read_single()

            data.theBoneTransforms.append(a_bone_transform)

            input_file_stream_position = reader.position

            if a_bone_transform.nameOffset != 0:
                reader.seek(
                    bone_input_file_stream_position + a_bone_transform.nameOffset,
                    SEEK_BEGIN,
                )
                a_bone_transform.theName = reader.read_null_terminated_string()
            elif a_bone_transform.theName is None:
                a_bone_transform.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_linear_bone_table(self) -> None:
        """Implement ``ReadLinearBoneTable`` (4009-4229).

        Base address is ``studioHeader2Offset + linearBoneOffset``; the nine arrays
        hang off ``boneTableInputFileStreamPosition`` (the start of the
        ``mstudiolinearbone_t``), each 4-byte aligned, so every block is reached by
        an explicit seek.  The whole body is wrapped in the VB's ``Try``/``Catch``.
        """
        data = self.theMdlFileData
        if data.linearBoneOffset <= 0:
            return

        reader = self.theInputFileReader
        try:
            reader.seek(data.studioHeader2Offset + data.linearBoneOffset, SEEK_BEGIN)
            bone_table_input_file_stream_position = reader.position

            linear_bone_table = SourceMdlLinearBone()
            data.theLinearBoneTable = linear_bone_table
            linear_bone_table.boneCount = reader.read_int32()
            linear_bone_table.flagsOffset = reader.read_int32()
            linear_bone_table.parentOffset = reader.read_int32()
            linear_bone_table.posOffset = reader.read_int32()
            linear_bone_table.quatOffset = reader.read_int32()
            linear_bone_table.rotOffset = reader.read_int32()
            linear_bone_table.poseToBoneOffset = reader.read_int32()
            linear_bone_table.posScaleOffset = reader.read_int32()
            linear_bone_table.rotScaleOffset = reader.read_int32()
            linear_bone_table.qAlignmentOffset = reader.read_int32()
            linear_bone_table.unused = [reader.read_int32() for _ in range(6)]

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.flagsOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                linear_bone_table.theFlags.append(reader.read_int32())

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.parentOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                linear_bone_table.theParents.append(reader.read_int32())

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.posOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                position = SourceVector()
                position.x = reader.read_single()
                position.y = reader.read_single()
                position.z = reader.read_single()
                linear_bone_table.thePositions.append(position)

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.quatOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                quaternion = SourceQuaternion()
                quaternion.x = reader.read_single()
                quaternion.y = reader.read_single()
                quaternion.z = reader.read_single()
                quaternion.w = reader.read_single()
                linear_bone_table.theQuaternions.append(quaternion)

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.rotOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                rotation = SourceVector()
                rotation.x = reader.read_single()
                rotation.y = reader.read_single()
                rotation.z = reader.read_single()
                linear_bone_table.theRotations.append(rotation)

            reader.seek(
                bone_table_input_file_stream_position
                + linear_bone_table.poseToBoneOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                pose_to_bone_data_column0 = SourceVector()
                pose_to_bone_data_column1 = SourceVector()
                pose_to_bone_data_column2 = SourceVector()
                pose_to_bone_data_column3 = SourceVector()
                pose_to_bone_data_column0.x = reader.read_single()
                pose_to_bone_data_column1.x = reader.read_single()
                pose_to_bone_data_column2.x = reader.read_single()
                pose_to_bone_data_column3.x = reader.read_single()
                pose_to_bone_data_column0.y = reader.read_single()
                pose_to_bone_data_column1.y = reader.read_single()
                pose_to_bone_data_column2.y = reader.read_single()
                pose_to_bone_data_column3.y = reader.read_single()
                pose_to_bone_data_column0.z = reader.read_single()
                pose_to_bone_data_column1.z = reader.read_single()
                pose_to_bone_data_column2.z = reader.read_single()
                pose_to_bone_data_column3.z = reader.read_single()
                linear_bone_table.thePoseToBoneDataColumn0s.append(pose_to_bone_data_column0)
                linear_bone_table.thePoseToBoneDataColumn1s.append(pose_to_bone_data_column1)
                linear_bone_table.thePoseToBoneDataColumn2s.append(pose_to_bone_data_column2)
                linear_bone_table.thePoseToBoneDataColumn3s.append(pose_to_bone_data_column3)

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.posScaleOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                position_scale = SourceVector()
                position_scale.x = reader.read_single()
                position_scale.y = reader.read_single()
                position_scale.z = reader.read_single()
                linear_bone_table.thePositionScales.append(position_scale)

            reader.seek(
                bone_table_input_file_stream_position + linear_bone_table.rotScaleOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                rotation_scale = SourceVector()
                rotation_scale.x = reader.read_single()
                rotation_scale.y = reader.read_single()
                rotation_scale.z = reader.read_single()
                linear_bone_table.theRotationScales.append(rotation_scale)

            reader.seek(
                bone_table_input_file_stream_position
                + linear_bone_table.qAlignmentOffset,
                SEEK_BEGIN,
            )
            for _ in range(linear_bone_table.boneCount):
                q_alignment = SourceQuaternion()
                q_alignment.x = reader.read_single()
                q_alignment.y = reader.read_single()
                q_alignment.z = reader.read_single()
                q_alignment.w = reader.read_single()
                linear_bone_table.theQAlignments.append(q_alignment)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_bodygroup_presets(self) -> None:
        """Implement ``ReadBodygroupPresets`` (4231-4282).

        ``bodygroupPresetOffset`` is absolute: ``ReadMdlHeader02`` stores it as
        ``studioHeader2Offset``-relative + the structure's own start offset.
        """
        data = self.theMdlFileData
        if not (data.bodygroupPresetCount > 0 and data.bodygroupPresetOffset != 0):
            return

        reader = self.theInputFileReader
        try:
            reader.seek(data.bodygroupPresetOffset, SEEK_BEGIN)

            data.theBodygroupPresets = []
            for _ in range(data.bodygroupPresetCount):
                bodygroup_preset_input_file_stream_position = reader.position
                a_bodygroup_preset = SourceMdlBodygroupPreset()

                a_bodygroup_preset.nameOffset = reader.read_int32()
                a_bodygroup_preset.value = reader.read_int32()
                a_bodygroup_preset.mask = reader.read_int32()

                data.theBodygroupPresets.append(a_bodygroup_preset)

                input_file_stream_position = reader.position

                if a_bodygroup_preset.nameOffset != 0:
                    reader.seek(
                        bodygroup_preset_input_file_stream_position
                        + a_bodygroup_preset.nameOffset,
                        SEEK_BEGIN,
                    )
                    a_bodygroup_preset.theName = reader.read_null_terminated_string()
                elif a_bodygroup_preset.theName is None:
                    a_bodygroup_preset.theName = ""

                reader.seek(input_file_stream_position, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------
    def post_process(self) -> None:
        """Implement ``PostProcess`` (4337-4361).

        Marks a body part ``theModelCommandIsUsed`` when any of its models has
        eyeballs or any of its meshes has flexes, and marks
        ``theEyeballOptionIsUsed`` for eyeballs.  The VB ``Exit For`` statements mean
        the model loop stops at the first model that qualifies.
        """
        data = self.theMdlFileData

        # ``SourceMdlFileDataBase`` declares this; make sure it exists even for
        # models with no eyeballs at all.
        if not hasattr(data, "theUpAxisYCommandWasUsed"):
            data.theUpAxisYCommandWasUsed = False

        if data.theBodyParts is None:
            return

        for a_body_part in data.theBodyParts:
            for a_body_model in a_body_part.theModels:
                if a_body_model.theEyeballs is not None and len(a_body_model.theEyeballs) > 0:
                    a_body_part.theModelCommandIsUsed = True
                    a_body_part.theEyeballOptionIsUsed = True
                    break

                if a_body_model.theMeshes is not None:
                    for a_mesh in a_body_model.theMeshes:
                        if a_mesh.theFlexes is not None and len(a_mesh.theFlexes) > 0:
                            a_body_part.theModelCommandIsUsed = True
                            break
                    if a_body_part.theModelCommandIsUsed:
                        break
