"""Pure Python compiled Source pose evaluation in unmodified model coordinates.

The binary channel decoder is shared with decompilation; sequence mixing and
skinning never pass through SMD coordinates or decompiler correction options.
"""
from __future__ import annotations

import copy
import math
import weakref
from functools import lru_cache

from .core.vectors import SourceVector
from .core.writers.animation import calc_animation, _stored_euler_to_quaternion

IDENTITY_Q = (0.0, 0.0, 0.0, 1.0)
IDENTITY = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)


def vec(v):
    return (v.x, v.y, v.z)


def quat(q):
    values = (q.x, q.y, q.z, q.w)
    size = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(size) or size < 1e-12:
        raise ValueError('Invalid animation quaternion')
    return tuple(x / size for x in values)


def euler(v):
    return quat(_stored_euler_to_quaternion(v))


def qmul(a, b):
    x, y, z, w = a; X, Y, Z, W = b
    return (w*X + x*W + y*Z - z*Y, w*Y - x*Z + y*W + z*X,
            w*Z + x*Y - y*X + z*W, w*W - x*X - y*Y - z*Z)


def slerp(a, b, t):
    dot = sum(x*y for x, y in zip(a, b))
    if dot < 0:
        b = tuple(-x for x in b); dot = -dot
    dot = min(1., max(-1., dot))
    if dot > .9995:
        q = tuple(x + (y-x)*t for x, y in zip(a, b))
        size = math.sqrt(sum(x*x for x in q))
        return tuple(x / size for x in q)
    angle = math.acos(dot)
    u, v = math.sin((1-t)*angle) / math.sin(angle), math.sin(t*angle) / math.sin(angle)
    return tuple(x*u+y*v for x, y in zip(a, b))


def quaternion_blend(a, b, t):
    """Source QuaternionBlend: align hemispheres, linearly blend, normalize.

    Used by CalcBoneQuaternion/BlendBones, unlike the spherical interpolation
    used by SlerpBones when accumulating separate sequences.
    """
    if dot(a, b) < 0:
        b = scale(b, -1.)
    return unit(lerp(a, b, t))


def quaternion_scale(rotation, factor):
    """Source QuaternionScale, including its unnormalized-input semantics."""
    sine = min(math.sqrt(dot(rotation[:3], rotation[:3])), 1.)
    scaled_sine = math.sin(math.asin(sine)*factor)
    vector = scale(rotation[:3], scaled_sine/(sine+2**-23))  # FLT_EPSILON
    scalar = math.sqrt(max(0., 1-scaled_sine*scaled_sine))
    return (*vector, -scalar if rotation[3] < 0 else scalar)


def lerp(a, b, t):
    return tuple(x+(y-x)*t for x, y in zip(a, b))


def add(a, b):
    return tuple(x+y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def scale(a, s):
    return tuple(x*s for x in a)


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def unit(a):
    length = math.sqrt(dot(a, a))
    if length < 1e-10:
        raise ValueError('Degenerate IK direction')
    return scale(a, 1/length)


def conjugate(q):
    return (-q[0], -q[1], -q[2], q[3])


def rotate(q, v):
    return qmul(qmul(q, (*v, 0.)), conjugate(q))[:3]


def from_to(a, b):
    a, b = unit(a), unit(b)
    cross = (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    q = (*cross, 1+dot(a, b))
    if dot(q, q) < 1e-12:
        helper = (1., 0., 0.) if abs(a[0]) < .8 else (0., 1., 0.)
        axis = unit(sub(helper, scale(a, dot(a, helper))))
        return (*axis, 0.)
    return unit(q)


def hierarchy_order(bones):
    """Validate parent links iteratively, without a frame-retaining closure."""
    done=set();order=[]
    for start in range(len(bones)):
        chain=[];visiting=set();index=start
        while index>=0 and index not in done:
            if index>=len(bones) or index in visiting:
                raise ValueError('Invalid/cyclic bone hierarchy')
            visiting.add(index);chain.append(index)
            index=bones[index].parentBoneIndex
        for index in reversed(chain):
            done.add(index);order.append(index)
    return order


def world_transforms(bones, pose):
    result=[None]*len(bones)
    for i in hierarchy_order(bones):
        p,q=pose[i];parent=bones[i].parentBoneIndex
        if parent>=0:
            pp,pq=result[parent];p,q=add(pp,rotate(pq,p)),qmul(pq,q)
        result[i]=(p,q)
    return tuple(result)


def matrix(position, rotation):
    x, y, z, w = rotation
    return (1-2*(y*y+z*z), 2*(x*y+z*w), 2*(x*z-y*w), 0.,
            2*(x*y-z*w), 1-2*(x*x+z*z), 2*(y*z+x*w), 0.,
            2*(x*z+y*w), 2*(y*z-x*w), 1-2*(x*x+y*y), 0.,
            *position, 1.)


def multiply(a, b):
    return tuple(sum(a[k*4+r]*b[c*4+k] for k in range(4))
                 for c in range(4) for r in range(4))


def world_matrices(bones, pose):
    result=[None]*len(bones)
    for i in hierarchy_order(bones):
        m=matrix(*pose[i]);parent=bones[i].parentBoneIndex
        result[i]=multiply(result[parent],m) if parent>=0 else m
    return tuple(result)


def envelope(value, start, peak, tail, end):
    if start == end:
        return 1.
    if value < start or value >= end:
        return 0.
    if value < peak and peak != start:
        return (value-start)/(peak-start)
    if value > tail and end != tail:
        return (end-value)/(end-tail)
    return 1.


class PoseEvaluator:
    def __init__(self, data, warn):
        self.data, self.warn = data, warn
        self.bones = data.theBones or []
        self.sequences = data.theSequenceDescs or []
        self.animations = data.theAnimationDescs or []
        self.bind = tuple((vec(b.position), euler(b.rotation)) for b in self.bones)
        self.zero = tuple(((0., 0., 0.), IDENTITY_Q) for _ in self.bones)
        owner=weakref.ref(self)
        @lru_cache(maxsize=256)
        def decode(index,frame):
            evaluator=owner()
            if evaluator is None:raise RuntimeError('Preview evaluator has been released')
            return evaluator._decode_frame(index,frame)
        self._decode=decode
        for b in self.bones:
            if b.proceduralRuleType:
                warn('Procedural bone effects are not simulated: ' + b.theName)
        if getattr(data, 'theBoneControllers', None):
            warn('Bone controllers use their compiled neutral pose; runtime inputs are unavailable.')
        if data.theModelGroups:
            warn('Included model animation libraries are not merged; only local compiled sequences are previewed.')
        if getattr(data, 'theIkLocks', None):
            warn('Autoplay IK locks require runtime state and are not simulated.')

    def checked_sequence(self, index):
        if not isinstance(index, int) or not 0 <= index < len(self.sequences):
            raise IndexError('Invalid compiled sequence index')
        return self.sequences[index]

    def _animation(self, index):
        if not 0 <= index < len(self.animations):
            raise ValueError('Sequence refers to an unavailable compiled animation')
        return self.animations[index]

    def frame_count(self, index):
        s = self.checked_sequence(index)
        return max((self._animation(i).frameCount for i in s.theAnimDescIndexes or []), default=1)

    def fps(self, index):
        s = self.checked_sequence(index)
        return sum(self._animation(i).fps * w for i, w in self._grid(s))

    def _grid(self, sequence):
        return [(index, wx*wy) for row, wy in self._grid_rows(sequence)
                for index, wx in row if wx*wy > 0]

    def _grid_rows(self, sequence):
        indexes = sequence.theAnimDescIndexes or []
        if not indexes:
            raise ValueError('Sequence has no local compiled animations')
        sizes = [max(1, n) for n in sequence.groupSize]
        if sizes[0] * sizes[1] != len(indexes):
            raise ValueError('Invalid compiled sequence blend grid')
        axes = []
        keys = sequence.thePoseKeys or []
        for axis, size in enumerate(sizes):
            if size == 1:
                axes.append([(0, 1.)]); continue
            # Neutral is physical zero, clamped to the sequence's pose range.
            offset = sizes[0] if axis else 0
            values = keys[offset:offset+size] if sequence.poseKeyOffset else []
            if not values:
                lo, hi = sequence.paramStart[axis], sequence.paramEnd[axis]
                values = [lo + (hi-lo)*i/(size-1) for i in range(size)]
            if len(values) == size and values[-1] < values[0]:
                values = [-v for v in values]
            if len(values) != size or any(values[i] > values[i+1] for i in range(size-1)):
                raise ValueError('Invalid sequence pose keys')
            value = min(values[-1], max(values[0], 0.))
            left = next((i for i in range(size-1) if value <= values[i+1]), size-2)
            span = values[left+1]-values[left]
            t = (value-values[left])/span if span else 0.
            axes.append([(left, 1-t), (left+1, t)])
        return [([(indexes[x+y*sizes[0]], wx) for x, wx in axes[0] if wx > 0], wy)
                for y, wy in axes[1] if wy > 0]

    def _decode_frame(self, index, frame):
        a = self._animation(index)
        if a.flags & 32:
            return self.zero if a.flags & 4 else self.bind
        section, local = 0, frame
        if a.sectionFrameCount:
            if a.frameCount > a.sectionFrameCount and frame == a.frameCount-1:
                section, local = a.frameCount//a.sectionFrameCount+1, 0
            else:
                section, local = divmod(frame, a.sectionFrameCount)
        if a.flags & 64:
            return self._frame_animation(a, section, local)
        sections = a.theSectionsOfAnimations
        if sections is None or section >= len(sections):
            raise ValueError('Compiled animation section is missing: ' + a.theName)
        # calc_animation chooses ordinary sections itself; an isolated descriptor
        # also handles Source's special terminal-frame section correctly.
        selected = copy.copy(a)
        selected.sectionFrameCount = 0
        selected.theSectionsOfAnimations = [sections[section]]
        lines = calc_animation(self.data, None, selected, local)
        return tuple((vec(lines[i].position), euler(lines[i].rotation)) for i in range(len(self.bones)))

    def _frame_animation(self, a, section, frame):
        sections = a.theSectionsOfFrameAnim
        if not sections or section >= len(sections):
            raise ValueError('Compiled frame-animation section is missing')
        block = sections[section]
        flags = block.theBoneFlags
        if len(flags) != len(self.bones):
            raise ValueError('Incomplete frame-animation bone table')
        pose = list(self.zero if a.flags & 4 else self.bind)
        for i, flag in enumerate(flags):
            p, q = pose[i]
            if block.theBoneConstantInfos:
                c = block.theBoneConstantInfos[i]
                if flag & 2: q = quat(c.theConstantRawRot.quaternion)
                if flag & 1: p = vec(c.theConstantRawPos)
                if flag & 0x20: p = vec(c.theConstantPosition2)
                if flag & 0x40: q = quat(c.theConstantRotation2.quaternion)
            if block.theBoneFrameDataInfos:
                f = block.theBoneFrameDataInfos[frame][i]
                if flag & 8: q = quat(f.theAnimRotation.quaternion)
                if flag & 4: p = vec(f.theAnimPosition)
                if flag & 0x10: p = vec(f.theFullAnimPosition)
                if flag & 0x80: q = quat(f.theAnimRotationUnknown.quaternion)
            pose[i] = (p, q)
        return tuple(pose)

    def _sample_animation(self, index, cycle):
        a = self._animation(index)
        frame = max(0., min(float(max(0, a.frameCount-1)), cycle*max(0, a.frameCount-1)))
        first = int(frame); last = min(first+1, max(0, a.frameCount-1))
        p, q = self._decode(index, first), self._decode(index, last)
        t = frame-first
        return tuple((lerp(x[0], y[0], t), quaternion_blend(x[1], y[1], t)) for x, y in zip(p, q))

    def _raw_sequence(self, sequence, cycle):
        def mix(items):
            result = None; total = 0.
            for pose, weight in items:
                if result is None:
                    result = pose
                else:
                    t = weight/(total+weight)
                    result = tuple((lerp(x[0], y[0], t), quaternion_blend(x[1], y[1], t)) for x, y in zip(result, pose))
                total += weight
            return result
        rows = []
        for row, wy in self._grid_rows(sequence):
            samples = []
            for index, wx in row:
                a = self._animation(index)
                if a.localHierarchyCount:
                    self.warn('Local hierarchy animation is not simulated: ' + a.theName)
                if a.theMovements:
                    self.warn('Extracted root motion is held in place: ' + a.theName)
                samples.append((self._sample_animation(index, cycle), wx))
            rows.append((mix(samples), wy))
        return mix(rows)

    def _blend(self, base, pose, sequence, weight):
        weights = sequence.theBoneWeights
        if weights is not None and len(weights) != len(self.bones):
            raise ValueError('Incomplete sequence bone weight list')
        result = []
        delta = bool(sequence.flags & 4)
        for i, ((p, q), (r, s)) in enumerate(zip(base, pose)):
            w = max(0., min(1., weight*(weights[i] if weights is not None else 1.)))
            if delta:
                p = tuple(x+y*w for x, y in zip(p, r))
                scaled = slerp(IDENTITY_Q, s, w)
                q = qmul(q, scaled) if sequence.flags & 16 else qmul(scaled, q)
            else:
                p, q = lerp(p, r, w), slerp(q, s, w)
            result.append((p, q))
        return tuple(result)

    def _layer(self, base, layer, cycle, parent_weight, stack):
        value = 0. if layer.flags & 0x4000 else cycle
        influence = envelope(value, layer.start, layer.peak, layer.tail, layer.end)
        if influence <= 0:
            return base
        if layer.flags & 0x40:
            influence = influence*influence*(3-2*influence)
        if layer.flags & 0x80 and value > layer.tail:
            weight = influence*parent_weight/(1-parent_weight+influence*parent_weight)
        elif layer.flags & 0x200:
            weight = influence
        else:
            weight = influence*parent_weight
        layer_cycle = cycle
        if layer.start != layer.end and not layer.flags & 0x4000:
            layer_cycle = (cycle-layer.start)/(layer.end-layer.start)
        return self._accumulate(base, layer.iSequence, layer_cycle, weight, stack)

    def _accumulate(self, base, index, cycle, weight, stack):
        if weight <= 0:
            return base
        if index in stack or len(stack) >= 32:
            self.warn('Autolayer cycle/depth limit encountered; cyclic layer omitted.')
            return base
        s = self.checked_sequence(index); stack = (*stack, index)
        if s.theIkLocks:
            self.warn('Sequence IK locks require previous-frame state and are not simulated: ' + s.theName)
        if s.flags & (0x80 | 0x100 | 0x4000):
            self.warn('Cycle-pose/realtime/world-space sequence modifiers are not simulated: ' + s.theName)
        raw = self._raw_sequence(s, cycle)
        layers = s.theAutoLayers or []
        local_request_start = len(self._ik_requests)
        for layer in layers:
            if layer.flags & 0x1000:
                raw = self._layer(raw, layer, cycle, 1., stack)
        # Local layers build the raw sequence at full influence; their IK
        # requests still inherit the outer sequence's accumulation weight.
        self._ik_requests[local_request_start:] = [
            (grid, c, w*weight) for grid, c, w in self._ik_requests[local_request_start:]]
        result = self._blend(base, raw, s, weight)
        grid = tuple((self._animation(i), w) for i, w in self._grid(s))
        if weight > 0 and any(a.theIkRules for a, _ in grid):
            ik_cycle = cycle
            if ik_cycle < 0 or ik_cycle >= 1:
                ik_cycle = ik_cycle % 1 if s.flags & 1 else max(0., min(.9999, ik_cycle))
            # One request per sequence: grid errors mix together first, while
            # independent sequence/autolayer requests retain accumulation order.
            self._ik_requests.append((grid, ik_cycle, min(1., weight)))
        for layer in layers:
            if not layer.flags & 0x1000:
                result = self._layer(result, layer, cycle, weight, stack)
        return result

    def sample(self, index, frame):
        self._ik_requests = []
        s = self.checked_sequence(index)
        if not math.isfinite(frame):
            raise ValueError('Animation frame must be finite')
        length = max(1, self.frame_count(index)-1)
        cycle = max(0., min(1., frame/length))
        if s.flags & 1 and (frame < 0 or frame > length):
            cycle = (frame/length) % 1.
        base = self._delta_base(s) if s.flags & 4 else self.bind
        pose = self._accumulate(base, index, cycle, 1., ())
        return self._apply_ik(pose)

    def _delta_base(self, s, selected=None):
        if selected is not None:
            index, cycle = selected
            self.warn('Delta sequences use the preceding selected absolute idle; gameplay layer weights may differ.')
            return self._accumulate(self.bind, index, cycle, 1., ())
        base = self.bind
        if s.flags & 4:
            from .core.subtract_correction import _is_real_idle_activity
            candidates = [(i, item) for i, item in enumerate(self.sequences)
                          if not item.flags & 4 and _is_real_idle_activity(item.theActivityName)]
            if candidates:
                # ADS1/ADS2/etc. and MIXED state belong to the compiled Activity
                # domain. Sequence/animation labels provide no playback evidence.
                domain, marker, _ = s.theActivityName.upper().partition('_VM_')
                domains = [domain] if marker else []
                if domain.startswith('MIXED_'):
                    domains.append(domain.removeprefix('MIXED_'))
                if 'ACT' not in domains:
                    domains.append('ACT')
                def priority(candidate):
                    i, item = candidate
                    candidate_domain, _, suffix = item.theActivityName.upper().partition('_VM_')
                    rank = domains.index(candidate_domain) if candidate_domain in domains else len(domains)
                    return rank, suffix != 'IDLE', i
                selected, selected_sequence = min(candidates, key=priority)
                if selected_sequence.theActivityName.upper().partition('_VM_')[0] != domain:
                    self.warn('No absolute idle in the requested Activity domain; another compiled idle is used.')
                base = self._accumulate(self.bind, selected, 0., 1., ())
                self.warn('Delta sequences use an absolute idle at frame 0; gameplay-selected base/layer weights may differ.')
            else:
                self.warn('No absolute idle available for delta sequence; bind pose is used as base.')
        return base

    def sample_continuous(self, entries):
        """Accumulate a short sequence history locally, then solve IK once.

        Entries carry (index, cycle, incoming weight, explicit idle base).
        Earlier transitions keep advancing when a short clip starts another
        fade. Absolute incoming weights retain the outgoing value on masked
        bones. Delta entries replace the outgoing contribution over their
        selected absolute base before adding their weighted local delta.
        """
        self._ik_requests = []
        return self._apply_ik(self._continuous_pose(entries))

    def _continuous_pose(self, entries):
        """Build a playlist locally, leaving its ordered IK requests unsolved."""
        pose = self.bind
        for ordinal, (index, cycle, weight, selected) in enumerate(entries):
            sequence = self.checked_sequence(index)
            if not math.isfinite(cycle) or not math.isfinite(weight) or not 0 <= weight <= 1:
                raise ValueError('Invalid continuous pose cycle/weight')
            if ordinal:
                self._ik_requests = [(grid, c, w*(1-weight)) for grid, c, w in self._ik_requests if w*(1-weight) > 0]
            if sequence.flags & 4:
                outgoing_requests = self._ik_requests
                self._ik_requests = []
                base = self._delta_base(sequence, selected)
                base_requests = [(grid, c, w*weight) for grid, c, w in self._ik_requests if w*weight > 0]
                # An explicitly reused idle is the same dependency on both
                # sides of this blend. Merge its influence instead of applying
                # two fractional constraints sequentially (which weakens it).
                for grid, c, w in base_requests:
                    key = tuple((id(a), gw) for a, gw in grid)
                    for i, (old_grid, old_cycle, old_weight) in enumerate(outgoing_requests):
                        if abs(old_cycle-c) < 1e-12 and tuple((id(a), gw) for a, gw in old_grid) == key:
                            outgoing_requests[i] = old_grid, old_cycle, min(1., old_weight+w)
                            break
                    else:
                        outgoing_requests.append((grid, c, w))
                self._ik_requests = outgoing_requests
                pose = (tuple((lerp(a[0], b[0], weight), slerp(a[1], b[1], weight))
                              for a, b in zip(pose, base)) if ordinal else base)
            pose = self._accumulate(pose, index, cycle, weight, ())
        return pose

    @staticmethod
    def _mix_local(first, second, weight):
        if weight <= 0:
            return first
        if weight >= 1:
            return second
        return tuple((lerp(a[0], b[0], weight), slerp(a[1], b[1], weight))
                     for a, b in zip(first, second))

    def sample_layered(self, main_entries, layers):
        """HLMV ordered local accumulation with a single shared IK solve.

        Queue candidates all use the current lower-lane pose. Crossfading
        those candidates replaces this lane's old delta without rebuilding
        an unrelated idle or erasing a lower lane's contribution.
        """
        main_entries = tuple(main_entries)
        self._ik_requests = []
        pose = self._continuous_pose((index, cycle, weight, None)
                                     for index, cycle, weight in main_entries)
        # Retain the main playlist's FK mask/delta behavior, but treat its IK
        # clips as alternatives too. Collect each full candidate separately;
        # this also retains any idle-base/autolayer dependencies of delta donors.
        main_candidates = []
        for index, cycle, weight in main_entries:
            self._ik_requests = []
            self._continuous_pose(((index, cycle, 1., None),))
            main_candidates.append((self._ik_requests, weight))
        ik_lanes = [(main_candidates, 1.)]
        for entries, presence in layers:
            if presence <= 0 or not entries:
                continue
            underlying = pose
            lane_pose = underlying
            candidates = []
            for ordinal, (index, cycle, weight) in enumerate(entries):
                self._ik_requests = []
                candidate = self._accumulate(underlying, index, cycle, 1., ())
                candidate_requests = self._ik_requests
                candidates.append((candidate_requests, weight))
                if ordinal == 0:
                    lane_pose = candidate
                else:
                    lane_pose = self._mix_local(lane_pose, candidate, weight)
            pose = self._mix_local(underlying, lane_pose, presence)
            ik_lanes.append((candidates, presence))
        self._ik_requests = []
        return self._apply_ik(pose, ik_lanes)

    @staticmethod
    def _ik_window(rule):
        start, peak, tail, end = rule.influenceStart, rule.influencePeak, rule.influenceTail, rule.influenceEnd
        # Compiled Source normally extends end beyond 1. Also accept legacy
        # reader fixtures whose trailing limits are folded back into [0, 1].
        if start > end:
            if peak < start: peak += 1
            if tail < start: tail += 1
            end += 1
        return start, peak, tail, end

    @staticmethod
    def _ik_influence(cycle, window):
        start, peak, tail, end = window
        phase = cycle+1 if end > 1 and cycle < start else cycle
        if phase < start or phase >= end:
            value = 0.
        elif phase < peak:
            value = (phase-start)/(peak-start)
        elif phase < tail:
            value = 1.
        else:
            value = (end-phase)/(end-tail) if end != tail else 0.
        return value*value*(3-2*value), phase

    def _ik_sequence_rules(self, grid, cycle):
        """Studio_IKSequenceError: mix each rule slot across the blend grid."""
        from .core.subtract_correction import _strict_ik_value
        counts = [len(a.theIkRules or []) for a, _ in grid]
        if len(set(counts)) != 1:
            self.warn('IK rule counts differ across the sequence blend grid; constraints omitted.')
            return
        for slot in range(counts[0]):
            entries = [(a, a.theIkRules[slot], weight) for a, weight in grid]
            rule = entries[0][1]
            if rule.type not in (1, 4):
                self.warn(f'IK type {rule.type} requires runtime targets and is not simulated.')
                continue
            if any((r.chain, r.bone, r.type) != (rule.chain, rule.bone, rule.type) for _, r, _ in entries):
                self.warn('IK rule targets differ across the sequence blend grid; constraint omitted.')
                continue
            windows = [(self._ik_window(r), w) for _, r, w in entries]
            first_start = windows[0][0][0]
            blended = [0., 0., 0., 0.]
            for window, weight in windows:
                difference = window[0]-first_start
                shift = -1 if difference > .5 else 1 if difference < -.5 else 0
                blended = [a+(b+shift)*weight for a, b in zip(blended, window)]
            shift = -1 if blended[0] > 1 else 1 if blended[0] < 0 else 0
            blended = [v+shift for v in blended]
            influence, _ = self._ik_influence(cycle, blended)
            if influence <= .001:
                continue
            if rule.type == 4:
                yield rule, None, None, influence
                continue
            position = (0., 0., 0.); rotation = (0., 0., 0., 0.); total = 0.
            try:
                for a, r, blend_weight in entries:
                    window = self._ik_window(r)
                    individual_weight, phase = self._ik_influence(cycle, window)
                    if individual_weight < .0001:
                        continue
                    fake_q = (r.q.x, r.q.y, r.q.z, r.q.w)
                    if any(vec(r.pos)) or (any(fake_q) and (any(fake_q[:3]) or abs(fake_q[3]) != 1)):
                        raise ValueError('nonidentity fake touch target offsets are not simulated')
                    # Source: fraq=(numframes-1)*(wrappedCycle-start)+iStart;
                    # compressed data are indexed by floor(fraq)-iStart.
                    frame = (a.frameCount-1)*(phase-window[0])+r.ikErrorIndexStart
                    first = int(frame); fraction = frame-first
                    left = tuple(_strict_ik_value(a, r, j, first, False, a.theName)[0] for j in range(6))
                    right = (tuple(_strict_ik_value(a, r, j, first+1, False, a.theName)[0] for j in range(6))
                             if fraction > .0001 else left)
                    p = lerp(left[:3], right[:3], fraction)
                    q = quaternion_blend(euler(SourceVector(*left[3:])), euler(SourceVector(*right[3:])), fraction)
                    position = add(position, scale(p, blend_weight))
                    # QuaternionAccumulate aligns to the running, unnormalized
                    # sum; normalize once after all active grid references.
                    if dot(rotation, q) < 0: q = scale(q, -1.)
                    rotation = add(rotation, scale(q, blend_weight))
                    total += blend_weight
                if total > .0001:
                    if total < .999:
                        position = scale(position, 1/total)
                        # Studio_IKSequenceError scales the rotation represented
                        # by the unnormalized sum, not its four components.
                        rotation = quaternion_scale(rotation, 1/total)
                    yield rule, position, unit(rotation), influence
            except (ValueError, IndexError, AttributeError, TypeError) as error:
                self.warn('Touch IK curve unavailable; constraint omitted: ' + str(error))

    def _ik_targets(self, requests, world, previous=None):
        """Apply Source dependency order without solving any limb yet."""
        targets = dict(previous) if previous is not None else {}
        chains = self.data.theIkChains or []
        for grid, cycle, sequence_weight in requests:
            for rule, error_p, error_q, influence in self._ik_sequence_rules(grid, cycle):
                if not 0 <= rule.chain < len(chains):
                    self.warn('Touch IK has an invalid chain and was omitted.'); continue
                links = chains[rule.chain].theLinks or []
                ids = tuple(link.boneIndex for link in links)
                if (len(ids) != 3 or any(not 0 <= i < len(self.bones) for i in ids)
                        or self.bones[ids[1]].parentBoneIndex != ids[0]
                        or self.bones[ids[2]].parentBoneIndex != ids[1]
                        or (rule.type == 1 and (not 0 <= rule.bone < len(self.bones) or rule.bone in ids))):
                    self.warn('Touch IK supports three contiguous limb bones and an external target bone; rule omitted.')
                    continue
                weight = influence*sequence_weight
                original_p, original_q = world[ids[-1]]
                old_p, old_q, old_weight, _ = targets.get(rule.chain, (original_p, original_q, 0., ids))
                if weight > .999:
                    # AddDependencies discards earlier rules at full influence.
                    old_p, old_q, old_weight = original_p, original_q, 0.
                    targets.pop(rule.chain, None)
                    if rule.type == 4:
                        continue
                if rule.type == 4:
                    desired_p, desired_q = original_p, original_q
                else:
                    target_p, target_q = world[rule.bone]
                    desired_p = add(target_p, rotate(target_q, error_p))
                    desired_q = qmul(target_q, error_q)
                    old_weight = old_weight*(1-weight)+weight
                    self.warn('Touch IK uses stateless bone-relative limb solving; runtime contact history is unavailable.')
                # SolveDependencies applies requests in sequence order from FK.
                # The resulting target already contains its influence; the limb
                # solver must not interpolate it by that influence a second time.
                targets[rule.chain] = (lerp(old_p, desired_p, weight), slerp(old_q, desired_q, weight), old_weight, ids)
        return targets

    @staticmethod
    def _mix_ik_targets(first, second, weight, world):
        """Mix alternative lane targets; absent/released chains use FK."""
        if weight <= 0:
            return first
        if weight >= 1:
            return second
        targets = {}
        for chain in dict.fromkeys((*first, *second)):
            ids = (first[chain] if chain in first else second[chain])[3]
            original_p, original_q = world[ids[-1]]
            fallback = (original_p, original_q, 0., ids)
            old_p, old_q, old_weight, _ = first.get(chain, fallback)
            new_p, new_q, new_weight, _ = second.get(chain, fallback)
            targets[chain] = (lerp(old_p, new_p, weight), slerp(old_q, new_q, weight),
                              old_weight+(new_weight-old_weight)*weight, ids)
        return targets

    def _apply_ik(self, pose, lanes=()):
        """Stateless, bone-relative touch IK for ordinary three-link limbs.

        Main and independent overlay lanes retain Source dependency order.
        Within a queued overlay, clips are alternatives: resolve each against
        the same lower target state, mix their targets, then apply presence.
        Scaling two requests by complementary weights and applying them in
        sequence would weaken the lane and leak the lower target into it.
        Ground/attachment targets and lock history still require engine state.
        """
        chains = self.data.theIkChains or []
        world = world_transforms(self.bones, pose)
        targets = self._ik_targets(self._ik_requests, world)
        for candidates, presence in lanes:
            lane_targets = None
            for requests, weight in candidates:
                candidate = self._ik_targets(requests, world, targets)
                lane_targets = candidate if lane_targets is None else self._mix_ik_targets(
                    lane_targets, candidate, weight, world)
            if lane_targets is not None:
                targets = self._mix_ik_targets(targets, lane_targets, presence, world)
        result = list(pose)
        for chain_index, (desired_p, desired_q, weight, ids) in targets.items():
            if weight <= 0:
                continue
            try:
                current = world_transforms(self.bones, result)
                upper, lower, end = ids
                a, b, c = (current[i][0] for i in ids)
                target = desired_p
                length1, length2 = math.dist(a, b), math.dist(b, c)
                direction = unit(sub(target, a)); distance = math.dist(a, target)
                reachable = min(length1+length2-1e-8, max(abs(length1-length2)+1e-8, distance))
                if abs(reachable-distance) > 1e-4:
                    self.warn('Touch IK target outside limb reach; target clamped without stretching.')
                along = (length1*length1-length2*length2+reachable*reachable)/(2*reachable)
                height = math.sqrt(max(0., length1*length1-along*along))
                bend = sub(sub(b, a), scale(direction, dot(sub(b, a), direction)))
                if dot(bend, bend) < 1e-10:
                    knee = getattr(chains[chain_index].theLinks[0], 'kneeDir', SourceVector())
                    bend = rotate(current[upper][1], vec(knee))
                    bend = sub(bend, scale(direction, dot(bend, direction)))
                    if dot(bend, bend) < 1e-10:
                        helper = (0., 0., 1.) if abs(direction[2]) < .9 else (0., 1., 0.)
                        bend = sub(helper, scale(direction, dot(helper, direction)))
                knee_target = add(add(a, scale(direction, along)), scale(unit(bend), height))
                end_target = add(a, scale(direction, reachable))
                def set_world_rotation(index, rotation, transforms):
                    parent = self.bones[index].parentBoneIndex
                    local = qmul(conjugate(transforms[parent][1]), rotation) if parent >= 0 else rotation
                    result[index] = result[index][0], local
                upper_q = qmul(from_to(sub(b, a), sub(knee_target, a)), current[upper][1])
                set_world_rotation(upper, upper_q, current)
                current = world_transforms(self.bones, result)
                lower_q = qmul(from_to(sub(current[end][0], current[lower][0]), sub(end_target, current[lower][0])), current[lower][1])
                set_world_rotation(lower, lower_q, current)
                current = world_transforms(self.bones, result)
                set_world_rotation(end, desired_q, current)
            except (ValueError, ZeroDivisionError):
                self.warn('Touch IK limb is degenerate; constraint omitted.')
        return tuple(result)
