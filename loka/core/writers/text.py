"""UTF-8 text output, numeric formatting and generated-file headers."""

from __future__ import annotations

import decimal
import struct
from typing import Callable, Iterable, Optional
from ... import __version__

__all__ = [
    "WriteLine",
    "TextWriter",
    "NAN_SYMBOL",
    "POSITIVE_INFINITY_SYMBOL",
    "NEGATIVE_INFINITY_SYMBOL",
    "PRESERVE_NEGATIVE_ZERO",
    "DEFAULT_PRODUCT_NAME",
    "DEFAULT_VERSION",
    "fmt_fixed6",
    "fmt_trim",
    "fmt_int",
    "join_fields",
    "get_product_name_and_version",
    "get_header_comment",
    "write_header_comment",
    "half_to_float",
]

#: Signature of the parser's ``StreamWriter.WriteLine(String)``.  Both writers take
#: such a callable so that they can be driven by a :class:`TextWriter` or by any
#: callable that collects output lines.
WriteLine = Callable[[str], None]

#: Symbols produced by the ``en-US`` ``NumberFormatInfo`` for non-finite values.
NAN_SYMBOL = "NaN"
POSITIVE_INFINITY_SYMBOL = "\u221e"
NEGATIVE_INFINITY_SYMBOL = "-\u221e"

_TRIM6_EXPONENT = 6

#: ``CultureInfo("en-US", False).NumberFormat.NegativeSign`` / ``PositiveSign``.
_NEGATIVE_SIGN = "-"
_POSITIVE_SIGN = ""
_DECIMAL_SEPARATOR = "."

#: Source text output normalizes negative zero unless explicitly requested.
PRESERVE_NEGATIVE_ZERO = False
_PRESERVE_NEGATIVE_ZERO = PRESERVE_NEGATIVE_ZERO

#: Export headers use the current application identity without a second version.
DEFAULT_PRODUCT_NAME = f"Loka {__version__}"
DEFAULT_VERSION = ()


class TextWriter:
    """Buffer UTF-8 text with CRLF line endings; write the completed file on close."""

    __slots__ = ("_path", "_encoding", "_lines", "_closed")

    def __init__(self, path: Optional[str] = None, encoding: str = "utf-8") -> None:
        self._path = path
        self._encoding = encoding
        self._lines: list[str] = []
        self._closed = False

    # -- collecting ----------------------------------------------------------
    def write_line(self, line: str = "") -> None:
        """Implement ``StreamWriter.WriteLine(String)``.

        ``WriteLine()`` with no argument emits an empty line; that is exactly
        what ``SourceVrdFile49.WriteCommands`` line 62 does before ``<helper>``.
        """
        self._lines.append(line)

    # ``TextWriter`` is also a plain callable, so it can be handed straight to
    # the writers' ``write_line`` parameter without wrapping.
    __call__ = write_line

    @property
    def lines(self) -> list[str]:
        return self._lines

    def getvalue(self) -> str:
        """The complete file contents (CRLF terminated, no BOM)."""
        return "".join(line + "\r\n" for line in self._lines)

    # -- persisting ----------------------------------------------------------
    def save(self, path: Optional[str] = None) -> str:
        """Write the accumulated text to ``path`` (or the constructor's path)."""
        target = path if path is not None else self._path
        if target is None:
            raise ValueError("no output path was given to TextWriter")
        # ``newline=""`` stops Python from translating "\n" and therefore keeps
        # the explicit CRLF terminators byte-exact.
        with open(target, "w", encoding=self._encoding, newline="") as handle:
            handle.write(self.getvalue())
        return target

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._path is not None:
            self.save(self._path)

    # ``flush`` mirrors the VB ``Finally`` block's ``Flush()``; because the whole
    # buffer is written at once there is nothing to flush before ``close``.
    def flush(self) -> None:  # pragma: no cover - API compatibility
        return

    def __enter__(self) -> "TextWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._lines)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TextWriter({self._path or '<memory>'!r}, lines={len(self._lines)})"


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------

def _quantize_half_away_from_zero(value: float, exponent: int = _TRIM6_EXPONENT) -> decimal.Decimal:
    """Round ``value`` to ``exponent`` decimal places, halves away from zero.

    The input is converted through :func:`decimal.Decimal.__init__`'s two-argument
    form (``Decimal(float)``), i.e. from the *exact* binary value of the double.
    That preserves the rounding decision for doubles that are meant to sit on a
    half-way boundary but are not exactly representable (for example
    ``0.0000005``, whose double is a hair *below* 5e-7 and therefore legitimately
    rounds down).
    """
    quantized = decimal.Decimal(value)
    if exponent != 0:
        quantized = quantized.quantize(
            decimal.Decimal(1).scaleb(-exponent), rounding=decimal.ROUND_HALF_UP
        )
    else:
        quantized = quantized.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP)
    return quantized


def _fixed6_digits(quantized: decimal.Decimal) -> tuple[bool, str, str]:
    """Split an already-rounded decimal into ``(negative, integer, fraction)``.

    The decimal was quantized to six places, so ``f"{quantized:.6f}"`` renders it
    exactly (no further rounding) and the split is lossless.  Both .NET formats
    used by these writers have the ``"0.######"``-family shape: exactly one
    mandatory integer digit and a fixed number of fractional placeholders.
    """
    text = f"{quantized:.6f}"
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    integer_text, _, fraction_text = text.partition(".")
    if not integer_text:
        integer_text = "0"
    return negative, integer_text, fraction_text


def _non_finite_symbol(value: float) -> Optional[str]:
    """The culture symbols .NET prints for non-finite doubles, else ``None``."""
    if value != value:  # NaN
        return NAN_SYMBOL
    if value == float("inf"):
        return POSITIVE_INFINITY_SYMBOL
    if value == float("-inf"):
        return NEGATIVE_INFINITY_SYMBOL
    return None


def fmt_fixed6(value: float, preserve_negative_zero: bool = _PRESERVE_NEGATIVE_ZERO) -> str:
    """Implement ``value.ToString("0.000000", TheApp.InternalNumberFormat)``.

    Always exactly six fractional digits.  ``-0.0`` (and any value that *rounds* to
    zero) prints as ``0.000000`` on the runtime the parser actually ships on; see
    :data:`PRESERVE_NEGATIVE_ZERO` for the evidence and for the switch that turns
    the sign back on.  Infinities/NaN print as ``∞`` / ``-∞`` / ``NaN``.
    """
    number = float(value)
    symbol = _non_finite_symbol(number)
    if symbol is not None:
        return symbol

    negative, integer_text, fraction_text = _fixed6_digits(
        _quantize_half_away_from_zero(number, 6)
    )
    if not preserve_negative_zero and not integer_text.lstrip("0") and not fraction_text.strip("0"):
        # The rounded value is exactly zero: .NET Framework 4.8 drops the sign
        # (measured -- see PRESERVE_NEGATIVE_ZERO).
        return f"0{_DECIMAL_SEPARATOR}{fraction_text}"
    prefix = _NEGATIVE_SIGN if negative else _POSITIVE_SIGN
    return f"{prefix}{integer_text}{_DECIMAL_SEPARATOR}{fraction_text}"


def fmt_trim(value: float, preserve_negative_zero: bool = _PRESERVE_NEGATIVE_ZERO) -> str:
    """Implement ``value.ToString("0.######", TheApp.InternalNumberFormat)``.

    Zero to six fractional digits with trailing zeros dropped; an integral value
    prints with no decimal point at all (``1``, not ``1.0``).  ``-0`` prints as
    ``0`` on every .NET runtime, so ``preserve_negative_zero`` only affects a value
    like ``-0.0000001``, which rounds to an integral zero.
    """
    number = float(value)
    symbol = _non_finite_symbol(number)
    if symbol is not None:
        return symbol

    negative, integer_text, fraction_text = _fixed6_digits(
        _quantize_half_away_from_zero(number, 6)
    )
    fraction_text = fraction_text.rstrip("0")
    if not fraction_text:
        # Nothing but zeros after the point: "0.######" emits the integer only.
        if not preserve_negative_zero and not integer_text.lstrip("0"):
            return "0"
        return ("-" if negative and integer_text.lstrip("0") else "") + integer_text
    prefix = _NEGATIVE_SIGN if negative else _POSITIVE_SIGN
    return f"{prefix}{integer_text}{_DECIMAL_SEPARATOR}{fraction_text}"


def fmt_int(value: int) -> str:
    """Implement ``value.ToString(TheApp.InternalNumberFormat)`` (the ``"G"`` format).

    Uses the culture's negative sign and no group separator; ``en-US`` negative
    sign is ``-``, which is also what Python's ``str()`` emits for an ``int``.
    """
    return str(int(value))


def join_fields(prefix: str, fields: Iterable[str]) -> str:
    """Join ``prefix`` and single-space separated ``fields``.

    Every quoted VB line in the writers is built as ``prefix + " " + field +
    " " + field ...``; this helper makes that shape explicit and impossible to get
    subtly wrong (a trailing space, a stray tab).
    """
    return " ".join((prefix, *fields))


# ---------------------------------------------------------------------------
# Header comment
# ---------------------------------------------------------------------------

def get_product_name_and_version(
    product_name: str = DEFAULT_PRODUCT_NAME,
    version: tuple[int, ...] = DEFAULT_VERSION,
) -> str:
    """Implement ``App.GetProductNameAndVersion``.

    ``AssemblyProduct`` plus ``Version.ToString(2)`` -- i.e. ``"Major.Minor"``.
    """
    as_text = ".".join(str(part) for part in version[:2])
    return f"{product_name} {as_text}".rstrip()


def get_header_comment(
    product_name: str = DEFAULT_PRODUCT_NAME,
    version: tuple[int, ...] = DEFAULT_VERSION,
) -> str:
    """Implement ``App.GetHeaderComment`` -- ``"Created by <product> <major.minor>"``."""
    return "Created by " + get_product_name_and_version(product_name, version)


def write_header_comment(
    writer: WriteLine,
    stricter_format: bool = False,
    product_name: str = DEFAULT_PRODUCT_NAME,
    version: tuple[int, ...] = DEFAULT_VERSION,
) -> None:
    """Implement ``Common.WriteHeaderComment``.

    Emits ``"// Created by <product> <major.minor>"`` unless the "stricter format"
    decompile option is enabled, in which case nothing at all is written.
    """
    if stricter_format:
        return
    writer("// " + get_header_comment(product_name, version))


# ---------------------------------------------------------------------------
# Half-float decoding
# ---------------------------------------------------------------------------

def half_to_float(the16_bit_value: int) -> float:
    """Implement ``SourceFloat16bits.TheFloatValue``.

    ``SourceMdlVertAnim`` stores its position/normal deltas as 16-bit half
    floats; the parser expands them with
    ``sign | ((exp - 15 + 127) << 23) | (mantissa << 13)`` (all arithmetic exact),
    maps infinities to ``+/-65504`` and NaN to ``0``.  ``struct``'s ``'e'``
    format produces the same finite values, so only the two special cases need
    fixing up.

    The Python port stores already-decoded floats on
    ``SourceMdlVertAnim.theDelta`` / ``.theNDelta``; this helper exists so a
    parser (or a test) can reproduce the VB conversion exactly.
    """
    raw = int(the16_bit_value) & 0xFFFF
    value = struct.unpack("<e", struct.pack("<H", raw))[0]
    if value != value:  # NaN
        return 0.0
    if value == float("inf"):
        return 65504.0
    if value == float("-inf"):
        return -65504.0
    return value
