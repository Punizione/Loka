"""Measure and recolor only the reference animation's transient sweep light."""
from __future__ import annotations

from .sequence_material_phases import PRINT_HEAD, WIRE

SWEEP_COLOR_METHOD = 'authored-print-front-rgb-v3'
WIRE_COLOR_METHOD = 'authored-shared-wire-rgb-v2'


def _sweep_sample(frame, at, layer, phase):
    if layer == 'normal' or phase != PRINT_HEAD:
        return None
    rgb = frame[at:at + 3]
    peak = max(rgb)
    if peak < 12:
        return None
    light_color = tuple(round(c * 255 / peak) for c in rgb)
    strength = peak if layer == 'glow' else peak - min(rgb)
    if strength <= 0:
        return None
    return light_color, strength


def _wire_sample(frame, at, layer, phase):
    if layer in ('normal', 'glow') or phase != WIRE:
        return None
    rgb = frame[at:at + 3]
    peak = max(rgb)
    chroma = peak - min(rgb)
    # Phase zero also contains transparent black/background pixels. Only the
    # authored chromatic wire strokes are a color target; retain their alpha.
    if peak < 12 or chroma < max(8., peak * .08):
        return None
    return tuple(round(c * 255 / peak) for c in rgb), chroma


class SweepColorAccumulator:
    """Per-output dominant transient chromaticity, sampled across real frames."""

    def __init__(self, rule, *, wire=False):
        self.rule = rule
        self.wire = wire
        self.bins = {}

    def add(self, frame, phases, *, paired_frame=None):
        if self.rule.layer == 'normal' or (self.wire and self.rule.layer == 'glow'):
            return
        sample_pixel = _wire_sample if self.wire else _sweep_sample
        step = max(1, self.rule.size // 64)
        for y in range(0, self.rule.size, step):
            for x in range(0, self.rule.size, step):
                at = (y * self.rule.size + x) * 4
                if self.wire and paired_frame is not None and max(abs(frame[at + c] - paired_frame[at + c]) for c in range(3)) > 8:
                    continue
                sample = sample_pixel(frame, at, self.rule.layer, phases[y * self.rule.size + x])
                if sample is None:
                    continue
                color, strength = sample
                # Colorful sweep light wins over neutral material brightening;
                # white remains a valid dominant sweep for neutral recipes.
                chroma = (max(color) - min(color)) / 255
                weight = strength * (.05 + chroma)
                key = tuple(c // 16 for c in color)
                record = self.bins.setdefault(key, [0., 0., 0., 0.])
                record[0] += weight
                for channel in range(3):
                    record[channel + 1] += color[channel] * weight
        if not self.bins:
            target = WIRE if self.wire else PRINT_HEAD
            candidates = range(len(phases)) if self.wire else (phases.find(bytes([target])),)
            if self.wire and all(max(frame[channel::4]) < 12 for channel in range(3)):
                return
            for pixel in candidates:
                if pixel < 0 or phases[pixel] != target:
                    continue
                at = pixel * 4
                if self.wire and paired_frame is not None and max(abs(frame[at + c] - paired_frame[at + c]) for c in range(3)) > 8:
                    continue
                sample = sample_pixel(frame, pixel * 4, self.rule.layer, target)
                if sample is not None:
                    color, strength = sample
                    self.bins[tuple(c // 16 for c in color)] = [strength, *(c * strength for c in color)]
                    break

    def color(self):
        if self.rule.layer == 'normal':
            return None
        if not self.bins:
            return None
        record = max(self.bins.values(), key=lambda item: item[0])
        return tuple(round(value / record[0]) for value in record[1:])


def validate_sweep_color(color):
    if color is None:
        return None
    if (not isinstance(color, (tuple, list)) or len(color) != 3
            or any(type(c) is not int or not 0 <= c <= 255 for c in color)):
        raise ValueError('颜色必须是 0–255 范围内的 RGB 三元组。')
    return tuple(color)


def recolor_sweep(image, reference, template, color, rule, frame_index, *,
                  cancelled=lambda: False):
    from PIL import Image
    from .sequence_material_vtf import check_cancel
    if color is None or color == template.sweep_color:
        return image
    if rule.layer == 'normal':
        raise ValueError('法线层不支持扫过颜色自定义。')
    output = bytearray(image.tobytes())
    phases = template.phase_frame(frame_index)
    for at in range(0, len(output), 4):
        if at % (rule.size * 4 * 16) == 0:
            check_cancel(cancelled)
        sample = _sweep_sample(reference, at, rule.layer, phases[at // 4])
        if sample is None:
            continue
        rgb = reference[at:at + 3]
        floor = 0 if rule.layer == 'glow' else min(rgb)
        strength = max(rgb) - floor
        for channel in range(3):
            # The front is a chromatic coat over the neutral white shading.
            # White-model pixels never reach this branch; keep the coat's
            # neutral floor, brightness profile and exact authored alpha.
            output[at + channel] = max(0, min(255, round(floor + color[channel] * strength / 255)))
    return Image.frombytes('RGBA', image.size, bytes(output))


def recolor_wire(image, reference, template, color, rule, frame_index, *, cancelled=lambda: False):
    from PIL import Image
    from .sequence_material_vtf import check_cancel
    if color is None or color == template.wire_color:
        return image
    if rule.layer == 'normal':
        raise ValueError('法线层不支持线框色彩自定义。')
    output = bytearray(image.tobytes())
    phases = template.phase_frame(frame_index)
    for at in range(0, len(output), 4):
        if at % (rule.size * 4 * 16) == 0:
            check_cancel(cancelled)
        sample = _wire_sample(reference, at, rule.layer, phases[at // 4])
        if sample is None:
            continue
        observed = sample[0]
        standard = template.wire_color
        observed_floor, standard_floor = min(observed), min(standard)
        observed_chroma = max(observed) - observed_floor
        standard_chroma = max(standard) - standard_floor
        # WIRE also covers alpha-zero padding. Match the authored wire hue to
        # avoid tinting unrelated hidden material RGB in those padding pixels.
        if not standard_chroma or max(abs((observed[c] - observed_floor) / observed_chroma
                                          - (standard[c] - standard_floor) / standard_chroma)
                                      for c in range(3)) > .35:
            continue
        peak = max(reference[at:at + 3])
        for channel in range(3):
            output[at + channel] = round(peak * color[channel] / 255)
    return Image.frombytes('RGBA', image.size, bytes(output))
