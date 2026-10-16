"""Decode MDL and ANI bone-animation data.

How to use this module
----------------------

``class SourceMdlFile49AnimationMixin`` holds every animation reader that belongs to
``SourceMdlFile49``.  Its methods operate on ``self.theInputFileReader`` and
``self.theMdlFileData`` exactly like the VB class does, so the main MDL parser can
simply subclass it::

    from .ani import SourceMdlFile49AnimationMixin

    class SourceMdlFile49(SourceMdlFile49AnimationMixin):
        ...
        def read_mdl_file(self):
            self.read_bones()                     # MUST come first
            self.read_local_animation_descs()     # then, in this order:
            self.read_animation_sections()
            self.read_animation_mdl_blocks()

``SourceAniFile49`` / ``SourceAniFile44`` reuse the very same readers through the
shared ``_AnimationReaderBase`` -- exactly as ``SourceAniFile49`` inherits
``SourceMdlFile49`` in the VB original -- and add
``read_animation_ani_blocks()``, which resolves ANI-resident animation data through
the **MDL's** anim-block table.

Format notes
------------

1. Real ``.ani`` files begin with the magic **``IDAG``**, not ``IDST``, and their
   version equals the MDL's version (48 or 49).  the parser never validates the ANI ID
   (``SourceModel.ReadFile`` only opens a ``BinaryReader``), so this port does not
   either.
2. Every offset/count field in a real ``.ani`` header is **zero** (``boneCount``,
   ``localAnimationCount``, ``animBlockCount``, ...).  Consequently
   ``SourceAniFile49.theMdlFileData.theAnimBlocks`` stays ``None`` and **all**
   positional lookups must go through the *real MDL's* ``theAnimBlocks`` table
   (``self.theRealMdlFileData``).
3. ``theAnimBlocks[0]`` is the MDL-resident pseudo-block and is ``{0, 0}``; entries
   ``[n >= 1]`` hold **absolute byte offsets into the ``.ani`` file**.  The first ANI
   block normally starts at ``ALIGN16(sizeof(studiohdr_t)) = 416``, but that is never
   hard-coded here -- it is always resolved through ``theAnimBlocks[n].dataStart``.
4. The two animation passes (MDL and ANI) write into the *same*
   ``animdesc.theSectionsOfAnimations`` lists, so a sectioned animation whose first
   section lives in the MDL and whose remaining sections live in the ANI ends up
   fully populated by running both passes.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator, Mapping
from typing import Final, Optional

from .mdl_data import (
    SourceMdlAnimBlock,
    SourceMdlAnimationDesc49,
    SourceMdlBone,
    SourceMdlFileData49,
)
from .reader import BinaryReader, SEEK_BEGIN
from .vectors import SourceQuaternion, SourceVector

__all__ = [
    # -- quantization helpers ------------------------------------------------
    "source_float16bits",
    "source_float8bits",
    "SourceFloat16bits",
    "SourceVector48bits",
    "source_vector48bits",
    "SourceQuaternion48bits",
    "SourceQuaternion48bitsAssumedW",
    "SourceQuaternion48bitsSmallest3",
    "SourceQuaternion64bits",
    "SourceQuaternion48bitsViaBytes",
    "source_quaternion48bits",
    "source_quaternion48bits_assumed_w",
    "source_quaternion48bits_smallest3",
    "source_quaternion64bits",
    "euler_angles_to_quaternion",
    "to_euler_angles",
    "eul_from_quat",
    "eul_from_hmatrix",
    "vector_yaw_rotate",
    "align_long",
    "radians_to_degrees",
    "degrees_to_radians",
    # -- value / container classes ------------------------------------------
    "SourceMdlAnimationValue",
    "SourceMdlAnimationValuePointer",
    "SourceMdlAnimation",
    "SourceMdlAnimationSection",
    "SourceMdlMovement",
    "SourceMdlLocalHierarchy",
    "SourceMdlCompressedIkError",
    "SourceMdlIkRule",
    "BoneConstantInfo49",
    "BoneFrameDataInfo49",
    "SourceAniFrameAnim49",
    "FileSeekLog",
    # -- readers -------------------------------------------------------------
    "extract_anim_value",
    "SourceMdlFile49AnimationMixin",
    "SourceAniFile49",
    "SourceAniFile44",
]


# ===========================================================================
# Constants
# ===========================================================================

#: ``SourceMdlAnimation`` flag constants.
STUDIO_ANIM_RAWPOS: Final = 0x01  # Vector48
STUDIO_ANIM_RAWROT: Final = 0x02  # Quaternion48
STUDIO_ANIM_ANIMPOS: Final = 0x04  # mstudioanim_valueptr_t
STUDIO_ANIM_ANIMROT: Final = 0x08  # mstudioanim_valueptr_t
STUDIO_ANIM_DELTA: Final = 0x10
STUDIO_ANIM_RAWROT2: Final = 0x20  # Quaternion64

#: ``SourceMdlAnimationDesc`` flags (only the ones used by the animation decode).
STUDIO_LOOPING: Final = 0x1
STUDIO_SNAP: Final = 0x2
STUDIO_DELTA: Final = 0x4
STUDIO_ALLZEROS: Final = 0x20
STUDIO_FRAMEANIM: Final = 0x40

#: ``SourceAniFrameAnim49`` per-bone flag constants.
STUDIO_FRAME_RAWPOS: Final = 0x01  # Vector48 in constants
STUDIO_FRAME_RAWROT: Final = 0x02  # Quaternion48 in constants
STUDIO_FRAME_ANIMPOS: Final = 0x04  # Vector48 in framedata
STUDIO_FRAME_ANIMROT: Final = 0x08  # Quaternion48 in framedata
STUDIO_FRAME_FULLANIMPOS: Final = 0x10  # Vector in framedata
STUDIO_FRAME_CONST_POS2: Final = 0x20  # Vector in constants
STUDIO_FRAME_CONST_ROT2: Final = 0x40  # Quaternion48S in constants
STUDIO_FRAME_ANIM_ROT2: Final = 0x80  # Quaternion48S in framedata

#: The magic "no more bones in this section" marker in an ``mstudioanim_t`` list.
ANIM_BONE_INDEX_TERMINATOR: Final = 255

#: ``MathModule.FLT_EPSILON`` -- the gimbal-lock threshold used by ``Eul_FromHMatrix``.
FLT_EPSILON: Final = 0.00001

#: ``MathModule`` radians/degrees conversion factors.
_PI: Final = 3.1415926535897931
_DEG_TO_RAD: Final = _PI / 180.0
_RAD_TO_DEG: Final = 180.0 / _PI

#: ``SourceQuaternion48bitsViaBytes`` components (``MISSING_COMPONENT_*``).
MISSING_COMPONENT_W: Final = 0
MISSING_COMPONENT_X: Final = 1
MISSING_COMPONENT_Y: Final = 2
MISSING_COMPONENT_Z: Final = 3

#: ``SourceQuaternion48bitsViaBytes`` scale/shift.
SCALE48S: Final = 23168.0
SHIFT48S: Final = 16384

#: Default number of entries in ``SourceMdlAnimationDesc49unused1`` (5 int32 for v49).
_ANIMDESC_UNUSED1_LENGTH: Final = 5


def align_long(current_value: int, alignment_value: int) -> int:
    """Implement ``MathModule.AlignLong`` -- round *up* to the next alignment."""
    return (current_value + alignment_value - 1) & ~(alignment_value - 1)


def degrees_to_radians(degrees: float) -> float:
    """Implement ``MathModule.DegreesToRadians``."""
    return degrees * _DEG_TO_RAD


def radians_to_degrees(radians: float) -> float:
    """Implement ``MathModule.RadiansToDegrees``."""
    return radians * _RAD_TO_DEG


def vector_yaw_rotate(input_vector: SourceVector, yaw_in_radians: float) -> SourceVector:
    """Implement ``MathModule.VectorYawRotate``."""
    sy = math.sin(yaw_in_radians)
    cy = math.cos(yaw_in_radians)
    output = SourceVector()
    output.x = input_vector.x * cy - input_vector.y * sy
    output.y = input_vector.x * sy + input_vector.y * cy
    output.z = input_vector.z
    return output


# ===========================================================================
# Quantization helpers
# ===========================================================================


def _int32_as_single(bits: int) -> float:
    """Reinterpret a 32-bit pattern as a ``Single``.

    Mirrors the ``IntegerAndSingleUnion`` explicit-layout unions that the parser uses
    everywhere the original C++ code had a ``float32bits`` union.
    """
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def source_float16bits(the16bitvalue: int) -> float:
    """Decode a 16-bit half float the way ``SourceFloat16bits.TheFloatValue`` does.

    ``SourceFloat16bits`` implements the C++ ``Convert16bitFloatTo32bits``:

    * exponent 31 / mantissa 0 -> ``maxfloat16bits * sign`` (``+/-65504.0``);
    * exponent 31 / mantissa != 0 -> ``0.0`` (NaN maps to zero);
    * exponent 0 / mantissa != 0 -> denormal:
      ``sign * (mantissa / 1024.0F) * (1.0F / 16384.0F)``, all in ``Single``;
    * otherwise rebuild the float32 bit pattern and reinterpret it.

    The float32 re-interpretation is bit-exact with IEEE binary16 conversion for the
    denormal case too, so a plain ``struct`` round-trip is used for clarity.
    """
    return struct.unpack("<e", struct.pack("<H", the16bitvalue & 0xFFFF))[0]


def source_float8bits(the8bitvalue: int) -> float:
    """Decode an 8-bit float the way ``SourceFloat8bits.TheFloatValue`` does.

    NOTE: ported for completeness;
    the parser's bone-animation readers never use it.  ``SourceFloat8bits`` is *not* an
    IEEE 8-bit format: exponent 15 has no infinity distinction from max-float other
    than the mantissa test, and the denormal scale is ``1/8``.
    """
    value = the8bitvalue & 0xFF
    sign = (value & 0x80) >> 7
    mantissa = value & 0x7
    biased_exponent = (value & 0x78) >> 3
    float_sign = -1 if sign == 1 else 1

    if biased_exponent == 15 and mantissa == 0:
        return 122880.0 * float_sign  # maxfloat8bits
    if biased_exponent == 15 and mantissa != 0:
        return 0.0

    half_denorm = 1.0 / 8.0
    if biased_exponent == 0 and mantissa != 0:
        float_mantissa = _single(mantissa / 8.0)
        return float_sign * float_mantissa * half_denorm

    result_mantissa = mantissa << (23 - 3)
    if biased_exponent == 0:
        result_biased_exponent = 0
    else:
        result_biased_exponent = (biased_exponent - 7 + 127) << 23
    result_sign = sign << 31
    return _int32_as_single(result_sign | result_biased_exponent | result_mantissa)


def _single(value: float) -> float:
    """Narrow a Python float to ``Single`` exactly like a VB.NET ``Single`` local."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


class SourceFloat16bits:
    """``SourceFloat16bits`` -- one 16-bit half float."""

    __slots__ = ("the16BitValue",)

    def __init__(self, the16BitValue: int = 0) -> None:
        self.the16BitValue = the16BitValue

    @property
    def TheFloatValue(self) -> float:
        """Implement the VB property of the same (PascalCase) name."""
        return source_float16bits(self.the16BitValue)


class SourceVector48bits:
    """``SourceVector48bits`` -- a 3D vector stored as three half floats (6 bytes)."""

    __slots__ = ("theXInput", "theYInput", "theZInput")

    def __init__(self) -> None:
        self.theXInput = SourceFloat16bits()
        self.theYInput = SourceFloat16bits()
        self.theZInput = SourceFloat16bits()

    @property
    def x(self) -> float:
        return self.theXInput.TheFloatValue

    @property
    def y(self) -> float:
        return self.theYInput.TheFloatValue

    @property
    def z(self) -> float:
        return self.theZInput.TheFloatValue

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceVector48bits({self.x!r}, {self.y!r}, {self.z!r})"


def source_vector48bits(x_input: int, y_input: int, z_input: int) -> tuple[float, float, float]:
    """Decode a ``Vector48`` from its three raw ``uint16`` halves."""
    return (
        source_float16bits(x_input),
        source_float16bits(y_input),
        source_float16bits(z_input),
    )


class SourceQuaternion48bits:
    """``SourceQuaternion48bits`` -- ``x:16, y:16, z:15, wneg:1`` (6 bytes)."""

    __slots__ = ("theXInput", "theYInput", "theZWInput")

    def __init__(self, theXInput: int = 0, theYInput: int = 0, theZWInput: int = 0) -> None:
        self.theXInput = theXInput
        self.theYInput = theYInput
        self.theZWInput = theZWInput

    @property
    def x(self) -> float:
        return (self.theXInput - 32768) * (1 / 32768)

    @property
    def y(self) -> float:
        return (self.theYInput - 32768) * (1 / 32768)

    @property
    def z(self) -> float:
        z_input = self.theZWInput & 0x7FFF
        return (z_input - 16384) * (1 / 16384)

    @property
    def w(self) -> float:
        return math.sqrt(1 - self.x * self.x - self.y * self.y - self.z * self.z) * self.wneg

    @property
    def wneg(self) -> float:
        return -1.0 if (self.theZWInput & 0x8000) > 0 else 1.0

    @property
    def quaternion(self) -> SourceQuaternion:
        return SourceQuaternion(self.x, self.y, self.z, self.w)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceQuaternion48bits({self.x!r}, {self.y!r}, {self.z!r}, {self.w!r})"


def source_quaternion48bits(
    x_input: int, y_input: int, z_w_input: int
) -> tuple[float, float, float, float]:
    """Decode a ``Quaternion48`` (``x:16, y:16, z:15, wneg:1``)."""
    return SourceQuaternion48bits(x_input, y_input, z_w_input).quaternion.as_tuple()


class SourceQuaternion48bitsAssumedW:
    """``SourceQuaternion48bitsAssumedW`` -- like Quaternion48 but ``z`` is 16-bit too.

    All three components are ``((raw) - 32768) * (1/32768)`` and ``w`` is always the
    positive root.  the parser never instantiates this class in the v44..v49 animation
    readers; it is ported because the task lists it.
    """

    __slots__ = ("theXInput", "theYInput", "theZWInput")

    def __init__(self, theXInput: int = 0, theYInput: int = 0, theZWInput: int = 0) -> None:
        self.theXInput = theXInput
        self.theYInput = theYInput
        self.theZWInput = theZWInput

    @property
    def x(self) -> float:
        return (self.theXInput - 32768) * (1 / 32768)

    @property
    def y(self) -> float:
        return (self.theYInput - 32768) * (1 / 32768)

    @property
    def z(self) -> float:
        return (self.theZWInput - 32768) * (1 / 32768)

    @property
    def w(self) -> float:
        return math.sqrt(1 - self.x * self.x - self.y * self.y - self.z * self.z)

    @property
    def quaternion(self) -> SourceQuaternion:
        return SourceQuaternion(self.x, self.y, self.z, self.w)


def source_quaternion48bits_assumed_w(
    x_input: int, y_input: int, z_w_input: int
) -> tuple[float, float, float, float]:
    """Decode the ``Quaternion48`` "assumed W" variant."""
    return SourceQuaternion48bitsAssumedW(x_input, y_input, z_w_input).quaternion.as_tuple()


class SourceQuaternion48bitsSmallest3:
    """``SourceQuaternion48bitsSmallest3`` -- "smallest three" 48-bit quaternion.

    Three packed ``uint16`` inputs hold a 2-bit missing-component index plus three
    components, each of which is expanded to a 14-bit signed-ish value scaled by
    ``Maximum - Minimum`` (``+/-1/sqrt(2)``).  ``w`` of the result is the positive
    root, the missing component replaces one of x/y/z/w.

    NOTE: this is the *live* implementation of ``SourceQuaternion48bitsSmallest3``
    (the alternative bit-splitting variant in that file is commented out).  The
    animation readers never use it; it is ported because the task lists it.
    """

    __slots__ = ("theAInput", "theBInput", "theCInput")

    MISSING_COMPONENT_X: Final = 0
    MISSING_COMPONENT_Y: Final = 1
    MISSING_COMPONENT_Z: Final = 2
    MISSING_COMPONENT_W: Final = 3

    Minimum: Final = -1 / 1.414214
    Maximum: Final = 1 / 1.414214

    def __init__(self, theAInput: int = 0, theBInput: int = 0, theCInput: int = 0) -> None:
        self.theAInput = theAInput
        self.theBInput = theBInput
        self.theCInput = theCInput

    def compute_quaternion(self) -> SourceQuaternion:
        """Implement ``ComputeQuaternion``."""
        missing_component_type = (self.theAInput & 0xC000) >> 14
        integer_a = (self.theAInput & 0x1FFF) | ((self.theBInput & 0xC000) >> 14)
        integer_b = (self.theBInput & 0x3FFF) | ((self.theCInput & 0x8000) >> 15)
        integer_c = (self.theCInput & 0x7FFF) >> 1

        a = integer_a / 1024.0 * (self.Maximum - self.Minimum) + self.Minimum
        b = integer_b / 1024.0 * (self.Maximum - self.Minimum) + self.Minimum
        c = integer_c / 1024.0 * (self.Maximum - self.Minimum) + self.Minimum

        # GetMissingComponent(a, b, c) == sqrt(1 - a*a - b*b - c*c)
        quaternion = SourceQuaternion()
        if missing_component_type == self.MISSING_COMPONENT_X:
            quaternion.x = math.sqrt(1 - a * a - b * b - c * c)
            quaternion.y = a
            quaternion.z = b
            quaternion.w = c
        elif missing_component_type == self.MISSING_COMPONENT_Y:
            quaternion.x = a
            quaternion.y = math.sqrt(1 - a * a - b * b - c * c)
            quaternion.z = b
            quaternion.w = c
        elif missing_component_type == self.MISSING_COMPONENT_Z:
            quaternion.x = a
            quaternion.y = b
            quaternion.z = math.sqrt(1 - a * a - b * b - c * c)
            quaternion.w = c
        elif missing_component_type == self.MISSING_COMPONENT_W:
            quaternion.x = a
            quaternion.y = b
            quaternion.z = c
            quaternion.w = math.sqrt(1 - a * a - b * b - c * c)
        return quaternion

    @property
    def quaternion(self) -> SourceQuaternion:
        return self.compute_quaternion()


def source_quaternion48bits_smallest3(
    a_input: int, b_input: int, c_input: int
) -> tuple[float, float, float, float]:
    """Decode a "smallest three" 48-bit quaternion."""
    return SourceQuaternion48bitsSmallest3(a_input, b_input, c_input).compute_quaternion().as_tuple()


class SourceQuaternion64bits:
    """``SourceQuaternion64bits`` -- ``x:21, y:21, z:21, wneg:1`` (8 bytes).

    The bit layout below is the *active* one in ``SourceQuaternion64bits``
    (``byte0/1/2`` for x, ``byte2/3/4/5`` for y, ``byte5/6/7`` for z, ``byte7`` bit 7
    for the w sign); the reversed-order variants in that file are commented out.
    """

    __slots__ = ("theBytes",)

    def __init__(self, theBytes: bytes = b"") -> None:
        self.theBytes = theBytes

    def _b(self, index: int) -> int:
        return self.theBytes[index] & 0xFF

    @property
    def x(self) -> float:
        byte0 = self._b(0) & 0xFF
        byte1 = (self._b(1) & 0xFF) << 8
        byte2 = (self._b(2) & 0x1F) << 16
        i = byte2 | byte1 | byte0
        return (i - 1048576) * (1 / 1048576.5)

    @property
    def y(self) -> float:
        byte2 = (self._b(2) & 0xE0) >> 5
        byte3 = (self._b(3) & 0xFF) << 3
        byte4 = (self._b(4) & 0xFF) << 11
        byte5 = (self._b(5) & 0x3) << 19
        i = byte5 | byte4 | byte3 | byte2
        return (i - 1048576) * (1 / 1048576.5)

    @property
    def z(self) -> float:
        byte5 = (self._b(5) & 0xFC) >> 2
        byte6 = (self._b(6) & 0xFF) << 6
        byte7 = (self._b(7) & 0x7F) << 14
        i = byte7 | byte6 | byte5
        return (i - 1048576) * (1 / 1048576.5)

    @property
    def w(self) -> float:
        return math.sqrt(1 - self.x * self.x - self.y * self.y - self.z * self.z) * self.wneg

    @property
    def wneg(self) -> float:
        return -1.0 if (self._b(7) & 0x80) > 0 else 1.0

    @property
    def quaternion(self) -> SourceQuaternion:
        return SourceQuaternion(self.x, self.y, self.z, self.w)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SourceQuaternion64bits({self.x!r}, {self.y!r}, {self.z!r}, {self.w!r})"
        )


def source_quaternion64bits(the_bytes: bytes) -> tuple[float, float, float, float]:
    """Decode a ``Quaternion64`` from its 8 raw bytes."""
    return SourceQuaternion64bits(the_bytes).quaternion.as_tuple()


class SourceQuaternion48bitsViaBytes:
    """``SourceQuaternion48bitsViaBytes`` -- Quaternion48S, "sorted" 48-bit quaternion.

    Used for the frame-anim ``STUDIO_FRAME_CONST_ROT2`` (0x40) and
    ``STUDIO_FRAME_ANIM_ROT2`` (0x80) payloads.  The six bytes hold three 15-bit
    fields (``a``, ``b``, ``c``) plus a 2-bit index of the missing (largest)
    component split across two spare bits, and a sign bit for it.
    """

    __slots__ = ("theBytes",)

    MISSING_COMPONENT_W: Final = MISSING_COMPONENT_W
    MISSING_COMPONENT_X: Final = MISSING_COMPONENT_X
    MISSING_COMPONENT_Y: Final = MISSING_COMPONENT_Y
    MISSING_COMPONENT_Z: Final = MISSING_COMPONENT_Z

    def __init__(self, theBytes: bytes = b"") -> None:
        self.theBytes = theBytes

    def _b(self, index: int) -> int:
        return self.theBytes[index]

    def compute_quaternion(self) -> SourceQuaternion:
        """Implement ``ComputeQuaternion`` (note: the VB caches but never sets the flag)."""
        temp_integer = self._b(1) & 0x7F
        temp_integer2 = self._b(0)
        u_integer_a = (temp_integer << 8) | temp_integer2

        temp_integer = self._b(3) & 0x7F
        temp_integer2 = self._b(2)
        u_integer_b = (temp_integer << 8) | temp_integer2

        temp_integer = self._b(5) & 0x7F
        temp_integer2 = self._b(4)
        u_integer_c = (temp_integer << 8) | temp_integer2

        temp_integer = self._b(1) & 0x80
        temp_integer2 = self._b(3) & 0x80
        missing_component_index = (temp_integer >> 6) | (temp_integer2 >> 7)
        missing_component_sign = -1.0 if (self._b(5) & 0x80) > 0 else 1.0

        a = (u_integer_a - SHIFT48S) / SCALE48S
        b = (u_integer_b - SHIFT48S) / SCALE48S
        c = (u_integer_c - SHIFT48S) / SCALE48S

        def missing() -> float:
            return math.sqrt(1 - a * a - b * b - c * c) * missing_component_sign

        quaternion = SourceQuaternion()
        if missing_component_index == MISSING_COMPONENT_X:
            quaternion.x = missing()
            quaternion.y = a
            quaternion.z = b
            quaternion.w = c
        elif missing_component_index == MISSING_COMPONENT_Y:
            quaternion.x = c
            quaternion.y = missing()
            quaternion.z = a
            quaternion.w = b
        elif missing_component_index == MISSING_COMPONENT_Z:
            quaternion.x = b
            quaternion.y = c
            quaternion.z = missing()
            quaternion.w = a
        elif missing_component_index == MISSING_COMPONENT_W:
            quaternion.x = a
            quaternion.y = b
            quaternion.z = c
            quaternion.w = missing()
        return quaternion

    @property
    def quaternion(self) -> SourceQuaternion:
        return self.compute_quaternion()

    @property
    def x(self) -> float:
        return self.compute_quaternion().x

    @property
    def y(self) -> float:
        return self.compute_quaternion().y

    @property
    def z(self) -> float:
        return self.compute_quaternion().z

    @property
    def w(self) -> float:
        return self.compute_quaternion().w


# ===========================================================================
# Euler-angle conversions (MathModule)
# ===========================================================================


def euler_angles_to_quaternion(angle_vector: SourceVector) -> SourceQuaternion:
    """Implement ``MathModule.EulerAnglesToQuaternion``.

    ``angleVector`` holds ``x`` = pitch, ``y`` = yaw, ``z`` = roll, in radians.
    """
    f_pitch = angle_vector.x
    f_yaw = angle_vector.y
    f_roll = angle_vector.z

    f_sin_pitch = math.sin(f_pitch * 0.5)
    f_cos_pitch = math.cos(f_pitch * 0.5)
    f_sin_yaw = math.sin(f_yaw * 0.5)
    f_cos_yaw = math.cos(f_yaw * 0.5)
    f_sin_roll = math.sin(f_roll * 0.5)
    f_cos_roll = math.cos(f_roll * 0.5)
    f_cos_pitch_cos_yaw = f_cos_pitch * f_cos_yaw
    f_sin_pitch_sin_yaw = f_sin_pitch * f_sin_yaw

    return SourceQuaternion(
        f_sin_roll * f_cos_pitch_cos_yaw - f_cos_roll * f_sin_pitch_sin_yaw,
        f_cos_roll * f_sin_pitch * f_cos_yaw + f_sin_roll * f_cos_pitch * f_sin_yaw,
        f_cos_roll * f_cos_pitch * f_sin_yaw - f_sin_roll * f_sin_pitch * f_cos_yaw,
        f_cos_roll * f_cos_pitch_cos_yaw + f_sin_roll * f_sin_pitch_sin_yaw,
    )


def eul_from_hmatrix(
    matrix: list[list[float]],
    i: int,
    j: int,
    k: int,
    h: int,
    parity_odd: bool,
    repeat: bool,
    frame_r: bool,
) -> SourceVector:
    """Implement ``MathModule.Eul_FromHMatrix``.

    ``matrix`` is the 4x4 ``M[W][W]`` matrix built by :func:`eul_from_quat`.
    ``parity_odd``/``repeat``/``frame_r`` stand in for ``EulerParity`` /
    ``EulerRepeat`` / ``EulerFrame`` (the enums are ``Even``/``Odd``,
    ``No``/``Yes``, ``S``/``R``).  ``h`` is unused by the original as well.
    """
    ea = SourceVector()

    if repeat:
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

    if parity_odd:
        ea.x = -ea.x
        ea.y = -ea.y
        ea.z = -ea.z

    if frame_r:
        t = ea.x
        ea.x = ea.z
        ea.z = t

    # 'ea.w = order
    return ea


def eul_from_quat(
    q: SourceQuaternion,
    i: int,
    j: int,
    k: int,
    h: int,
    parity_odd: bool,
    repeat: bool,
    frame_r: bool,
) -> SourceVector:
    """Implement ``MathModule.Eul_FromQuat``."""
    matrix = [[0.0] * 4 for _ in range(4)]

    nq = q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w
    s = (2.0 / nq) if nq > 0 else 0.0

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

    return eul_from_hmatrix(matrix, i, j, k, h, parity_odd, repeat, frame_r)


def to_euler_angles(q: SourceQuaternion) -> SourceVector:
    """Implement ``MathModule.ToEulerAngles``.

    .. warning::

       ``MathModule.ToEulerAngles`` contains more than a dozen commented-out
       parameter sets.  **Only one line is live**::

           Return MathModule.Eul_FromQuat(q, 0, 1, 2, 0, EulerParity.Even, _
                                          EulerRepeat.No, EulerFrame.S)

       i.e. ``i=0, j=1, k=2, h=0``, ``parity=Even``, ``repeat=No``, ``frame=S``.
       There is deliberately no parity or frame swap, and the result is in
       **radians** (the name is misleading -- the parser writes those radians straight
       into the emitted ``.smd`` rotation columns).
    """
    return eul_from_quat(
        q, i=0, j=1, k=2, h=0, parity_odd=False, repeat=False, frame_r=False
    )


# ===========================================================================
# RLE (bone x frame) value containers
# ===========================================================================


class SourceMdlAnimationValue:
    """``SourceMdlAnimationValue`` -- the ``mstudioanimvalue_t`` union.

    ``<FieldOffset(0)> valid``, ``<FieldOffset(1)> total`` and ``<FieldOffset(0)>
    value`` overlap, so a port must store all three: one disk element is a
    little-endian ``int16`` whose low byte is ``valid`` and high byte is ``total``.
    """

    __slots__ = ("value",)

    def __init__(self, value: int = 0) -> None:
        self.value = value

    @property
    def valid(self) -> int:
        return self.value & 0xFF

    @property
    def total(self) -> int:
        return (self.value >> 8) & 0xFF

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceMdlAnimationValue(value={self.value}, valid={self.valid}, total={self.total})"


class SourceMdlAnimationValuePointer:
    """``SourceMdlAnimationValuePointer`` -- ``mstudioanim_valueptr_t``.

    The three offsets are **signed int16** values relative to the position of
    ``animXValueOffset`` itself (the start of this struct), and only read when ``> 0``.
    """

    __slots__ = (
        "animXValueOffset",
        "animYValueOffset",
        "animZValueOffset",
        "theAnimXValues",
        "theAnimYValues",
        "theAnimZValues",
    )

    def __init__(self) -> None:
        self.animXValueOffset = 0
        self.animYValueOffset = 0
        self.animZValueOffset = 0
        self.theAnimXValues: list[SourceMdlAnimationValue] = []
        self.theAnimYValues: list[SourceMdlAnimationValue] = []
        self.theAnimZValues: list[SourceMdlAnimationValue] = []


class SourceMdlAnimation:
    """``SourceMdlAnimation`` -- one ``mstudioanim_t`` record (plus its RLE streams)."""

    __slots__ = (
        "boneIndex",
        "flags",
        "nextSourceMdlAnimationOffset",
        "theRotV",
        "thePosV",
        "theRot48bits",
        "theRot64bits",
        "thePos",
    )

    # flag constants live on the class, exactly as in ``SourceMdlAnimation``
    STUDIO_ANIM_RAWPOS: Final = STUDIO_ANIM_RAWPOS
    STUDIO_ANIM_RAWROT: Final = STUDIO_ANIM_RAWROT
    STUDIO_ANIM_ANIMPOS: Final = STUDIO_ANIM_ANIMPOS
    STUDIO_ANIM_ANIMROT: Final = STUDIO_ANIM_ANIMROT
    STUDIO_ANIM_DELTA: Final = STUDIO_ANIM_DELTA
    STUDIO_ANIM_RAWROT2: Final = STUDIO_ANIM_RAWROT2

    def __init__(self) -> None:
        self.boneIndex = 0  # uint8
        self.flags = 0  # uint8
        self.nextSourceMdlAnimationOffset = 0  # int16 (signed on use)
        self.theRotV: Optional[SourceMdlAnimationValuePointer] = None
        self.thePosV: Optional[SourceMdlAnimationValuePointer] = None
        self.theRot48bits: Optional[SourceQuaternion48bits] = None
        self.theRot64bits: Optional[SourceQuaternion64bits] = None
        self.thePos: Optional[SourceVector48bits] = None


class SourceMdlAnimationSection:
    """``SourceMdlAnimationSection`` -- one ``mstudioanimsections_t``."""

    __slots__ = ("animBlock", "animOffset")

    def __init__(self) -> None:
        self.animBlock = 0
        self.animOffset = 0


class SourceMdlMovement:
    """``SourceMdlMovement`` -- one 40-byte movement record of an animdesc."""

    STUDIO_X: Final = 0x1
    STUDIO_Y: Final = 0x2
    STUDIO_Z: Final = 0x4
    STUDIO_XR: Final = 0x8
    STUDIO_YR: Final = 0x10
    STUDIO_ZR: Final = 0x20
    STUDIO_LX: Final = 0x40
    STUDIO_LY: Final = 0x80
    STUDIO_LZ: Final = 0x100
    STUDIO_LXR: Final = 0x200
    STUDIO_LYR: Final = 0x400
    STUDIO_LZR: Final = 0x800
    STUDIO_LINEAR: Final = 0x1000
    STUDIO_QUADRATIC_MOTION: Final = 0x2000

    __slots__ = ("endframeIndex", "motionFlags", "v0", "v1", "angle", "vector", "position")

    def __init__(self) -> None:
        self.endframeIndex = 0
        self.motionFlags = 0
        self.v0 = 0.0
        self.v1 = 0.0
        self.angle = 0.0
        self.vector = SourceVector()
        self.position = SourceVector()


class SourceMdlLocalHierarchy:
    """``SourceMdlLocalHierarchy`` -- one ``mstudiolocalhierarchy_t``."""

    __slots__ = (
        "boneIndex",
        "boneNewParentIndex",
        "startInfluence",
        "peakInfluence",
        "tailInfluence",
        "endInfluence",
        "startFrameIndex",
        "localAnimOffset",
        "unused",
    )

    def __init__(self) -> None:
        self.boneIndex = 0
        self.boneNewParentIndex = 0
        self.startInfluence = 0.0
        self.peakInfluence = 0.0
        self.tailInfluence = 0.0
        self.endInfluence = 0.0
        self.startFrameIndex = 0
        self.localAnimOffset = 0
        self.unused: list[int] = [0] * 4


class SourceMdlCompressedIkError:
    """``SourceMdlCompressedIkError`` -- compressed IK-error data of one IK rule."""

    __slots__ = ("scale", "offset", "theAnimValues", "theOffsetStart", "theChannelSpans")

    def __init__(self) -> None:
        self.theOffsetStart = -1
        self.theChannelSpans = [None] * 6
        self.scale: list[float] = [0.0] * 6
        self.offset: list[int] = [0] * 6
        self.theAnimValues: list[Optional[list[SourceMdlAnimationValue]]] = [None] * 6


class SourceMdlIkRule:
    """``SourceMdlIkRule`` -- one ``mstudioikrule_t`` (152 bytes in the v49 reader)."""

    __slots__ = (
        "index",
        "type",
        "chain",
        "bone",
        "slot",
        "height",
        "radius",
        "floor",
        "pos",
        "q",
        "compressedIkErrorOffset",
        "theOffsetStart",
        "theOffsetEnd",
        "unused2",
        "ikErrorIndexStart",
        "ikErrorOffset",
        "influenceStart",
        "influencePeak",
        "influenceTail",
        "influenceEnd",
        "unused3",
        "contact",
        "drop",
        "top",
        "unused6",
        "unused7",
        "unused8",
        "attachmentNameOffset",
        "unused",
        "theAttachmentName",
        "theCompressedIkError",
    )

    def __init__(self) -> None:
        self.index = 0
        self.type = 0
        self.chain = 0
        self.bone = 0
        self.slot = 0
        self.height = 0.0
        self.radius = 0.0
        self.floor = 0.0
        self.pos = SourceVector()
        self.q = SourceQuaternion()
        self.theOffsetStart = -1
        self.theOffsetEnd = -1
        self.compressedIkErrorOffset = 0
        self.unused2 = 0
        self.ikErrorIndexStart = 0
        self.ikErrorOffset = 0
        self.influenceStart = 0.0
        self.influencePeak = 0.0
        self.influenceTail = 0.0
        self.influenceEnd = 0.0
        self.unused3 = 0.0
        self.contact = 0.0
        self.drop = 0.0
        self.top = 0.0
        self.unused6 = 0
        self.unused7 = 0
        self.unused8 = 0
        self.attachmentNameOffset = 0
        self.unused: list[int] = [0] * 7
        self.theAttachmentName = ""
        self.theCompressedIkError: Optional[SourceMdlCompressedIkError] = None


# ===========================================================================
# Frame-anim (frame x bone) containers
# ===========================================================================


class BoneConstantInfo49:
    """``BoneConstantInfo49`` -- the per-bone constant payload of a frame-anim section."""

    __slots__ = (
        "theConstantRawPos",
        "theConstantRawRot",
        "theConstantPosition2",
        "theConstantRotation2",
    )

    def __init__(self) -> None:
        self.theConstantRawPos: Optional[SourceVector48bits] = None
        self.theConstantRawRot: Optional[SourceQuaternion48bits] = None
        self.theConstantPosition2: Optional[SourceVector] = None
        self.theConstantRotation2: Optional[SourceQuaternion48bitsViaBytes] = None


class BoneFrameDataInfo49:
    """``BoneFrameDataInfo49`` -- the per-frame/per-bone payload of a frame-anim section."""

    __slots__ = (
        "theAnimPosition",
        "theAnimRotation",
        "theFullAnimPosition",
        "theAnimRotationUnknown",
    )

    def __init__(self) -> None:
        self.theAnimPosition: Optional[SourceVector48bits] = None
        self.theAnimRotation: Optional[SourceQuaternion48bits] = None
        self.theFullAnimPosition: Optional[SourceVector] = None
        self.theAnimRotationUnknown: Optional[SourceQuaternion48bitsViaBytes] = None


class SourceAniFrameAnim49:
    """``SourceAniFrameAnim49`` -- one ``mstudio_frame_anim_t`` section.

    ``theBoneFlags`` / ``theBoneConstantInfos`` are indexed by **global** bone index;
    ``theBoneFrameDataInfos`` is indexed by section-local frame index and then by
    global bone index.
    """

    STUDIO_FRAME_RAWPOS: Final = STUDIO_FRAME_RAWPOS
    STUDIO_FRAME_RAWROT: Final = STUDIO_FRAME_RAWROT
    STUDIO_FRAME_ANIMPOS: Final = STUDIO_FRAME_ANIMPOS
    STUDIO_FRAME_ANIMROT: Final = STUDIO_FRAME_ANIMROT
    STUDIO_FRAME_FULLANIMPOS: Final = STUDIO_FRAME_FULLANIMPOS
    STUDIO_FRAME_CONST_POS2: Final = STUDIO_FRAME_CONST_POS2
    STUDIO_FRAME_CONST_ROT2: Final = STUDIO_FRAME_CONST_ROT2
    STUDIO_FRAME_ANIM_ROT2: Final = STUDIO_FRAME_ANIM_ROT2

    __slots__ = (
        "constantsOffset",
        "frameOffset",
        "frameLength",
        "unused",
        "theBoneFlags",
        "theBoneConstantInfos",
        "theBoneFrameDataInfos",
    )

    def __init__(self) -> None:
        self.constantsOffset = 0
        self.frameOffset = 0
        self.frameLength = 0
        self.unused: list[int] = [0, 0, 0]
        self.theBoneFlags: list[int] = []
        self.theBoneConstantInfos: list[BoneConstantInfo49] = []
        self.theBoneFrameDataInfos: list[list[BoneFrameDataInfo49]] = []


# ===========================================================================
# File-seek log (the parser's ``FileSeekLog`` -- used for the alignment side effect)
# ===========================================================================


class _OffsetRanges(Mapping[int, str]):
    """Byte-addressable diagnostics backed by ranges, with last-write wins."""

    __slots__ = ("_ranges",)

    def __init__(self) -> None:
        self._ranges: list[tuple[int, int, str]] = []

    def add(self, start: int, end: int, description: str) -> None:
        self._ranges.append((start, end, description))

    def __getitem__(self, offset: int) -> str:
        for start, end, description in reversed(self._ranges):
            if start <= offset <= end:
                return description
        raise KeyError(offset)

    def covered_ranges(self) -> Iterator[tuple[int, int]]:
        """Merge coverage without visiting individual bytes or losing labels."""
        start = end = None
        for next_start, next_end, _ in sorted(self._ranges):
            if end is None:
                start, end = next_start, next_end
            elif next_start <= end + 1:
                end = max(end, next_end)
            else:
                yield start, end
                start, end = next_start, next_end
        if end is not None:
            yield start, end

    def __iter__(self) -> Iterator[int]:
        for start, end in self.covered_ranges():
            yield from range(start, end + 1)

    def __len__(self) -> int:
        return sum(end - start + 1 for start, end in self.covered_ranges())


class FileSeekLog:
    """Minimal port of the parser's ``FileSeekLog``.

    the parser uses it for diagnostics, but two of its methods have a **load-bearing**
    side effect on the file cursor:

    * ``LogToEndAndAlignToNextStart(reader, fileOffsetEnd, byteAlignmentCount, text)``
      seeks the reader to the next ``byteAlignmentCount`` boundary after
      ``fileOffsetEnd + 1``.  In the frame-anim path this is what makes the
      bone-flag array and the constants array land on their real offsets.
    * ``LogUnreadBytes(reader)`` walks every log entry and marks the gaps; the port
      keeps it as a no-op that only computes the gaps.

    The log itself is kept because the MDL parser may want it, but nothing in the
    animation decode depends on the recorded text.
    """

    __slots__ = ("FileSize", "theOffsets")

    def __init__(self) -> None:
        self.FileSize = 0
        self.theOffsets = _OffsetRanges()

    def add(self, fileOffsetStart: int, fileOffsetEnd: int, description: str) -> None:
        """Implement ``FileSeekLog.Add`` -- record one described byte range."""
        if fileOffsetEnd < fileOffsetStart:
            return
        self.theOffsets.add(fileOffsetStart, fileOffsetEnd, description)

    def log_to_end_and_align_to_next_start(
        self,
        inputFileReader: BinaryReader,
        fileOffsetEnd: int,
        byteAlignmentCount: int,
        description: str,
    ) -> None:
        """Implement ``FileSeekLog.LogToEndAndAlignToNextStart``."""
        fileOffsetStart2 = fileOffsetEnd + 1
        fileOffsetEnd2 = align_long(fileOffsetStart2, byteAlignmentCount) - 1
        inputFileReader.seek(fileOffsetEnd2 + 1, SEEK_BEGIN)
        self.add(fileOffsetStart2, fileOffsetEnd2, description)

    def log_unread_bytes(self, inputFileReader: BinaryReader) -> None:
        """Implement ``FileSeekLog.LogUnreadBytes`` -- mark the gaps between entries."""
        size = self.FileSize or inputFileReader.length
        cursor = 0
        for start, end in self.theOffsets.covered_ranges():
            if start > cursor:
                self.add(cursor, min(start, size) - 1, "unread")
            cursor = max(cursor, end + 1)
            if cursor >= size:
                return
        if cursor < size:
            self.add(cursor, size - 1, "unread")


# ===========================================================================
# ExtractAnimValue -- the RLE frame -> value lookup
# ===========================================================================


def extract_anim_value(
    frame_index: int, anim_values: list[SourceMdlAnimationValue], scale: float
) -> float:
    """Implement ``SourceMdFile49.ExtractAnimValue`` / ``SourceSmdFile49.ExtractAnimValue``.

    This is the engine's ``ExtractAnimValue`` (quoted verbatim in the VB source): walk
    the RLE spans while ``total <= k``, then take either ``panimvalue[k+1]`` (when the
    span still has explicit values left) or the last explicit value, multiplied by
    ``scale``.

    Returns ``0.0`` on any failure -- the VB ``v1`` is not initialised before the
    ``Try``, so an exception yields ``0.0`` for a ``Double``.
    """
    try:
        k = frame_index
        anim_value_index = 0
        while anim_values[anim_value_index].total <= k:
            k -= anim_values[anim_value_index].total
            anim_value_index += anim_values[anim_value_index].valid + 1
            if (
                anim_value_index >= len(anim_values)
                or anim_values[anim_value_index].total == 0
            ):
                # NOTE: bad if it reaches here; a newer anim-data format may be in use.
                return 0.0

        if anim_values[anim_value_index].valid > k:
            return anim_values[anim_value_index + k + 1].value * scale
        return anim_values[anim_value_index + anim_values[anim_value_index].valid].value * scale
    except MemoryError:
        raise
    except Exception:
        return 0.0


# ===========================================================================
# The shared reader base
# ===========================================================================


class _AnimationReaderBase:
    """The animation readers shared by the MDL parser and the ``.ani`` drivers.

    In the VB source this code lives on ``SourceMdlFile49`` and ``SourceAniFile49``
    simply inherits it.  The Python port keeps that shape: this class carries the
    readers, :class:`SourceMdlFile49AnimationMixin` is the public name that the MDL
    parser subclasses, and :class:`SourceAniFile49` / :class:`SourceAniFile44` also
    derive from it.

    Subclasses must provide:

    * ``self.theInputFileReader`` -- a :class:`~loka.core.reader.BinaryReader`;
    * ``self.theMdlFileData`` -- the :class:`SourceMdlFileData49` being read.
    """

    theInputFileReader: BinaryReader
    theMdlFileData: SourceMdlFileData49

    # -- small helpers -------------------------------------------------------

    @property
    def _seek_log(self) -> FileSeekLog:
        log = getattr(self.theMdlFileData, "theFileSeekLog", None)
        if log is None:
            log = FileSeekLog()
            try:
                log.FileSize = self.theInputFileReader.length
            except MemoryError:
                raise
            except Exception:
                pass
            try:
                self.theMdlFileData.theFileSeekLog = log  # type: ignore[attr-defined]
            except MemoryError:
                raise
            except Exception:
                pass
        return log

    def _bone_count(self) -> int:
        bones = getattr(self.theMdlFileData, "theBones", None)
        return 0 if not bones else len(bones)

    # ==================================================================
    # Header
    # ==================================================================

    def read_mdl_header00(self, log_description: str) -> None:
        """Implement ``SourceMdlFile49.ReadMdlHeader00``.

        Used for the ``.ani`` header as well -- which is why the ``IDAG`` magic is
        never rejected: nothing here validates ``id``.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        file_offset_start = reader.position

        data.id = reader.read_chars(4)
        data.theID = data.id
        data.version = reader.read_int32()

        data.checksum = reader.read_int32()

        data.name = reader.read_chars(64)
        data.theModelName = data.name.strip("\x00")

        data.fileSize = reader.read_int32()
        data.theActualFileSize = reader.length

        file_offset_end = reader.position - 1
        if log_description:
            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                log_description + " (MDL version: " + str(data.version) + ")",
            )

    def read_mdl_header01(self, log_description: str) -> None:
        """Implement ``SourceMdlFile49.ReadMdlHeader01`` -- the bulk of ``studiohdr_t``.

        This is what the ``.ani`` driver runs over the ``.ani`` header.  Every
        offset/count field read here is zero in a real ``.ani`` file, which is the
        reason ``theAnimBlocks`` stays ``None`` for the ANI's own file data.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        file_offset_start = reader.position

        for vector in (
            data.eyePosition,
            data.illuminationPosition,
            data.hullMinPosition,
            data.hullMaxPosition,
            data.viewBoundingBoxMinPosition,
            data.viewBoundingBoxMaxPosition,
        ):
            vector.x = reader.read_single()
            vector.y = reader.read_single()
            vector.z = reader.read_single()

        data.flags = reader.read_int32()

        data.boneCount = reader.read_int32()
        data.boneOffset = reader.read_int32()

        data.boneControllerCount = reader.read_int32()
        data.boneControllerOffset = reader.read_int32()

        data.hitboxSetCount = reader.read_int32()
        data.hitboxSetOffset = reader.read_int32()
        if data.hitboxSetCount > 64:
            data.hitboxSetCount = 64

        data.localAnimationCount = reader.read_int32()
        data.localAnimationOffset = reader.read_int32()

        data.localSequenceCount = reader.read_int32()
        data.localSequenceOffset = reader.read_int32()

        data.activityListVersion = reader.read_int32()
        data.eventsIndexed = reader.read_int32()

        data.textureCount = reader.read_int32()
        data.textureOffset = reader.read_int32()

        data.texturePathCount = reader.read_int32()
        data.texturePathOffset = reader.read_int32()

        data.skinReferenceCount = reader.read_int32()
        data.skinFamilyCount = reader.read_int32()
        data.skinFamilyOffset = reader.read_int32()

        data.bodyPartCount = reader.read_int32()
        data.bodyPartOffset = reader.read_int32()

        data.localAttachmentCount = reader.read_int32()
        data.localAttachmentOffset = reader.read_int32()

        data.localNodeCount = reader.read_int32()
        data.localNodeOffset = reader.read_int32()
        data.localNodeNameOffset = reader.read_int32()
        if data.localNodeCount > 100:
            data.localNodeCount = 100

        data.flexDescCount = reader.read_int32()
        data.flexDescOffset = reader.read_int32()

        data.flexControllerCount = reader.read_int32()
        data.flexControllerOffset = reader.read_int32()

        data.flexRuleCount = reader.read_int32()
        data.flexRuleOffset = reader.read_int32()

        data.ikChainCount = reader.read_int32()
        data.ikChainOffset = reader.read_int32()

        data.mouthCount = reader.read_int32()
        data.mouthOffset = reader.read_int32()

        data.localPoseParamaterCount = reader.read_int32()
        data.localPoseParameterOffset = reader.read_int32()

        data.surfacePropOffset = reader.read_int32()
        if data.surfacePropOffset > 0:
            input_file_stream_position = reader.position
            reader.seek(data.surfacePropOffset, SEEK_BEGIN)
            file_offset_start2 = reader.position
            data.theSurfacePropName = reader.read_null_terminated_string()
            file_offset_end2 = reader.position - 1
            self._seek_log.add(
                file_offset_start2,
                file_offset_end2,
                "theSurfacePropName = " + data.theSurfacePropName,
            )
            reader.seek(input_file_stream_position, SEEK_BEGIN)
        else:
            data.theSurfacePropName = ""

        data.keyValueOffset = reader.read_int32()
        data.keyValueSize = reader.read_int32()

        data.localIkAutoPlayLockCount = reader.read_int32()
        data.localIkAutoPlayLockOffset = reader.read_int32()

        data.mass = reader.read_single()
        data.contents = reader.read_int32()

        data.includeModelCount = reader.read_int32()
        data.includeModelOffset = reader.read_int32()

        data.virtualModelP = reader.read_int32()

        data.animBlockNameOffset = reader.read_int32()
        data.animBlockCount = reader.read_int32()
        data.animBlockOffset = reader.read_int32()
        data.animBlockModelP = reader.read_int32()
        if data.animBlockCount > 0:
            if data.animBlockNameOffset > 0:
                input_file_stream_position = reader.position
                reader.seek(data.animBlockNameOffset, SEEK_BEGIN)
                file_offset_start2 = reader.position

                data.theAnimBlockRelativePathFileName = reader.read_null_terminated_string()

                file_offset_end2 = reader.position - 1
                self._seek_log.add(
                    file_offset_start2,
                    file_offset_end2,
                    "theAnimBlockRelativePathFileName = "
                    + data.theAnimBlockRelativePathFileName,
                )
                reader.seek(input_file_stream_position, SEEK_BEGIN)
            if data.animBlockOffset > 0:
                input_file_stream_position = reader.position
                reader.seek(data.animBlockOffset, SEEK_BEGIN)
                file_offset_start2 = reader.position

                from .mdl_data import SourceMdlAnimBlock

                data.theAnimBlocks = []
                for _ in range(data.animBlockCount):
                    an_anim_block = SourceMdlAnimBlock()
                    an_anim_block.dataStart = reader.read_int32()
                    an_anim_block.dataEnd = reader.read_int32()
                    data.theAnimBlocks.append(an_anim_block)

                file_offset_end2 = reader.position - 1
                self._seek_log.add(
                    file_offset_start2,
                    file_offset_end2,
                    "theAnimBlocks " + str(len(data.theAnimBlocks)),
                )
                reader.seek(input_file_stream_position, SEEK_BEGIN)

        data.boneTableByNameOffset = reader.read_int32()

        data.vertexBaseP = reader.read_int32()
        data.indexBaseP = reader.read_int32()

        data.directionalLightDot = reader.read_byte()

        data.rootLod = reader.read_byte()

        data.allowedRootLodCount_VERSION48 = reader.read_byte()

        data.unused = reader.read_byte()

        data.zeroframecacheindex_VERSION44to47 = reader.read_int32()

        data.flexControllerUiCount = reader.read_int32()
        data.flexControllerUiOffset = reader.read_int32()

        data.vertAnimFixedPointScale = reader.read_single()
        data.surfacePropLookup = reader.read_int32()

        data.studioHeader2Offset = reader.read_int32()

        data.unused2 = reader.read_int32()

        file_offset_end = reader.position - 1
        self._seek_log.add(file_offset_start, file_offset_end, log_description)

        if data.bodyPartCount == 0 and data.localSequenceCount > 0:
            data.theMdlFileOnlyHasAnimations = True

    # ==================================================================
    # Animdesc / section / MDL-resident block reading (MDL pass)
    # ==================================================================

    def read_local_animation_descs(self) -> None:
        """Implement ``SourceMdlFile49.ReadLocalAnimationDescs``.

        Reads ``localAnimationCount`` 100-byte ``mstudioanimdesc_t`` records from
        ``localAnimationOffset`` and, per record, performs three side reads that seek
        away and are undone by the caller's final seek: the name, the
        zero-frame/save-frame span extent, and the movement records.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(data.localAnimationOffset, SEEK_BEGIN)
        file_offset_start = reader.position

        data.theAnimationDescs = []
        for _ in range(data.localAnimationCount):
            anim_input_file_stream_position = reader.position
            an_animation_desc = SourceMdlAnimationDesc49()

            an_animation_desc.theOffsetStart = reader.position

            an_animation_desc.baseHeaderOffset = reader.read_int32()
            an_animation_desc.nameOffset = reader.read_int32()
            an_animation_desc.fps = reader.read_single()
            an_animation_desc.flags = reader.read_int32()
            an_animation_desc.frameCount = reader.read_int32()
            an_animation_desc.movementCount = reader.read_int32()
            an_animation_desc.movementOffset = reader.read_int32()

            an_animation_desc.ikRuleZeroFrameOffset_VERSION49 = reader.read_int32()

            # NOTE: the VB loops over `anAnimationDesc.unused1.Length`, which is 5 for
            # v49 (the first entry is the v48 `unused1` slot).
            for x in range(_ANIMDESC_UNUSED1_LENGTH):
                if x < len(an_animation_desc.unused1):
                    an_animation_desc.unused1[x] = reader.read_int32()
                else:
                    reader.read_int32()

            an_animation_desc.animBlock = reader.read_int32()
            an_animation_desc.animOffset = reader.read_int32()
            an_animation_desc.ikRuleCount = reader.read_int32()
            an_animation_desc.ikRuleOffset = reader.read_int32()
            an_animation_desc.animblockIkRuleOffset = reader.read_int32()
            an_animation_desc.localHierarchyCount = reader.read_int32()
            an_animation_desc.localHierarchyOffset = reader.read_int32()
            an_animation_desc.sectionOffset = reader.read_int32()
            an_animation_desc.sectionFrameCount = reader.read_int32()

            an_animation_desc.spanFrameCount = reader.read_int16()
            an_animation_desc.spanCount = reader.read_int16()
            an_animation_desc.spanOffset = reader.read_int32()
            an_animation_desc.spanStallTime = reader.read_single()

            data.theAnimationDescs.append(an_animation_desc)

            input_file_stream_position = reader.position

            self.read_animation_desc_name(anim_input_file_stream_position, an_animation_desc)
            self.read_animation_desc_span_data(
                anim_input_file_stream_position, an_animation_desc
            )
            self.read_mdl_movements(anim_input_file_stream_position, an_animation_desc)

            reader.seek(input_file_stream_position, SEEK_BEGIN)

        file_offset_end = reader.position - 1
        self._seek_log.add(
            file_offset_start,
            file_offset_end,
            "theMdlFileData.theAnimationDescs " + str(len(data.theAnimationDescs)),
        )

    def read_animation_sections(self) -> None:
        """Implement ``SourceMdlFile49.ReadAnimationSections``.

        Always creates one empty section container up front; when
        ``sectionOffset != 0 and sectionFrameCount > 0`` it then grows the containers
        to ``frameCount // sectionFrameCount + 2`` entries and reads that many
        ``mstudioanimsections_t`` records from ``theOffsetStart + sectionOffset``.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        if data.theAnimationDescs is None:
            return

        for an_animation_desc in data.theAnimationDescs:
            an_animation_desc.theSectionsOfFrameAnim = [SourceAniFrameAnim49()]

            an_animation_desc.theSectionsOfAnimations = [[]]

            if an_animation_desc.sectionOffset != 0 and an_animation_desc.sectionFrameCount > 0:
                # TODO: shouldn't this be set to the largest sectionFrameCount?
                data.theSectionFrameCount = an_animation_desc.sectionFrameCount
                if data.theSectionFrameMinFrameCount >= an_animation_desc.frameCount:
                    data.theSectionFrameMinFrameCount = an_animation_desc.frameCount - 1

                # FROM simplify.cpp:
                #   panim->numsections = (int)(panim->numframes / panim->sectionframes) + 2;
                # NOTE: it is unclear why "+ 2" is used in studiomdl.
                section_count = (
                    int(an_animation_desc.frameCount / an_animation_desc.sectionFrameCount) + 2
                )

                # NOTE: the first sectionOfAnimation was created above.
                for _ in range(1, section_count):
                    an_animation_desc.theSectionsOfFrameAnim.append(SourceAniFrameAnim49())
                    an_animation_desc.theSectionsOfAnimations.append([])

                offset = an_animation_desc.theOffsetStart + an_animation_desc.sectionOffset
                if offset != reader.position:
                    reader.seek(offset, SEEK_BEGIN)

                an_animation_desc.theSections = []
                for _ in range(section_count):
                    self.read_mdl_animation_section(
                        reader.position, an_animation_desc, self._seek_log
                    )

    def read_animation_mdl_blocks(self) -> None:
        """Implement ``SourceMdlFile49.ReadAnimationMdlBlocks``.

        Reads every **MDL-resident** byte range: sections whose ``animBlock == 0``
        (plus the unsectioned ``animdesc.animBlock == 0`` case), then the IK rules and
        local hierarchies when the file version is 44 or the animdesc is MDL-resident.
        Sections with ``animBlock > 0`` are left for the ``.ani`` pass.
        """
        data = self.theMdlFileData

        if data.theAnimationDescs is None:
            return

        for an_animation_desc in data.theAnimationDescs:
            try:
                anim_input_file_stream_position = an_animation_desc.theOffsetStart
                section_index = 0

                # NOTE: need to check section.animBlock no matter what
                # anAnimationDesc.animBlock is.
                if (
                    an_animation_desc.theSections is not None
                    and len(an_animation_desc.theSections) > 0
                ):
                    section_count = len(an_animation_desc.theSections)
                    for section_index in range(section_count):
                        section = an_animation_desc.theSections[section_index]
                        if section.animBlock == 0:
                            # NOTE: weird, but it fits a few oddball models (such as
                            # L4D2 "left4dead2\ghostanim.mdl") without breaking the
                            # normal ones.
                            adjusted_anim_offset = section.animOffset + (
                                an_animation_desc.animOffset
                                - an_animation_desc.theSections[0].animOffset
                            )

                            if section_index < section_count - 2:
                                section_frame_count = an_animation_desc.sectionFrameCount
                            else:
                                # NOTE: due to the weird sectionCount calculation in
                                # studiomdl this line is hit twice, i.e. there are two
                                # "last" sections; the final one is likely bogus data.
                                section_frame_count = an_animation_desc.frameCount - (
                                    (section_count - 2) * an_animation_desc.sectionFrameCount
                                )

                            self.read_animation_frames(
                                anim_input_file_stream_position + adjusted_anim_offset,
                                an_animation_desc,
                                section_frame_count,
                                section_index,
                                (section_index >= section_count - 2)
                                or (
                                    an_animation_desc.frameCount
                                    == (section_index + 1) * an_animation_desc.sectionFrameCount
                                ),
                            )
                elif an_animation_desc.animBlock == 0:
                    section_index = 0
                    self.read_animation_frames(
                        anim_input_file_stream_position + an_animation_desc.animOffset,
                        an_animation_desc,
                        an_animation_desc.frameCount,
                        section_index,
                        True,
                    )

                # NOTE: these seem to always be stored in the MDL file for MDL44.
                if data.version == 44 or an_animation_desc.animBlock == 0:
                    self.read_mdl_ik_rules(anim_input_file_stream_position, an_animation_desc)
                    self.read_local_hierarchies(
                        anim_input_file_stream_position, an_animation_desc
                    )
            except MemoryError:
                raise
            except Exception:
                # the parser: Catch ex As Exception / Dim debug As Integer = 4242
                # A single bad animation must never abort the whole model.
                continue

    def read_animation_desc_name(
        self, anim_input_file_stream_position: int, an_animation_desc: SourceMdlAnimationDesc49
    ) -> None:
        """Implement ``SourceMdlFile49.ReadAnimationDescName``."""
        reader = self.theInputFileReader

        if an_animation_desc.nameOffset != 0:
            reader.seek(anim_input_file_stream_position + an_animation_desc.nameOffset, SEEK_BEGIN)
            file_offset_start = reader.position

            an_animation_desc.theName = reader.read_null_terminated_string()

            # NOTE: found in Garry's Mod garrysmod_dir.vpk
            # "\models\m_anm.mdl": "a_../combine_soldier_xsi/Hold_AR2_base.smd"
            if an_animation_desc.theName.startswith("a_../") or an_animation_desc.theName.startswith(
                "a_..\\"
            ):
                an_animation_desc.theName = an_animation_desc.theName[5:]
                directory = _get_path(an_animation_desc.theName)
                file_name = _get_file_name(an_animation_desc.theName)
                an_animation_desc.theName = _path_combine(directory, "a_" + file_name)

            file_offset_end = reader.position - 1
            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                "anAnimationDesc.theName = " + an_animation_desc.theName,
            )
        else:
            an_animation_desc.theName = ""

    def read_animation_desc_span_data(
        self, anim_input_file_stream_position: int, an_animation_desc: SourceMdlAnimationDesc49
    ) -> int:
        """Implement ``SourceMdlFile49.ReadAnimationDescSpanData``.

        The zero-frame/save-frame payload is never decoded; only its byte extent is
        computed (from the bones' ``BONE_HAS_SAVEFRAME_*`` flags and ``spanCount``)
        and ``theAnimBlockSizeNoStallOptionIsUsed`` is raised.
        """
        file_offset_end = 0

        if (
            an_animation_desc.spanFrameCount != 0
            or an_animation_desc.spanCount != 0
            or an_animation_desc.spanOffset != 0
            or an_animation_desc.spanStallTime != 0
        ):
            file_offset_start = anim_input_file_stream_position + an_animation_desc.spanOffset
            file_offset_end = file_offset_start - 1

            bones = getattr(self.theMdlFileData, "theBones", None) or []
            for a_bone in bones:
                if (a_bone.flags & 0x200000) > 0:  # BONE_HAS_SAVEFRAME_POS
                    file_offset_end += an_animation_desc.spanCount * 6  # Vector48
                if (a_bone.flags & 0x400000) > 0:  # BONE_HAS_SAVEFRAME_ROT
                    file_offset_end += an_animation_desc.spanCount * 8  # Quaternion64
                if (a_bone.flags & 0x800000) > 0:  # BONE_HAS_SAVEFRAME_ROT32
                    file_offset_end += an_animation_desc.spanCount * 4  # Quaternion32

            self.theMdlFileData.theAnimBlockSizeNoStallOptionIsUsed = True

            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                "anAnimationDesc.spanOffset (zeroframes/saveframes) ["
                + an_animation_desc.theName
                + "] [spanFrameCount = "
                + str(an_animation_desc.spanFrameCount)
                + "] [spanCount = "
                + str(an_animation_desc.spanCount)
                + "]",
            )

        return file_offset_end

    def read_animation_frames(
        self,
        anim_input_file_stream_position: int,
        an_animation_desc: SourceMdlAnimationDesc49,
        section_frame_count: int,
        section_index: int,
        last_section_is_being_read: bool,
    ) -> None:
        """Implement ``SourceMdlFile49.ReadAnimationFrames`` -- the format dispatcher."""
        if (an_animation_desc.flags & STUDIO_FRAMEANIM) != 0:
            self.read_animation_frame_by_bone(
                anim_input_file_stream_position,
                an_animation_desc,
                section_frame_count,
                section_index,
                last_section_is_being_read,
            )
        else:
            self.read_mdl_animation(
                anim_input_file_stream_position,
                an_animation_desc,
                section_frame_count,
                section_index,
                last_section_is_being_read,
            )

    # ==================================================================
    # RLE path
    # ==================================================================

    def read_mdl_animation(
        self,
        anim_input_file_stream_position: int,
        an_animation_desc: SourceMdlAnimationDesc49,
        section_frame_count: int,
        section_index: int,
        last_section_is_being_read: bool,
    ) -> None:
        """Implement ``SourceMdlFile49.ReadMdlAnimation`` -- the RLE ``mstudioanim_t`` walk.

        Termination rules (all reproduced literally):

        1. ``boneIndex == 255`` -> consume one extra byte + one extra ``int16``, stop,
           and **skip** the trailing "unused empty data structure" read.
        2. ``boneIndex >= boneCount`` (and not 255; the ``ghostanim.mdl`` case) -> stop
           *without* consuming flags/nextoffset, but still do the trailing read.
        3. ``nextSourceMdlAnimationOffset == 0`` -> stop.
        4. Otherwise seek **absolutely** to ``recordStart + nextSourceMdlAnimationOffset``
           (signed 16-bit).  Behind the cursor -> stop; ahead -> continue (data skipped).
        5. If the loop ran to completion, read 4 trailing bytes (byte, byte, int16).
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(anim_input_file_stream_position, SEEK_BEGIN)

        a_section_of_animation: list[SourceMdlAnimation] = (
            an_animation_desc.theSectionsOfAnimations[section_index]
        )

        bone_count = 1 if data.theBones is None else len(data.theBones)

        bone_index = 0
        for _ in range(bone_count):
            animation_input_file_stream_position = reader.position

            bone_index = reader.read_byte()
            if bone_index == ANIM_BONE_INDEX_TERMINATOR:
                reader.read_byte()
                reader.read_int16()

                file_offset_end = reader.position - 1
                self._seek_log.add(
                    animation_input_file_stream_position,
                    file_offset_end,
                    "anAnimationDesc.anAnimation ["
                    + an_animation_desc.theName
                    + "] (boneIndex = 255)",
                )
                break

            # DEBUG: L4D2 "left4dead2\ghostanim.mdl" reaches here.
            if bone_index >= bone_count:
                break

            an_animation = SourceMdlAnimation()
            a_section_of_animation.append(an_animation)

            an_animation.boneIndex = bone_index
            an_animation.flags = reader.read_byte()
            an_animation.nextSourceMdlAnimationOffset = reader.read_int16()

            if (an_animation.flags & STUDIO_ANIM_RAWROT2) > 0:
                an_animation.theRot64bits = SourceQuaternion64bits()
                an_animation.theRot64bits.theBytes = reader.read_bytes(8)

            if (an_animation.flags & STUDIO_ANIM_RAWROT) > 0:
                an_animation.theRot48bits = SourceQuaternion48bits()
                an_animation.theRot48bits.theXInput = reader.read_uint16()
                an_animation.theRot48bits.theYInput = reader.read_uint16()
                an_animation.theRot48bits.theZWInput = reader.read_uint16()

            if (an_animation.flags & STUDIO_ANIM_RAWPOS) > 0:
                an_animation.thePos = SourceVector48bits()
                an_animation.thePos.theXInput.the16BitValue = reader.read_uint16()
                an_animation.thePos.theYInput.the16BitValue = reader.read_uint16()
                an_animation.thePos.theZInput.the16BitValue = reader.read_uint16()

            # `animValuePointerInputFileStreamPosition` is assigned but unused in the
            # VB (the per-axis positions below are what the RLE reads need).
            _anim_value_pointer_input_file_stream_position = reader.position

            # First, read both sets of offsets.
            rot_value_pointer_input_file_stream_position = 0
            if (an_animation.flags & STUDIO_ANIM_ANIMROT) > 0:
                rot_value_pointer_input_file_stream_position = reader.position
                an_animation.theRotV = SourceMdlAnimationValuePointer()
                an_animation.theRotV.animXValueOffset = reader.read_int16()
                an_animation.theRotV.animYValueOffset = reader.read_int16()
                an_animation.theRotV.animZValueOffset = reader.read_int16()

            pos_value_pointer_input_file_stream_position = 0
            if (an_animation.flags & STUDIO_ANIM_ANIMPOS) > 0:
                pos_value_pointer_input_file_stream_position = reader.position
                an_animation.thePosV = SourceMdlAnimationValuePointer()
                an_animation.thePosV.animXValueOffset = reader.read_int16()
                an_animation.thePosV.animYValueOffset = reader.read_int16()
                an_animation.thePosV.animZValueOffset = reader.read_int16()

            self._seek_log.add(
                animation_input_file_stream_position,
                reader.position - 1,
                "anAnimationDesc.anAnimation  ["
                + an_animation_desc.theName
                + "] (frameCount = "
                + str(an_animation_desc.frameCount)
                + "; sectionFrameCount = "
                + str(section_frame_count)
                + ")",
            )

            # Second, read the anim values using the offsets.
            if (an_animation.flags & STUDIO_ANIM_ANIMROT) > 0:
                rot_v = an_animation.theRotV
                assert rot_v is not None
                if rot_v.animXValueOffset > 0:
                    self.read_mdl_anim_values(
                        rot_value_pointer_input_file_stream_position + rot_v.animXValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        rot_v.theAnimXValues,
                        "anAnimation.theRotV.theAnimXValues",
                    )
                if rot_v.animYValueOffset > 0:
                    self.read_mdl_anim_values(
                        rot_value_pointer_input_file_stream_position + rot_v.animYValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        rot_v.theAnimYValues,
                        "anAnimation.theRotV.theAnimYValues",
                    )
                if rot_v.animZValueOffset > 0:
                    self.read_mdl_anim_values(
                        rot_value_pointer_input_file_stream_position + rot_v.animZValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        rot_v.theAnimZValues,
                        "anAnimation.theRotV.theAnimZValues",
                    )

            if (an_animation.flags & STUDIO_ANIM_ANIMPOS) > 0:
                pos_v = an_animation.thePosV
                assert pos_v is not None
                if pos_v.animXValueOffset > 0:
                    self.read_mdl_anim_values(
                        pos_value_pointer_input_file_stream_position + pos_v.animXValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        pos_v.theAnimXValues,
                        "anAnimation.thePosV.theAnimXValues",
                    )
                if pos_v.animYValueOffset > 0:
                    self.read_mdl_anim_values(
                        pos_value_pointer_input_file_stream_position + pos_v.animYValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        pos_v.theAnimYValues,
                        "anAnimation.thePosV.theAnimYValues",
                    )
                if pos_v.animZValueOffset > 0:
                    self.read_mdl_anim_values(
                        pos_value_pointer_input_file_stream_position + pos_v.animZValueOffset,
                        section_frame_count,
                        last_section_is_being_read,
                        pos_v.theAnimZValues,
                        "anAnimation.thePosV.theAnimZValues",
                    )

            # NOTE: if the offset is 0 then there are no more bone animation
            # structures, so end the loop.
            if an_animation.nextSourceMdlAnimationOffset == 0:
                break

            # Skip to the next anim, just in case not all data is being read in.
            next_animation_input_file_stream_position = (
                animation_input_file_stream_position + an_animation.nextSourceMdlAnimationOffset
            )
            # NOTE: the parser treats nextSourceMdlAnimationOffset as a *signed* Short
            # (`ReadInt16`); the commented-out CType(..., UShort) variant is not used.
            if next_animation_input_file_stream_position < reader.position:
                # PROBLEM! Should not be going backwards in file.
                break
            # (advancing past the cursor is tolerated: some data is simply skipped)

            reader.seek(next_animation_input_file_stream_position, SEEK_BEGIN)

        if bone_index != ANIM_BONE_INDEX_TERMINATOR:
            # NOTE: there is always an unused empty data structure at the end of the list.
            file_offset_start = reader.position
            reader.read_byte()
            reader.read_byte()
            reader.read_int16()

            file_offset_end = reader.position - 1
            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                "anAnimationDesc.anAnimation ["
                + an_animation_desc.theName
                + "] (unused empty data structure at the end of the list)",
            )

    def read_mdl_anim_values(
        self,
        anim_values_input_file_stream_position: int,
        frame_count: int,
        last_section_is_being_read: bool,
        the_anim_values: list[SourceMdlAnimationValue],
        debug_description: str,
    ) -> None:
        """Implement ``SourceMdlFile49.ReadMdlAnimValues`` -- the RLE span reader.

        Each span is one header element (``total`` = run length, ``valid`` = number of
        explicit values) followed by exactly ``valid`` elements.  The loop ends when
        ``frame_count`` frames were consumed or a header has ``total == 0``.

        **Trailing 4-byte skip:** after the loop, when this was *not* the last section
        and ``accumulatedTotal == frameCount`` exactly, two more ``int16`` values are
        read and discarded.  That is a real cursor advance and is reproduced.
        """
        reader = self.theInputFileReader

        reader.seek(anim_values_input_file_stream_position, SEEK_BEGIN)
        file_offset_start = reader.position

        frame_count_remaining_to_be_checked = frame_count
        accumulated_total = 0
        while frame_count_remaining_to_be_checked > 0:
            anim_value = SourceMdlAnimationValue(reader.read_int16())
            current_total = anim_value.total
            accumulated_total += current_total
            if current_total == 0:
                # NOTE: bad if this is reached.
                break
            frame_count_remaining_to_be_checked -= current_total
            the_anim_values.append(anim_value)

            valid_count = anim_value.valid
            for _ in range(1, valid_count + 1):
                the_anim_values.append(SourceMdlAnimationValue(reader.read_int16()))

        if not last_section_is_being_read and accumulated_total == frame_count:
            reader.read_int16()
            reader.read_int16()

        file_offset_end = reader.position - 1
        self._seek_log.add(
            file_offset_start,
            file_offset_end,
            debug_description + " (accumulatedTotal = " + str(accumulated_total) + ")",
        )

    # ==================================================================
    # Frame-anim path
    # ==================================================================

    def read_animation_frame_by_bone(
        self,
        anim_input_file_stream_position: int,
        an_animation_desc: SourceMdlAnimationDesc49,
        section_frame_count: int,
        section_index: int,
        last_section_is_being_read: bool,
    ) -> None:
        """Implement ``SourceMdlFile49.ReadAnimationFrameByBone``.

        Layout of one ``mstudio_frame_anim_t`` section at ``base``:

        ===============  =====  ===================================
        field            bytes  read when
        ===============  =====  ===================================
        constantsOffset  4      always
        frameOffset      4      always
        frameLength      4      always
        unused[3]        12     always
        theBoneFlags     bones  always, then align the cursor to 4
        constants        6/12   at ``base + constantsOffset``, per bone flags
        frames           6/12   at ``base + frameOffset + i * frameLength``
        ===============  =====  ===================================

        The constant sub-order is ROT2, POS2, RAWROT, RAWPOS; the frame sub-order is
        ROT2, ANIMROT, ANIMPOS, FULLANIMPOS.  They are different -- do not conflate.

        ``adjustedFrameCount`` is ``sectionFrameCount`` for the last section and
        ``sectionFrameCount + 1`` otherwise; the extra frame of a non-last section is
        read but its list is **not** appended, so ``theBoneFrameDataInfos.Count`` is
        always ``sectionFrameCount``.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(anim_input_file_stream_position, SEEK_BEGIN)

        a_section_of_animation: SourceAniFrameAnim49 = (
            an_animation_desc.theSectionsOfFrameAnim[section_index]
        )

        bone_count = len(data.theBones) if data.theBones is not None else 0

        try:
            anim_frame_input_file_stream_position = reader.position
            file_offset_start = reader.position

            a_section_of_animation.constantsOffset = reader.read_int32()
            a_section_of_animation.frameOffset = reader.read_int32()
            a_section_of_animation.frameLength = reader.read_int32()
            for x in range(len(a_section_of_animation.unused)):
                a_section_of_animation.unused[x] = reader.read_int32()

            file_offset_end = reader.position - 1
            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                "anAnimationDesc.theAniFrameAnim ["
                + an_animation_desc.theName
                + "] (frameCount = "
                + str(an_animation_desc.frameCount)
                + "; sectionFrameCount = "
                + str(section_frame_count)
                + ")",
            )

            file_offset_start = reader.position

            a_section_of_animation.theBoneFlags = []
            for _ in range(bone_count):
                bone_flag = reader.read_byte()
                a_section_of_animation.theBoneFlags.append(bone_flag)

            file_offset_end = reader.position - 1
            self._seek_log.add(
                file_offset_start,
                file_offset_end,
                "anAniFrameAnim.theBoneFlags "
                + str(len(a_section_of_animation.theBoneFlags)),
            )
            # NOTE: load-bearing alignment -- flags occupy boneCount bytes and the
            # cursor must land on `base + constantsOffset` (a 4-byte boundary).
            self._seek_log.log_to_end_and_align_to_next_start(
                reader, file_offset_end, 4, "anAniFrameAnim.theBoneFlags alignment"
            )

            if a_section_of_animation.constantsOffset != 0:
                reader.seek(
                    anim_frame_input_file_stream_position
                    + a_section_of_animation.constantsOffset,
                    SEEK_BEGIN,
                )
                file_offset_start = reader.position

                a_section_of_animation.theBoneConstantInfos = []
                for bone_index in range(bone_count):
                    a_bone_constant_info = BoneConstantInfo49()
                    a_section_of_animation.theBoneConstantInfos.append(a_bone_constant_info)

                    bone_flag = a_section_of_animation.theBoneFlags[bone_index]
                    if (bone_flag & STUDIO_FRAME_CONST_ROT2) > 0:
                        a_bone_constant_info.theConstantRotation2 = SourceQuaternion48bitsViaBytes()
                        a_bone_constant_info.theConstantRotation2.theBytes = reader.read_bytes(6)
                    if (bone_flag & STUDIO_FRAME_CONST_POS2) > 0:
                        a_bone_constant_info.theConstantPosition2 = SourceVector()
                        a_bone_constant_info.theConstantPosition2.x = reader.read_single()
                        a_bone_constant_info.theConstantPosition2.y = reader.read_single()
                        a_bone_constant_info.theConstantPosition2.z = reader.read_single()
                    if (bone_flag & STUDIO_FRAME_RAWROT) > 0:
                        a_bone_constant_info.theConstantRawRot = SourceQuaternion48bits()
                        a_bone_constant_info.theConstantRawRot.theXInput = reader.read_uint16()
                        a_bone_constant_info.theConstantRawRot.theYInput = reader.read_uint16()
                        a_bone_constant_info.theConstantRawRot.theZWInput = reader.read_uint16()
                    if (bone_flag & STUDIO_FRAME_RAWPOS) > 0:
                        a_bone_constant_info.theConstantRawPos = SourceVector48bits()
                        a_bone_constant_info.theConstantRawPos.theXInput.the16BitValue = (
                            reader.read_uint16()
                        )
                        a_bone_constant_info.theConstantRawPos.theYInput.the16BitValue = (
                            reader.read_uint16()
                        )
                        a_bone_constant_info.theConstantRawPos.theZInput.the16BitValue = (
                            reader.read_uint16()
                        )

                if reader.position > file_offset_start:
                    file_offset_end = reader.position - 1
                    self._seek_log.add(
                        file_offset_start,
                        file_offset_end,
                        "aSectionOfAnimation.theBoneConstantInfos "
                        + str(len(a_section_of_animation.theBoneConstantInfos)),
                    )
                    self._seek_log.log_to_end_and_align_to_next_start(
                        reader,
                        file_offset_end,
                        4,
                        "aSectionOfAnimation.theBoneConstantInfos alignment",
                    )

            if (
                a_section_of_animation.frameLength > 0
                and a_section_of_animation.frameOffset != 0
            ):
                reader.seek(
                    anim_frame_input_file_stream_position + a_section_of_animation.frameOffset,
                    SEEK_BEGIN,
                )
                file_offset_start = reader.position

                a_section_of_animation.theBoneFrameDataInfos = []

                # NOTE: this adjustment is weird, but it fits all the data seen.
                if last_section_is_being_read:
                    adjusted_frame_count = section_frame_count
                else:
                    adjusted_frame_count = section_frame_count + 1

                for frame_index in range(adjusted_frame_count):
                    a_bone_frame_data_info_list: list[BoneFrameDataInfo49] = []
                    if last_section_is_being_read or (
                        frame_index < (adjusted_frame_count - 1)
                    ):
                        a_section_of_animation.theBoneFrameDataInfos.append(
                            a_bone_frame_data_info_list
                        )

                    bone_frame_data_start_input_file_stream_position = reader.position

                    for bone_index in range(bone_count):
                        a_bone_frame_data_info = BoneFrameDataInfo49()
                        a_bone_frame_data_info_list.append(a_bone_frame_data_info)

                        bone_flag = a_section_of_animation.theBoneFlags[bone_index]

                        if (bone_flag & STUDIO_FRAME_ANIM_ROT2) > 0:
                            a_bone_frame_data_info.theAnimRotationUnknown = (
                                SourceQuaternion48bitsViaBytes()
                            )
                            a_bone_frame_data_info.theAnimRotationUnknown.theBytes = (
                                reader.read_bytes(6)
                            )
                        if (bone_flag & STUDIO_FRAME_ANIMROT) > 0:
                            a_bone_frame_data_info.theAnimRotation = SourceQuaternion48bits()
                            a_bone_frame_data_info.theAnimRotation.theXInput = reader.read_uint16()
                            a_bone_frame_data_info.theAnimRotation.theYInput = reader.read_uint16()
                            a_bone_frame_data_info.theAnimRotation.theZWInput = reader.read_uint16()
                        if (bone_flag & STUDIO_FRAME_ANIMPOS) > 0:
                            a_bone_frame_data_info.theAnimPosition = SourceVector48bits()
                            a_bone_frame_data_info.theAnimPosition.theXInput.the16BitValue = (
                                reader.read_uint16()
                            )
                            a_bone_frame_data_info.theAnimPosition.theYInput.the16BitValue = (
                                reader.read_uint16()
                            )
                            a_bone_frame_data_info.theAnimPosition.theZInput.the16BitValue = (
                                reader.read_uint16()
                            )
                        if (bone_flag & STUDIO_FRAME_FULLANIMPOS) > 0:
                            a_bone_frame_data_info.theFullAnimPosition = SourceVector()
                            a_bone_frame_data_info.theFullAnimPosition.x = reader.read_single()
                            a_bone_frame_data_info.theFullAnimPosition.y = reader.read_single()
                            a_bone_frame_data_info.theFullAnimPosition.z = reader.read_single()

                    # DEBUG (VB): check that each frame really is `frameLength` bytes.
                    # The VB only raises a debug marker here; the port records nothing.

                file_offset_end = reader.position - 1
                text = "aSectionOfAnimation.theBoneFrameDataInfos " + str(
                    len(a_section_of_animation.theBoneFrameDataInfos)
                )
                if not last_section_is_being_read:
                    text += " plus an extra unused aBoneFrameDataInfo"
                self._seek_log.add(file_offset_start, file_offset_end, text)
                self._seek_log.log_to_end_and_align_to_next_start(
                    reader,
                    file_offset_end,
                    4,
                    "aSectionOfAnimation.theBoneFrameDataInfos alignment",
                )
        except MemoryError:
            raise
        except Exception:
            # the parser: Catch ex As Exception / Dim debug As Integer = 4242
            pass

    # ==================================================================
    # IK rules / local hierarchies / movements / sections
    # ==================================================================

    def read_mdl_ik_rules(
        self, anim_input_file_stream_position: int, an_animation_desc: SourceMdlAnimationDesc49
    ) -> int:
        """Implement ``SourceMdlFile49.ReadMdlIkRules``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        if an_animation_desc.ikRuleCount <= 0:
            return 0

        if (
            data.version >= 48
            and an_animation_desc.animBlock > 0
            and an_animation_desc.animblockIkRuleOffset == 0
        ):
            # 'Return 0 -- the VB falls through to the read below instead.
            pass
        else:
            reader.seek(anim_input_file_stream_position + an_animation_desc.ikRuleOffset, SEEK_BEGIN)
        file_offset_start = reader.position

        an_animation_desc.theIkRules = []
        for ik_rule_index in range(an_animation_desc.ikRuleCount):
            ik_rule_input_file_stream_position = reader.position
            an_ik_rule = SourceMdlIkRule()
            an_ik_rule.theOffsetStart = reader.position

            an_ik_rule.index = reader.read_int32()
            an_ik_rule.type = reader.read_int32()
            an_ik_rule.chain = reader.read_int32()
            an_ik_rule.bone = reader.read_int32()

            an_ik_rule.slot = reader.read_int32()
            an_ik_rule.height = reader.read_single()
            an_ik_rule.radius = reader.read_single()
            an_ik_rule.floor = reader.read_single()

            an_ik_rule.pos = SourceVector()
            an_ik_rule.pos.x = reader.read_single()
            an_ik_rule.pos.y = reader.read_single()
            an_ik_rule.pos.z = reader.read_single()
            an_ik_rule.q = SourceQuaternion()
            an_ik_rule.q.x = reader.read_single()
            an_ik_rule.q.y = reader.read_single()
            an_ik_rule.q.z = reader.read_single()
            an_ik_rule.q.w = reader.read_single()

            an_ik_rule.compressedIkErrorOffset = reader.read_int32()
            an_ik_rule.unused2 = reader.read_int32()
            an_ik_rule.ikErrorIndexStart = reader.read_int32()
            an_ik_rule.ikErrorOffset = reader.read_int32()

            an_ik_rule.influenceStart = reader.read_single()
            an_ik_rule.influencePeak = reader.read_single()
            an_ik_rule.influenceTail = reader.read_single()
            an_ik_rule.influenceEnd = reader.read_single()

            an_ik_rule.unused3 = reader.read_single()
            an_ik_rule.contact = reader.read_single()
            an_ik_rule.drop = reader.read_single()
            an_ik_rule.top = reader.read_single()

            an_ik_rule.unused6 = reader.read_int32()
            an_ik_rule.unused7 = reader.read_int32()
            an_ik_rule.unused8 = reader.read_int32()

            an_ik_rule.attachmentNameOffset = reader.read_int32()

            for x in range(len(an_ik_rule.unused)):
                an_ik_rule.unused[x] = reader.read_int32()

            an_ik_rule.theOffsetEnd = reader.position
            an_animation_desc.theIkRules.append(an_ik_rule)

            input_file_stream_position = reader.position

            if an_ik_rule.attachmentNameOffset != 0:
                reader.seek(
                    ik_rule_input_file_stream_position + an_ik_rule.attachmentNameOffset,
                    SEEK_BEGIN,
                )
                file_offset_start2 = reader.position

                an_ik_rule.theAttachmentName = reader.read_null_terminated_string()

                file_offset_end2 = reader.position - 1
                self._seek_log.add(
                    file_offset_start2,
                    file_offset_end2,
                    "anIkRule.theAttachmentName = " + an_ik_rule.theAttachmentName,
                )
            else:
                an_ik_rule.theAttachmentName = ""

            if an_ik_rule.compressedIkErrorOffset != 0:
                self.read_compressed_ik_errors(
                    ik_rule_input_file_stream_position, ik_rule_index, an_animation_desc
                )

            reader.seek(input_file_stream_position, SEEK_BEGIN)

        file_offset_end = reader.position - 1
        description = "anAnimationDesc.theIkRules " + str(len(an_animation_desc.theIkRules))
        if an_animation_desc.animBlock > 0 and an_animation_desc.animblockIkRuleOffset == 0:
            description += "   [animblockIkRuleOffset = 0]"
        self._seek_log.add(file_offset_start, file_offset_end, description)

        return 0

    def read_compressed_ik_errors(
        self,
        ik_rule_input_file_stream_position: int,
        ik_rule_index: int,
        an_animation_desc: SourceMdlAnimationDesc49,
    ) -> int:
        """Implement ``SourceMdlFile49.ReadCompressedIkErrors``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        assert an_animation_desc.theIkRules is not None
        an_ik_rule = an_animation_desc.theIkRules[ik_rule_index]

        reader.seek(
            ik_rule_input_file_stream_position + an_ik_rule.compressedIkErrorOffset, SEEK_BEGIN
        )
        file_offset_start = reader.position
        compressed_ik_error_input_file_stream_position = reader.position

        an_ik_rule.theCompressedIkError = SourceMdlCompressedIkError()
        an_ik_rule.theCompressedIkError.theOffsetStart = reader.position

        # First, read the scale data.
        for k in range(len(an_ik_rule.theCompressedIkError.scale)):
            k_input_file_stream_position = reader.position
            an_ik_rule.theCompressedIkError.scale[k] = reader.read_single()

            file_offset_end = reader.position - 1
            self._seek_log.add(
                k_input_file_stream_position,
                file_offset_end,
                "anIkRule.theCompressedIkError [ikRuleIndex = "
                + str(ik_rule_index)
                + "] [scale = "
                + str(an_ik_rule.theCompressedIkError.scale[k])
                + "]",
            )

        # Second, read the offset data.
        for k in range(len(an_ik_rule.theCompressedIkError.offset)):
            k_input_file_stream_position = reader.position
            an_ik_rule.theCompressedIkError.offset[k] = reader.read_int16()

            file_offset_end = reader.position - 1
            self._seek_log.add(
                k_input_file_stream_position,
                file_offset_end,
                "anIkRule.theCompressedIkError [ikRuleIndex = "
                + str(ik_rule_index)
                + "] [offset = "
                + str(an_ik_rule.theCompressedIkError.offset[k])
                + "]",
            )

        # TODO (VB): third, read the anim values.  This code does not correctly handle
        # versions below 48, and the frame count is not known.
        if data.version >= 48:
            for k in range(len(an_ik_rule.theCompressedIkError.scale)):
                if an_ik_rule.theCompressedIkError.offset[k] > 0:
                    an_ik_rule.theCompressedIkError.theAnimValues[k] = []
                    self.read_mdl_anim_values(
                        compressed_ik_error_input_file_stream_position
                        + an_ik_rule.theCompressedIkError.offset[k],
                        an_animation_desc.frameCount,
                        True,
                        an_ik_rule.theCompressedIkError.theAnimValues[k],  # type: ignore[arg-type]
                        "anIkRule.theCompressedIkError.theAnimValues(" + str(k) + ")",
                    )
                    an_ik_rule.theCompressedIkError.theChannelSpans[k] = (
                        compressed_ik_error_input_file_stream_position + an_ik_rule.theCompressedIkError.offset[k],
                        reader.position,
                    )

        return reader.position - 1

    def read_mdl_animation_section(
        self,
        anim_input_file_stream_position: int,
        an_animation_desc: SourceMdlAnimationDesc49,
        a_file_seek_log: Optional[FileSeekLog] = None,
    ) -> None:
        """Implement ``SourceMdlFile49.ReadMdlAnimationSection`` -- one 8-byte section."""
        reader = self.theInputFileReader

        try:
            file_offset_start = anim_input_file_stream_position

            an_anim_section = SourceMdlAnimationSection()
            an_anim_section.animBlock = reader.read_int32()
            an_anim_section.animOffset = reader.read_int32()
            if an_animation_desc.theSections is None:
                an_animation_desc.theSections = []
            an_animation_desc.theSections.append(an_anim_section)

            file_offset_end = reader.position - 1

            log = a_file_seek_log if a_file_seek_log is not None else self._seek_log
            if self.theMdlFileData.theAnimBlocks is None:
                log.add(
                    file_offset_start,
                    file_offset_end,
                    "anAnimSection ["
                    + an_animation_desc.theName
                    + "] [animBlock = "
                    + str(an_anim_section.animBlock)
                    + "]",
                )
            else:
                anim_block = an_anim_section.animBlock
                description = "MDL" if anim_block == 0 else "ANI"
                log.add(
                    file_offset_start,
                    file_offset_end,
                    "anAnimSection ["
                    + an_animation_desc.theName
                    + "] [animBlock = "
                    + str(anim_block)
                    + "] [calculated "
                    + description
                    + " file Offset = "
                    + str(
                        self.theMdlFileData.theAnimBlocks[anim_block].dataStart
                        + an_anim_section.animOffset
                    )
                    + "]",
                )
        except MemoryError:
            raise
        except Exception:
            # the parser: Catch ex As Exception / Dim debug As Integer = 4242
            pass

    def read_mdl_movements(
        self, anim_input_file_stream_position: int, an_animation_desc: SourceMdlAnimationDesc49
    ) -> None:
        """Implement ``SourceMdlFile49.ReadMdlMovements`` -- ``movementCount`` x 40 bytes."""
        reader = self.theInputFileReader

        if an_animation_desc.movementCount <= 0:
            return

        reader.seek(anim_input_file_stream_position + an_animation_desc.movementOffset, SEEK_BEGIN)
        file_offset_start = reader.position

        an_animation_desc.theMovements = []
        for _ in range(an_animation_desc.movementCount):
            a_movement = SourceMdlMovement()

            a_movement.endframeIndex = reader.read_int32()
            a_movement.motionFlags = reader.read_int32()
            a_movement.v0 = reader.read_single()
            a_movement.v1 = reader.read_single()
            a_movement.angle = reader.read_single()

            a_movement.vector = SourceVector()
            a_movement.vector.x = reader.read_single()
            a_movement.vector.y = reader.read_single()
            a_movement.vector.z = reader.read_single()
            a_movement.position = SourceVector()
            a_movement.position.x = reader.read_single()
            a_movement.position.y = reader.read_single()
            a_movement.position.z = reader.read_single()

            an_animation_desc.theMovements.append(a_movement)

        file_offset_end = reader.position - 1
        self._seek_log.add(
            file_offset_start,
            file_offset_end,
            "anAnimationDesc.theMovements " + str(len(an_animation_desc.theMovements)),
        )

    def read_local_hierarchies(
        self, anim_input_file_stream_position: int, an_animation_desc: SourceMdlAnimationDesc49
    ) -> int:
        """Implement ``SourceMdlFile49.ReadLocalHierarchies``."""
        reader = self.theInputFileReader

        if an_animation_desc.localHierarchyCount <= 0:
            return 0

        reader.seek(
            anim_input_file_stream_position + an_animation_desc.localHierarchyOffset, SEEK_BEGIN
        )
        file_offset_start = reader.position

        an_animation_desc.theLocalHierarchies = []
        for _ in range(an_animation_desc.localHierarchyCount):
            a_local_hierarchy = SourceMdlLocalHierarchy()

            a_local_hierarchy.boneIndex = reader.read_int32()
            a_local_hierarchy.boneNewParentIndex = reader.read_int32()
            a_local_hierarchy.startInfluence = reader.read_single()
            a_local_hierarchy.peakInfluence = reader.read_single()
            a_local_hierarchy.tailInfluence = reader.read_single()
            a_local_hierarchy.endInfluence = reader.read_single()
            a_local_hierarchy.startFrameIndex = reader.read_int32()
            a_local_hierarchy.localAnimOffset = reader.read_int32()
            for x in range(len(a_local_hierarchy.unused)):
                a_local_hierarchy.unused[x] = reader.read_int32()

            an_animation_desc.theLocalHierarchies.append(a_local_hierarchy)

        file_offset_end = reader.position - 1
        self._seek_log.add(
            file_offset_start,
            file_offset_end,
            "anAnimationDesc.theLocalHierarchies "
            + str(len(an_animation_desc.theLocalHierarchies)),
        )

        self._seek_log.log_to_end_and_align_to_next_start(
            reader, file_offset_end, 4, "anAnimationDesc.theLocalHierarchies alignment"
        )

        return reader.position - 1

    # ==================================================================
    # Bookkeeping
    # ==================================================================

    def read_unread_bytes(self) -> None:
        """Implement ``SourceMdlFile49.ReadUnreadBytes``."""
        self._seek_log.log_unread_bytes(self.theInputFileReader)


class SourceMdlFile49AnimationMixin(_AnimationReaderBase):
    """The animation readers of ``SourceMdlFile49``, as a mixin.

    ``loka.core.mdl_file.SourceMdlFile49`` (the parallel MDL parser) should
    inherit this class so that the VB's own split -- ``SourceMdlFile49`` owns the
    animation readers, ``SourceAniFile49`` inherits them -- is preserved::

        from .ani import SourceMdlFile49AnimationMixin

        class SourceMdlFile49(SourceMdlFile49AnimationMixin):
            def __init__(self, reader, mdl_file_data):
                self.theInputFileReader = reader
                self.theMdlFileData = mdl_file_data
            ...

    Required call order (``SourceModel49.ReadMdlFile_Internal``)::

        read_bones()
        read_bone_controllers()
        read_attachments()
        read_hitbox_sets()
        read_bone_table_by_name()
        if localAnimationCount > 0:
            read_local_animation_descs()   # sets theOffsetStart + sections of nothing yet
            read_animation_sections()      # creates the per-section containers
            read_animation_mdl_blocks()    # fills the MDL-resident sections
    """


# ===========================================================================
# Path helpers (PORT NOTE: System.IO.Path equivalents)
# ===========================================================================


def _get_path(file_name: str) -> str:
    """Implement ``FileManager.GetPath`` -- the directory part, with trailing separator."""
    index = max(file_name.rfind("/"), file_name.rfind("\\"))
    return file_name[: index + 1] if index >= 0 else ""


def _get_file_name(file_name: str) -> str:
    """Implement ``Path.GetFileName``."""
    index = max(file_name.rfind("/"), file_name.rfind("\\"))
    return file_name[index + 1 :]


def _path_combine(directory: str, file_name: str) -> str:
    """Implement ``Path.Combine`` for the two operands used by the animation names."""
    if not directory:
        return file_name
    if directory.endswith(("/", "\\")):
        return directory + file_name
    return directory + "/" + file_name


# ===========================================================================
# The ANI file drivers
# ===========================================================================


class SourceAniFile49(_AnimationReaderBase):
    """Implement ``SourceAniFile49`` -- the ``.ani`` driver for MDL v49 (and v45..v59).

    Constructor contract:

    * ``theInputFileReader``      -- the reader positioned on the ``.ani`` file;
    * ``theMdlFileData``          -- the **ANI's own** file data (``SourceMdlFileData49``),
      whose every header offset is zero in a real file;
    * ``theRealMdlFileData``      -- the **real MDL's** file data, which supplies
      ``theBones``, ``theAnimationDescs`` and -- crucially -- ``theAnimBlocks``.
    """

    def __init__(
        self,
        reader: BinaryReader,
        ani_file_data: SourceMdlFileData49,
        mdl_file_data: SourceMdlFileData49,
    ) -> None:
        self.theInputFileReader = reader
        self.theMdlFileData = ani_file_data
        self.theRealMdlFileData = mdl_file_data

        self.theMdlFileData.theFileSeekLog = FileSeekLog()  # type: ignore[attr-defined]
        self.theMdlFileData.theFileSeekLog.FileSize = reader.length  # type: ignore[attr-defined]

        # NOTE: need the bone data from the real MDL file because SourceAniFile
        # inherits SourceMdlFile.ReadMdlAnimation() that uses the data.
        if mdl_file_data is not None:
            self.theMdlFileData.theBones = mdl_file_data.theBones
        else:  # pragma: no cover - defensive; the VB would throw a NullReferenceException
            self.theMdlFileData.theBones = None

    # ------------------------------------------------------------------
    # Drivers
    # ------------------------------------------------------------------
    def read_ani_file(self) -> None:
        """The whole ``ReadAniFile_Internal`` body, in order."""
        self.read_mdl_header00("ANI File Header 00")
        self.read_mdl_header01("ANI File Header 01")
        self.read_animation_ani_blocks()
        self.read_unread_bytes()

    def read_animation_ani_blocks(self) -> None:
        """Implement ``SourceAniFile49.ReadAnimationAniBlocks``.

        Reads every **ANI-resident** animation through the *real MDL's* anim-block
        table.  Notes that matter:

        * ``theAnimBlocks[animdesc.animBlock]`` is evaluated up front for every
          animdesc -- index 0 (the ``{0, 0}`` MDL pseudo-block) for MDL-resident
          animdescs -- so ``theAnimBlocks`` must exist; the whole body is inside the
          VB's per-animdesc ``Try ... Catch``, which this port preserves.
        * ``animBlockInputFileStreamPosition`` is reassigned inside the section loop, so
          after the loop it holds the ``dataStart`` of the **last processed section**,
          and that stale value is what the IK-rule / local-hierarchy calls use.  This is
          reproduced literally.
        * ``self.theMdlFileData.version`` here is the **ANI** version (which equals the
          MDL's), and the branch is ``<> 44``, i.e. taken for v48/v49 ANIs.
        """
        real = self.theRealMdlFileData

        if real.theAnimationDescs is None:
            return

        for an_animation_desc in real.theAnimationDescs:
            try:
                anim_block_input_file_stream_position = real.theAnimBlocks[  # type: ignore[index]
                    an_animation_desc.animBlock
                ].dataStart
                section_index = 0

                # NOTE: there can be sections in the ANI file even if
                # anAnimationDesc.animBlock = 0.
                if (
                    an_animation_desc.theSections is not None
                    and len(an_animation_desc.theSections) > 0
                ):
                    section_count = len(an_animation_desc.theSections)

                    for section_index in range(section_count):
                        section = an_animation_desc.theSections[section_index]
                        if section.animBlock > 0:
                            if section_index < section_count - 2:
                                section_frame_count = an_animation_desc.sectionFrameCount
                            else:
                                # NOTE: due to the weird sectionCount calculation in
                                # studiomdl this line is hit twice, i.e. there are two
                                # "last" sections; the final one is likely bogus data.
                                section_frame_count = an_animation_desc.frameCount - (
                                    (section_count - 2) * an_animation_desc.sectionFrameCount
                                )

                            anim_block_input_file_stream_position = real.theAnimBlocks[
                                section.animBlock
                            ].dataStart
                            self.read_animation_frames(
                                anim_block_input_file_stream_position + section.animOffset,
                                an_animation_desc,
                                section_frame_count,
                                section_index,
                                (section_index >= section_count - 2)
                                or (
                                    an_animation_desc.frameCount
                                    == (section_index + 1) * an_animation_desc.sectionFrameCount
                                ),
                            )
                elif an_animation_desc.animBlock > 0:
                    section_index = 0
                    self.read_animation_frames(
                        anim_block_input_file_stream_position + an_animation_desc.animOffset,
                        an_animation_desc,
                        an_animation_desc.frameCount,
                        section_index,
                        True,
                    )

                # NOTE: these seem to always be stored in the MDL file for MDL44.
                if self.theMdlFileData.version != 44 and an_animation_desc.animBlock > 0:
                    self.read_mdl_ik_rules(
                        anim_block_input_file_stream_position
                        + an_animation_desc.animblockIkRuleOffset,
                        an_animation_desc,
                    )
                    self.read_local_hierarchies(
                        anim_block_input_file_stream_position, an_animation_desc
                    )
            except MemoryError:
                raise
            except Exception:
                # the parser: Catch ex As Exception / Dim debug As Integer = 4242
                continue


class SourceAniFile44(SourceAniFile49):
    """Implement ``SourceAniFile44`` -- the same driver for the v44 ANI layout.

    ``SourceAniFile44`` differs from ``SourceAniFile49`` only in the element type
    of ``theRealMdlFileData.theAnimationDescs`` (``SourceMdlAnimationDesc44``, which the
    Python port represents with the shared ``SourceMdlAnimationDesc49``) -- the section
    test is ``section.animBlock > 0`` in **both** files, and the version gate is the
    same ``<> 44`` (which is always false for a real v44 ANI, so IK rules and local
    hierarchies come from the MDL pass).
    """


# ===========================================================================
# Optional convenience on top of the readers
# ===========================================================================


class AnimationValueReader:
    """Tiny helper bundling ``ToEulerAngles``-style consumers for one animation.

    The RLE path does not materialise per-frame values: it keeps the RLE spans and
    evaluates :func:`extract_anim_value` on demand.  This class implements the two
    consumers the parser has (``SourceSmdFile49.CalcBoneRotation`` /
    ``CalcBonePosition``) so callers do not have to re-derive the flag handling.
    """

    __slots__ = ()

    @staticmethod
    def calc_bone_rotation(
        frame_index: int,
        a_bone: SourceMdlBone,
        an_animation: SourceMdlAnimation,
        rotation_quat_out: Optional[SourceQuaternion] = None,
    ) -> SourceVector:
        """Implement ``SourceSmdFile49.CalcBoneRotation`` (``s`` is always 0 at the call site)."""
        rot = SourceQuaternion()
        angle_vector = SourceVector()

        if (an_animation.flags & STUDIO_ANIM_RAWROT) > 0:
            raw48 = an_animation.theRot48bits
            assert raw48 is not None
            rot.x = raw48.x
            rot.y = raw48.y
            rot.z = raw48.z
            rot.w = raw48.w
            if rotation_quat_out is not None:
                rotation_quat_out.x = rot.x
                rotation_quat_out.y = rot.y
                rotation_quat_out.z = rot.z
                rotation_quat_out.w = rot.w
            angle_vector = to_euler_angles(rot)
            angle_vector.debug_text = "raw48"
            return angle_vector

        if (an_animation.flags & STUDIO_ANIM_RAWROT2) > 0:
            raw64 = an_animation.theRot64bits
            assert raw64 is not None
            rot.x = raw64.x
            rot.y = raw64.y
            rot.z = raw64.z
            rot.w = raw64.w
            if rotation_quat_out is not None:
                rotation_quat_out.x = rot.x
                rotation_quat_out.y = rot.y
                rotation_quat_out.z = rot.z
                rotation_quat_out.w = rot.w
            angle_vector = to_euler_angles(rot)
            angle_vector.debug_text = "raw64"
            return angle_vector

        if (an_animation.flags & STUDIO_ANIM_ANIMROT) == 0:
            if (an_animation.flags & STUDIO_ANIM_DELTA) > 0:
                angle_vector.x = 0.0
                angle_vector.y = 0.0
                angle_vector.z = 0.0
                if rotation_quat_out is not None:
                    rotation_quat_out.x = 0.0
                    rotation_quat_out.y = 0.0
                    rotation_quat_out.z = 0.0
                    rotation_quat_out.w = 0.0
                angle_vector.debug_text = "delta"
            else:
                angle_vector.x = a_bone.rotation.x
                angle_vector.y = a_bone.rotation.y
                angle_vector.z = a_bone.rotation.z
                if rotation_quat_out is not None:
                    rotation_quat_out.x = 0.0
                    rotation_quat_out.y = 0.0
                    rotation_quat_out.z = 0.0
                    rotation_quat_out.w = 0.0
                angle_vector.debug_text = "bone"
            return angle_vector

        rot_v = an_animation.theRotV
        assert rot_v is not None

        if rot_v.animXValueOffset <= 0:
            angle_vector.x = 0.0
        else:
            angle_vector.x = extract_anim_value(
                frame_index, rot_v.theAnimXValues, a_bone.rotationScale.x
            )
        if rot_v.animYValueOffset <= 0:
            angle_vector.y = 0.0
        else:
            angle_vector.y = extract_anim_value(
                frame_index, rot_v.theAnimYValues, a_bone.rotationScale.y
            )
        if rot_v.animZValueOffset <= 0:
            angle_vector.z = 0.0
        else:
            angle_vector.z = extract_anim_value(
                frame_index, rot_v.theAnimZValues, a_bone.rotationScale.z
            )

        angle_vector.debug_text = "anim"

        if (an_animation.flags & STUDIO_ANIM_DELTA) == 0:
            angle_vector.x += a_bone.rotation.x
            angle_vector.y += a_bone.rotation.y
            angle_vector.z += a_bone.rotation.z
            angle_vector.debug_text += "+bone"

        if rotation_quat_out is not None:
            quat = euler_angles_to_quaternion(angle_vector)
            rotation_quat_out.x = quat.x
            rotation_quat_out.y = quat.y
            rotation_quat_out.z = quat.z
            rotation_quat_out.w = quat.w
        return angle_vector

    @staticmethod
    def calc_bone_position(
        frame_index: int, a_bone: SourceMdlBone, an_animation: SourceMdlAnimation
    ) -> SourceVector:
        """Implement ``SourceSmdFile49.CalcBonePosition`` (``s`` is always 0 at the call site)."""
        pos = SourceVector()

        if (an_animation.flags & STUDIO_ANIM_RAWPOS) > 0:
            raw_pos = an_animation.thePos
            assert raw_pos is not None
            pos.x = raw_pos.x
            pos.y = raw_pos.y
            pos.z = raw_pos.z
            pos.debug_text = "raw"
            return pos

        if (an_animation.flags & STUDIO_ANIM_ANIMPOS) == 0:
            if (an_animation.flags & STUDIO_ANIM_DELTA) > 0:
                pos.x = 0.0
                pos.y = 0.0
                pos.z = 0.0
                pos.debug_text = "delta"
            else:
                pos.x = a_bone.position.x
                pos.y = a_bone.position.y
                pos.z = a_bone.position.z
                pos.debug_text = "bone"
            return pos

        pos_v = an_animation.thePosV
        assert pos_v is not None

        if pos_v.animXValueOffset <= 0:
            pos.x = 0.0
        else:
            pos.x = extract_anim_value(
                frame_index, pos_v.theAnimXValues, a_bone.positionScale.x
            )
        if pos_v.animYValueOffset <= 0:
            pos.y = 0.0
        else:
            pos.y = extract_anim_value(
                frame_index, pos_v.theAnimYValues, a_bone.positionScale.y
            )
        if pos_v.animZValueOffset <= 0:
            pos.z = 0.0
        else:
            pos.z = extract_anim_value(
                frame_index, pos_v.theAnimZValues, a_bone.positionScale.z
            )

        pos.debug_text = "anim"

        if (an_animation.flags & STUDIO_ANIM_DELTA) == 0:
            pos.x += a_bone.position.x
            pos.y += a_bone.position.y
            pos.z += a_bone.position.z
            pos.debug_text += "+bone"

        return pos
