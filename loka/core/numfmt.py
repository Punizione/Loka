"""Locale-independent Source text formatting.

Numbers use a decimal point, no thousands separators, and explicit rounding
half away from zero. Decimal(float) retains the exact input binary value.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP

__all__ = [
    "round_half_away_from_zero",
    "fmt_decimals",
    "fmt_fixed6",
    "fmt_trim6",
    "fmt_f2",
    "fmt_int",
    "fmt_single_decimals",
    "fmt_single_fixed6",
    "fmt_single_trim6",
    "fmt_single_f2",
]


def round_half_away_from_zero(value: float, decimals: int) -> Decimal:
    """Round ``value`` to ``decimals`` places, half away from zero, like .NET Framework."""
    if math.isnan(value) or math.isinf(value):
        # .NET renders these as "NaN"/"Infinity"; callers that can hit them handle it.
        return Decimal(repr(value))
    quantum = Decimal(1).scaleb(-decimals)
    return Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


def _drop_negative_zero(rounded: Decimal) -> Decimal:
    """Drop the sign of a value that rounded to zero.

    Verified empirically against .NET Framework 4.x: ``(-0.0).ToString("0.000000")``
    and ``(-1e-7).ToString("0.000000")`` both yield ``"0.000000"`` without a sign,
    while ``(-5e-7).ToString("0.000000")`` yields ``"-0.000001"`` (half away from
    zero).  So the sign is dropped based on the *rounded* value, not the original.
    """
    if rounded == 0:
        return abs(rounded)
    return rounded


def fmt_decimals(value: float, decimals: int) -> str:
    """Format with exactly ``decimals`` digits after the point (e.g. ``"0.000000"``)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    rounded = _drop_negative_zero(round_half_away_from_zero(value, decimals))
    return format(rounded, "f")


def fmt_fixed6(value: float) -> str:
    """VB ``"0.000000"`` -- used for almost every SMD/QC float."""
    return fmt_decimals(value, 6)


def fmt_trim6(value: float) -> str:
    """VB ``"0.######"`` -- up to six decimals, trailing zeros trimmed.

    This is the format the parser's QC writer uses for most angles and offsets.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    rounded = _drop_negative_zero(round_half_away_from_zero(value, 6))
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def fmt_f2(value: float) -> str:
    """VB ``"0.##"`` -- up to two decimals, trailing zeros trimmed."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    rounded = _drop_negative_zero(round_half_away_from_zero(value, 2))
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def fmt_int(value: int) -> str:
    """VB ``Integer.ToString(InternalNumberFormat)`` -- plain invariant digits."""
    return str(int(value))


# ---------------------------------------------------------------------------
# VB ``Single`` fields format differently from ``Double`` fields.
#
# .NET Framework converts a ``Single`` to a decimal using **7 significant
# digits** before applying the format string, while a ``Double`` is used at full
# precision.  Verified exhaustively: formatting all 16,656 bone weights of
# hl2/models/gman.vvd with this model matches .NET Framework's
# ``Single.ToString("0.000000")`` for every single value.
#
# This matters because the parser's model classes mix the two: for example
# ``SourceVertex.positionX`` is a ``Double`` (exact) while
# ``SourceBoneWeight.weight`` is a ``Single`` (7-digit pre-rounded).
# ---------------------------------------------------------------------------

def _single_decimal(value: float) -> Decimal:
    """Convert a 32-bit float the way .NET Framework formats a ``Single``."""
    return Decimal(f"{value:.7g}")


def fmt_single_decimals(value: float, decimals: int) -> str:
    """Like :func:`fmt_decimals` but for a VB ``Single`` field."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    quantum = Decimal(1).scaleb(-decimals)
    rounded = _single_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)
    return format(_drop_negative_zero(rounded), "f")


def fmt_single_fixed6(value: float) -> str:
    """VB ``"0.000000"`` applied to a ``Single`` (e.g. bone weights in SMD)."""
    return fmt_single_decimals(value, 6)


def fmt_single_trim6(value: float) -> str:
    """VB ``"0.######"`` applied to a ``Single``."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    rounded = _drop_negative_zero(
        _single_decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    )
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def fmt_single_f2(value: float) -> str:
    """VB ``"0.##"`` applied to a ``Single``."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    rounded = _drop_negative_zero(
        _single_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"
