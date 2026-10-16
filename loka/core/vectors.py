"""Vector and quaternion helpers. Binary float32 components are widened losslessly to Python float."""

from __future__ import annotations

import math
from typing import Iterable

__all__ = [
    "SourceVector",
    "SourceVector4D",
    "SourceQuaternion",
    "degrees_to_radians",
    "radians_to_degrees",
    "euler_angles_to_quaternion",
    "quaternion_to_euler_angles",
]


def degrees_to_radians(degrees: float) -> float:
    """Implement ``MathModule.DegreesToRadians``."""
    return degrees * math.pi / 180.0


def radians_to_degrees(radians: float) -> float:
    """Implement ``MathModule.RadiansToDegrees``."""
    return radians * 180.0 / math.pi


class SourceVector:
    """A 3D vector.  Implement ``SourceVector``."""

    __slots__ = ("x", "y", "z", "debug_text")

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.debug_text = ""

    # -- container-ish helpers (VB.NET exposes x/y/z as plain fields) ---------
    def copy(self) -> "SourceVector":
        return SourceVector(self.x, self.y, self.z)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def cross_product(self, other: "SourceVector") -> "SourceVector":
        """Implement ``SourceVector.CrossProduct``."""
        return SourceVector(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    def normalize(self) -> "SourceVector":
        """Implement ``SourceVector.Normalize``.

        NOTE: the parser does not guard against a zero magnitude here, so a zero
        vector yields NaN/inf exactly as the original does.
        """
        magnitude = math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)
        return SourceVector(self.x / magnitude, self.y / magnitude, self.z / magnitude)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceVector({self.x!r}, {self.y!r}, {self.z!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceVector):
            return NotImplemented
        return (self.x, self.y, self.z) == (other.x, other.y, other.z)


class SourceVector4D(SourceVector):
    """A 4D vector used for texture coordinates / bone weights.

    Implement ``SourceVector4D`` which extends ``SourceVector`` with a ``w``
    component.
    """

    __slots__ = ("w",)

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 0.0) -> None:
        super().__init__(x, y, z)
        self.w = float(w)

    def copy(self) -> "SourceVector4D":
        return SourceVector4D(self.x, self.y, self.z, self.w)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceVector4D({self.x!r}, {self.y!r}, {self.z!r}, {self.w!r})"


class SourceQuaternion:
    """A quaternion.  Implement ``SourceQuaternion``.

    Note that the parser uses ``w`` as the scalar part.  The quaternion is only used
    for procedural-bone (``.vrd``) output and animation decoding in this port.
    """

    __slots__ = ("x", "y", "z", "w", "debug_text")

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 0.0) -> None:
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.w = float(w)
        self.debug_text = ""

    def copy(self) -> "SourceQuaternion":
        return SourceQuaternion(self.x, self.y, self.z, self.w)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.z, self.w)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceQuaternion({self.x!r}, {self.y!r}, {self.z!r}, {self.w!r})"


# ---------------------------------------------------------------------------
# Angle conversions.  These mirror ``MathModule`` helpers used by the SMD writer
# to turn a stored quaternion into the Euler angles written to ``.smd`` files.
# ---------------------------------------------------------------------------

def _euler_to_axis_angle(
    x: float, y: float, z: float
) -> tuple[float, float, float, float]:
    """Implement ``MathModule.AngleQuaternion``-style Euler(x,y,z) -> quaternion.

    Angles are in radians and applied in the Source engine's ``pitch yaw roll``
    (x, y, z) order.
    """
    sy = math.sin(y * 0.5)
    cy = math.cos(y * 0.5)
    sp = math.sin(x * 0.5)
    cp = math.cos(x * 0.5)
    sr = math.sin(z * 0.5)
    cr = math.cos(z * 0.5)

    # Quaternion multiplication order matches Source's AngleQuaternion():
    #   q = (cy*cp*cr + sy*sp*sr,
    #        cy*sp*cr + sy*cp*sr,
    #        sy*cp*cr - cy*sp*sr,
    #        cy*cp*sr - sy*sp*cr)
    w = cy * cp * cr + sy * sp * sr
    qx = cy * sp * cr + sy * cp * sr
    qy = sy * cp * cr - cy * sp * sr
    qz = cy * cp * sr - sy * sp * cr
    return (qx, qy, qz, w)


def euler_angles_to_quaternion(x: float, y: float, z: float) -> SourceQuaternion:
    """Convert Euler angles (radians, pitch/yaw/roll) to a quaternion."""
    qx, qy, qz, qw = _euler_to_axis_angle(x, y, z)
    return SourceQuaternion(qx, qy, qz, qw)


def quaternion_to_euler_angles(
    quat: SourceQuaternion,
) -> SourceVector:
    """Convert a quaternion to Euler angles (radians) in Source's x/y/z order.

    This mirrors ``MathModule.QuaternionToEulerAngles`` semantics: the returned
    vector carries ``x`` = pitch, ``y`` = yaw, ``z`` = roll, all in radians.
    Handling of the degenerate gimbal-lock case matches the original by clamping
    the ``yaw`` term to +/- 90 degrees.
    """
    x, y, z, w = quat.x, quat.y, quat.z, quat.w

    # Normalize defensively; the original assumes a unit quaternion.
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length != 0.0:
        x, y, z, w = x / length, y / length, z / length, w / length

    # roll (z axis)
    sinr_cosp = 2.0 * (w * z + x * y)
    cosr_cosp = 1.0 - 2.0 * (y * y + z * z)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # pitch (x axis)
    sinp = 2.0 * (w * x - y * z)
    if abs(sinp) >= 1.0:
        pitch = math.copysign(math.pi / 2.0, sinp)
    else:
        pitch = math.asin(sinp)

    # yaw (y axis)
    siny_cosp = 2.0 * (w * y + z * x)
    cosy_cosp = 1.0 - 2.0 * (x * x + y * y)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return SourceVector(pitch, yaw, roll)


def dot(a: Iterable[float], b: Iterable[float]) -> float:
    """Simple float dot product helper."""
    return sum(x * y for x, y in zip(a, b))
