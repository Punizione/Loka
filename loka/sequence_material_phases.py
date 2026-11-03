"""Extract authored UV-local construction stages from complete VTF timelines."""
from __future__ import annotations

WIRE = 0
WHITE_MODEL = 1
PRINT_HEAD = 2
MATERIAL = 3
PHASE_METHOD = 'uv-wire-white-front-material-v2'


def _neutral_white(r, g, b):
    peak = max(r, g, b)
    return peak >= 80 and peak - min(r, g, b) <= max(12., peak * .12)


def extract_phase_maps(frames, rule, *, cancelled=lambda: False):
    """Classify each UV's time sequence; never use final-frame brightness lift.

    The suffix already settled to its final material is found backwards. In
    the earlier portion, a persistent neutral plateau separates initial wire
    from the later colored printing front. Local white chroma accounts for
    tinted-white model shading and BC compression, including antialiased fronts.
    """
    from .sequence_material_vtf import check_cancel
    pixels = rule.size * rule.size
    if len(frames) != rule.frames or any(len(frame) != pixels * 4 for frame in frames):
        raise ValueError('材质阶段参考帧长度无效。')
    if rule.layer == 'normal':
        return tuple(bytes([MATERIAL]) * pixels for _ in frames)
    phases = [bytearray(pixels) for _ in frames]
    final = frames[-1]
    for pixel in range(pixels):
        if pixel % (rule.size * 8) == 0:
            check_cancel(cancelled)
        at = pixel * 4
        end = final[at:at + 4]
        if rule.layer == 'glow':
            for index, frame in enumerate(frames):
                rgb = frame[at:at + 3]
                if max(rgb) < 12:
                    phase = WIRE
                elif max(abs(rgb[c] - end[c]) for c in range(3)) > 6:
                    phase = PRINT_HEAD
                else:
                    phase = MATERIAL
                phases[index][pixel] = phase
            continue
        settled = len(frames) - 1
        for index in range(len(frames) - 2, -1, -1):
            sample = frames[index][at:at + 4]
            if max(abs(sample[c] - end[c]) for c in range(4)) > 8:
                break
            settled = index
        white_counts = {}
        first_white = None
        for index in range(settled):
            sample = frames[index][at:at + 4]
            if sample[3] and _neutral_white(*sample[:3]):
                if first_white is None:
                    first_white = index
                rgb = tuple(sample[:3])
                white_counts[rgb] = white_counts.get(rgb, 0) + 1
        white = max(white_counts, key=white_counts.get) if white_counts else None
        white_chroma = max(white) - min(white) if white is not None else 0
        end_peak = max(end[:3])
        end_chroma = end_peak - min(end[:3])
        for index, frame in enumerate(frames):
            sample = frame[at:at + 4]
            r, g, b, alpha = sample
            if not alpha:
                phase = WIRE
            elif index >= settled:
                phase = MATERIAL
            elif first_white is not None and index >= first_white:
                peak = max(r, g, b)
                chroma = peak - min(r, g, b)
                material_like = (end_chroma > 24 and peak > 0 and end_peak > 0
                                 and max(abs(sample[c] / peak - end[c] / end_peak)
                                         for c in range(3)) < .08)
                # White-model shading is a neutral color range, not an exact
                # RGB match to one modal sample. Protect low-chroma transitions
                # as white too; never recolor a compressed/tinted white plateau.
                white_like = _neutral_white(r, g, b)
                if white_like:
                    phase = WHITE_MODEL
                elif material_like:
                    phase = MATERIAL
                elif peak >= 64 and chroma > white_chroma + 4:
                    phase = PRINT_HEAD
                else:
                    phase = WHITE_MODEL
            elif max(abs(sample[c] - end[c]) for c in range(4)) <= 8:
                phase = MATERIAL
            else:
                phase = WIRE
            phases[index][pixel] = phase
    return tuple(bytes(phase) for phase in phases)
