"""Offline palette measurements and shared runtime RGB validation."""
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
