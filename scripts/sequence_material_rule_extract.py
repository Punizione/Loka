"""Offline analysis helpers. NumPy is used only by the developer extractor."""
from __future__ import annotations

import itertools

import numpy as np

from loka.sequence_material_phases import WIRE, WHITE_MODEL, PRINT_HEAD
from loka.sequence_material_rules import (EFFECT, FIELD_NAMES,
                                          GAIN_STEPS, LIGHT_STEPS, SHARED_MATERIAL, WIRE_COLOR)


def _palette():
    colors = [(0, 0, 0)] + sorted({rgb for rgb in itertools.product(range(32), repeat=3)
                                   if min(rgb) == 0 and max(rgb) == 31})
    lookup = np.zeros(32 ** 3, dtype=np.uint8)
    for index, (r, g, b) in enumerate(colors):
        lookup[r * 1024 + g * 32 + b] = index
    return tuple(tuple(round(c * 255 / 31) for c in rgb) for rgb in colors), lookup


def _coefficients(baseline, reference, fallback):
    """Measure black/input/white transfer, including saturated-source fallback."""
    delta = np.where(reference < baseline,
                     -(baseline - reference) / np.maximum(baseline, 1),
                     np.where(reference > baseline,
                              (reference - baseline) / np.maximum(255 - baseline, 1),
                              np.where(baseline == 0, fallback - 1,
                                       np.where(baseline == 255, 1 - fallback, 0))))
    return np.rint(np.clip(delta, -1, 1) * GAIN_STEPS + GAIN_STEPS).astype(np.uint8)


def _apply_coefficients(values, codes):
    values, codes = values.astype(np.int32), codes.astype(np.int32)
    delta = codes - GAIN_STEPS
    return np.where(delta <= 0,
                    (values * (GAIN_STEPS + delta) + GAIN_STEPS // 2) // GAIN_STEPS,
                    (values * (GAIN_STEPS - delta) + 255 * delta + GAIN_STEPS // 2) // GAIN_STEPS)


def _shared_material_curve(reference, final, normal):
    """Fit one light/normal-build coefficient; retain exceptions separately."""
    if normal:
        neutral = np.array([128,128,255],dtype=np.float32)
        ray = final - neutral
        gain = np.clip(((reference-neutral)*ray).sum(axis=1) / np.maximum((ray*ray).sum(axis=1),1),0,1)
        code = np.rint(gain*LIGHT_STEPS).astype(np.uint8)
        fit = (neutral.astype(np.int32)*(LIGHT_STEPS-code[:,None])
               + final.astype(np.int32)*code[:,None] + LIGHT_STEPS//2)//LIGHT_STEPS
    else:
        dark_gain = np.clip((reference*final).sum(axis=1) / np.maximum((final*final).sum(axis=1),1),0,1)
        light = 255-final
        light_gain = np.clip(((255-reference)*light).sum(axis=1) / np.maximum((light*light).sum(axis=1),1),0,1)
        dark_code = np.rint(dark_gain*GAIN_STEPS).astype(np.uint8)
        light_code = np.rint((2-light_gain)*GAIN_STEPS).astype(np.uint8)
        dark = _apply_coefficients(final,dark_code[:,None])
        bright = _apply_coefficients(final,light_code[:,None])
        choose_dark = ((reference-dark)**2).sum(axis=1) <= ((reference-bright)**2).sum(axis=1)
        code = np.where(choose_dark,dark_code,light_code).astype(np.uint8)
        fit = np.where(choose_dark[:,None],dark,bright)
    return code,np.abs(fit-reference).max(axis=1)


def analyze_parameters(baseline, frames, phases, rule, wire_color):
    """Extract only temporal operators, effect hue/intensity and visibility."""
    pixels = rule.size ** 2
    base = np.frombuffer(baseline, dtype=np.uint8).reshape(pixels, 4).astype(np.float32)
    animation = np.frombuffer(b''.join(frames), dtype=np.uint8).reshape(rule.frames, pixels, 4)
    stage_maps = np.frombuffer(b''.join(phases), dtype=np.uint8).reshape(rule.frames, pixels)
    peak_alpha = np.maximum(animation[:, :, 3].max(axis=0), 1)
    peak_rgb = np.maximum(animation[:, :, :3].max(axis=(0, 2)), 1)
    palette, hue_lookup = _palette()
    fields = [[] for _ in FIELD_NAMES]
    final_rgb = animation[-1,:,:3].astype(np.float32)
    final_measured = np.where(final_rgb < base[:,:3],final_rgb/np.maximum(base[:,:3],1),
                             np.where(final_rgb > base[:,:3],(255-final_rgb)/np.maximum(255-base[:,:3],1),
                                      np.where((base[:,:3]>0)&(base[:,:3]<255),1,np.inf)))
    final_fallback = final_measured.min(axis=1)
    final_gate = (final_rgb.max(axis=1)/peak_rgb if rule.layer=='glow' else animation[-1,:,3]/peak_alpha)
    final_fallback = np.where(np.isfinite(final_fallback),final_fallback,final_gate)
    calibration = _coefficients(base[:,:3],final_rgb,final_fallback[:,None])
    calibrated = _apply_coefficients(base[:,:3],calibration).astype(np.float32)
    maximum_shared_error = np.zeros(pixels,dtype=np.float32)
    has_input = np.zeros(pixels,dtype=bool)
    for frame_index in range(rule.frames):
        reference = animation[frame_index].astype(np.float32)
        phase = stage_maps[frame_index]
        rgb = reference[:, :3]
        floor = rgb.min(axis=1)
        peak = rgb.max(axis=1)
        strength = peak - floor
        chromaticity = (rgb - floor[:, None]) / np.maximum(strength[:, None], 1)
        wire = np.zeros(pixels, dtype=bool)
        if wire_color is not None and rule.layer not in ('normal', 'glow'):
            standard = np.array(wire_color, dtype=np.float32)
            standard_strength = standard.max() - standard.min()
            if standard_strength > 0:
                standard = (standard - standard.min()) / standard_strength
                wire = ((phase == WIRE) & (peak >= 12)
                        & (strength >= np.maximum(8, peak * .08))
                        & (np.abs(chromaticity - standard).max(axis=1) <= .35))
        effect = ((phase == WHITE_MODEL) | (phase == PRINT_HEAD)
                  | ((phase == WIRE) & ((reference[:, 3] > 0) | wire)))
        if rule.layer == 'normal':
            effect[:] = False
        has_input |= ~effect
        modes = phase | (effect.astype(np.uint8) * EFFECT) | (wire.astype(np.uint8) * WIRE_COLOR)
        gate = (peak / peak_rgb if rule.layer == 'glow' else reference[:, 3] / peak_alpha)
        measured = np.where(rgb < base[:, :3], rgb / np.maximum(base[:, :3], 1),
                            np.where(rgb > base[:, :3], (255 - rgb) / np.maximum(255 - base[:, :3], 1),
                                     np.where((base[:, :3] > 0) & (base[:, :3] < 255), 1, np.inf)))
        fallback = measured.min(axis=1)
        fallback = np.where(np.isfinite(fallback), fallback, gate)
        gain = _coefficients(base[:, :3], rgb, fallback[:, None])
        gain[effect] = GAIN_STEPS
        alpha = _coefficients(base[:, 3], reference[:, 3], gate)
        absolute_alpha = 127 + np.clip(np.rint(reference[:, 3] * 127 / 255), 1, 126).astype(np.uint8)
        alpha[effect] = absolute_alpha[effect]
        alpha[reference[:, 3] == 0] = 254
        alpha[reference[:, 3] == 255] = 255
        if rule.layer == 'glow':
            alpha[:] = 255
        hue = np.rint(chromaticity * 31).astype(np.int32)
        hue_ids = hue_lookup[hue[:, 0] * 1024 + hue[:, 1] * 32 + hue[:, 2]]
        hue_ids[~effect] = 0
        shared_gain, shared_error = _shared_material_curve(rgb,calibrated,rule.layer=='normal')
        maximum_shared_error = np.maximum(maximum_shared_error,np.where(effect,0,shared_error))
        values = [modes, alpha, gain[:, 0], gain[:, 1], gain[:, 2],
                  np.where(effect, np.rint(floor * LIGHT_STEPS / 255), 0).astype(np.uint8),
                  np.where(effect, np.rint(strength * LIGHT_STEPS / 255), 0).astype(np.uint8), hue_ids,
                  shared_gain]
        for output, value in zip(fields, values):
            output.append(value.tobytes())
    shared_pixels = (maximum_shared_error <= 6) & has_input
    # A flat normal or saturated black/white color cannot identify a temporal
    # build coefficient. Keep the independent operators at those UVs so new
    # skin/normal details cannot be erased by an arbitrary zero-gain fit.
    if rule.layer == 'normal':
        shared_pixels &= np.abs(calibrated-np.array([128,128,255])).max(axis=1) >= 4
    else:
        shared_pixels &= (calibrated.max(axis=1)>=8) & ((255-calibrated).max(axis=1)>=8)
    for frame_index in range(rule.frames):
        modes = np.frombuffer(fields[0][frame_index],dtype=np.uint8).copy()
        use_shared = shared_pixels & ((modes & EFFECT)==0)
        modes[use_shared] |= SHARED_MATERIAL
        fields[0][frame_index] = modes.tobytes()
        for channel in range(3):
            gain = np.frombuffer(fields[2+channel][frame_index],dtype=np.uint8).copy()
            gain[shared_pixels] = calibration[shared_pixels,channel]
            unused = has_input & ((modes & EFFECT)!=0)
            gain[unused] = calibration[unused,channel]
            fields[2+channel][frame_index] = gain.tobytes()
        shared = np.frombuffer(fields[-1][frame_index],dtype=np.uint8).copy()
        if frame_index == rule.frames-1:
            shared[:] = LIGHT_STEPS if rule.layer=='normal' else GAIN_STEPS
        shared[~use_shared] = 0
        fields[-1][frame_index] = shared.tobytes()
    return palette, fields
