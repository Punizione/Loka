"""UV-local construction programs, without source skins or animated textures.

Each UV selects regional operators through a shared SMD island atlas. Material RGB is computed
from the user's image; effect RGB is computed from a hue, floor and strength.
"""
from __future__ import annotations

from dataclasses import dataclass

from .sequence_material_phases import PRINT_HEAD

GAIN_STEPS = 63
LIGHT_STEPS = 127
EFFECT = 4
WIRE_COLOR = 8
SHARED_MATERIAL = 16
FIELD_NAMES = ('mode', 'alpha', 'red_gain', 'green_gain', 'blue_gain',
               'effect_floor', 'effect_strength', 'effect_hue', 'material_gain')


@dataclass(frozen=True)
class ParameterCurves:
    indices: object
    values: tuple[bytes, ...]

    def at(self, frame, *, cancelled=lambda: False):
        from .sequence_material_vtf import check_cancel
        output = bytearray(len(self.indices))
        for start in range(0, len(output), 16384):
            check_cancel(cancelled)
            end = min(start + 16384, len(output))
            output[start:end] = bytes(self.values[self.indices[i]][frame]
                                      for i in range(start, end))
        return bytes(output)


@dataclass(frozen=True)
class MaterialProgram:
    frames: int
    palette: tuple[tuple[int, int, int], ...]
    fields: tuple[ParameterCurves, ...]


def _tone(value, coefficient):
    delta = coefficient - GAIN_STEPS
    if delta <= 0:
        return (value * (GAIN_STEPS + delta) + GAIN_STEPS // 2) // GAIN_STEPS
    return (value * (GAIN_STEPS - delta) + 255 * delta + GAIN_STEPS // 2) // GAIN_STEPS


def synthesize_rule_frame(static, template, rule, frame_index, *, sweep_color=None,
                          wire_color=None, cancelled=lambda: False):
    from PIL import Image
    from .sequence_material_vtf import check_cancel
    if not 0 <= frame_index < template.program.frames:
        raise ValueError('材质阶段帧索引超出范围。')
    parameters = [field.at(frame_index, cancelled=cancelled) for field in template.program.fields]
    modes, alphas, red, green, blue, floors, strengths, hues, material_gain = parameters
    current = static.tobytes()
    output = bytearray(len(current))
    for pixel, mode in enumerate(modes):
        if pixel % (rule.size * 16) == 0:
            check_cancel(cancelled)
        at = pixel * 4
        if mode & EFFECT:
            floor = (floors[pixel] * 255 + LIGHT_STEPS // 2) // LIGHT_STEPS
            strength = (strengths[pixel] * 255 + LIGHT_STEPS // 2) // LIGHT_STEPS
            if wire_color is not None and mode & WIRE_COLOR:
                peak = min(255, floor + strength)
                rgb = tuple((peak * c + 127) // 255 for c in wire_color)
            else:
                color = (sweep_color if sweep_color is not None and (mode & 3) == PRINT_HEAD
                         else template.program.palette[hues[pixel]])
                if sweep_color is not None and (mode & 3) == PRINT_HEAD and rule.layer == 'glow':
                    strength, floor = min(255, floor + strength), 0
                rgb = tuple(min(255, floor + (strength * c + 127) // 255) for c in color)
            output[at:at + 3] = bytes(rgb)
        else:
            rgb = (_tone(current[at], red[pixel]), _tone(current[at + 1], green[pixel]),
                   _tone(current[at + 2], blue[pixel]))
            if mode & SHARED_MATERIAL:
                gain = material_gain[pixel]
                if rule.layer == 'normal':
                    rgb = tuple((neutral * (LIGHT_STEPS - gain) + value * gain + LIGHT_STEPS // 2)
                                // LIGHT_STEPS for neutral, value in zip((128, 128, 255), rgb))
                else:
                    rgb = tuple(_tone(value, gain) for value in rgb)
            output[at:at + 3] = bytes(rgb)
        alpha = alphas[pixel]
        if alpha == 254:
            output[at + 3] = 0
        elif alpha == 255:
            output[at + 3] = 255
        elif alpha >= 128:
            output[at + 3] = ((alpha - 127) * 255 + 63) // 127
        else:
            output[at + 3] = _tone(current[at + 3], alpha)
    return Image.frombytes('RGBA', static.size, bytes(output))
