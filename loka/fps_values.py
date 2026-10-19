"""Quantize final playback rates without rounding timing calculations."""
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from fractions import Fraction
import math


def _decimal_fps(value):
    try:
        if isinstance(value, Fraction):
            return Decimal(str(scaled_fps(value, 1, 1)))
        if isinstance(value, bool):
            raise ValueError('FPS 无效')
        rate = Decimal(str(value))
        if not rate.is_finite() or rate <= 0:
            raise ValueError('FPS 无效')
        with localcontext() as context:
            context.prec = max(28, len(rate.as_tuple().digits) + max(0, rate.adjusted()) + 3)
            rate = rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if rate <= 0 or not math.isfinite(float(rate)):
            raise ValueError('FPS 四舍五入后无效')
        return rate
    except (DecimalException, TypeError, OverflowError, ValueError) as error:
        raise ValueError('FPS 必须是可四舍五入至两位小数的正有限数') from error


def round_fps(value) -> float:
    return float(_decimal_fps(value))


def fps_text(value) -> str:
    return format(_decimal_fps(value), '.2f')


def scaled_fps(value, numerator, denominator) -> float:
    """Multiply decimal inputs exactly, then round the final rate HALF_UP.

    Fraction factors are accepted so callers can retain multi-step timing ratios.
    No input rate, duration or ratio is quantized before the final hundredths.
    """
    def positive_fraction(item):
        if isinstance(item, bool):
            raise ValueError('FPS 比例必须为正有限数')
        result = item if isinstance(item, Fraction) else Fraction(Decimal(str(item)))
        if result <= 0:
            raise ValueError('FPS 比例必须为正有限数')
        return result
    try:
        rate = positive_fraction(value) * positive_fraction(numerator) / positive_fraction(denominator)
        hundredths, remainder = divmod(rate.numerator * 100, rate.denominator)
        if remainder * 2 >= rate.denominator:
            hundredths += 1
        result = float(Fraction(hundredths, 100))
        if hundredths <= 0 or not math.isfinite(result):
            raise ValueError('FPS 四舍五入后无效')
        return result
    except (DecimalException, TypeError, OverflowError, ZeroDivisionError, ValueError) as error:
        raise ValueError('FPS 和比例必须产生可四舍五入至两位小数的正有限数') from error


def round_qc_fps(text: str) -> str:
    """Normalize rate operands, preserving comments, events and other numbers."""
    # Import lazily: the QC writer consumes the scalar helpers in this module.
    from .weapon_qc import _tokens, _patch
    tokens = _tokens(text)
    depth = 0
    kind = None
    command_start = 0
    event_depth = None
    changes = []
    for index, token in enumerate(tokens):
        if token.quoted:
            continue
        if token.value == '{':
            depth += 1
            if index + 1 < len(tokens) and not tokens[index + 1].quoted and tokens[index + 1].value.lower() == 'event':
                event_depth = depth
        elif token.value == '}':
            if event_depth == depth:
                event_depth = None
            depth -= 1
        elif depth == 0 and token.value.startswith('$'):
            kind = token.value[1:].lower()
            command_start = index
        elif depth in (0, 1) and event_depth is None and kind in ('animation', 'sequence') and token.value.lower() == 'fps' and index > command_start + (2 if kind == 'animation' else 1):
            if index + 1 >= len(tokens) or tokens[index + 1].quoted:
                raise ValueError('QC FPS 缺少数值')
            operand = tokens[index + 1]
            changes.append((operand.start, operand.end, fps_text(operand.value)))
    return _patch(text, changes)
