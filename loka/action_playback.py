"""Deterministic, read-only compiled sequence playlists.

This clock models a short explicitly selected sequence history. It has no
gameplay state: activities, procedural effects and contact history remain
subject to the ordinary compiled preview's limitations.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class PlaybackClip:
    sequence_index: int
    frame_count: int
    fps: float
    duration: float
    start: float


@dataclass(frozen=True)
class PlaybackState:
    clip_index: int
    frame: float
    elapsed: float
    transition_weight: float
    transition_duration: float


class ContinuousPlayback:
    """Play one cycle each of one to three sequences, with compiled fades.

    Boundaries start the incoming cycle at zero. Outgoing non-looping clips
    hold their last frame; compiled looping clips continue cycling while
    fading. Looping the playlist restarts its clock without a wrap crossfade.
    A static entry occupies one second, independent of its FPS metadata.
    """
    def __init__(self, model, sequence_indexes, rate_scales=None):
        indexes = tuple(sequence_indexes)
        if not 1 <= len(indexes) <= 3:
            raise ValueError('Continuous playback requires one to three sequences')
        rates = (1.,)*len(indexes) if rate_scales is None else tuple(rate_scales)
        if len(rates) != len(indexes):
            raise ValueError('Every playback entry needs one rate scale')
        sequences = model.data.theSequenceDescs or []
        clips = []; start = 0.
        for index, rate in zip(indexes, rates):
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(sequences):
                raise IndexError('Invalid compiled sequence index')
            timeline = model.timeline(index, rate)
            duration = timeline.duration if timeline.frame_count > 1 else 1.
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError('Moving playback entries require a finite positive duration')
            clips.append(PlaybackClip(index, timeline.frame_count, timeline.fps, duration, start))
            start += duration
        self.model = model
        self.clips = tuple(clips)
        self.duration = start
        transitions = [0.]
        for previous, incoming in zip(clips, clips[1:]):
            old, new = sequences[previous.sequence_index], sequences[incoming.sequence_index]
            # CSequenceTransitioner::CheckForSequenceChange uses the shorter
            # compiled fade, and STUDIO_SNAP on the incoming sequence cuts.
            fade = min(float(old.fadeOutTime), float(new.fadeInTime))
            transitions.append(fade if math.isfinite(fade) and fade > 0 and not new.flags & 0x20 else 0.)
        self._transitions = tuple(transitions)
        from .core.subtract_correction import _is_real_idle_activity
        bases = []; idle = None
        for ordinal, clip in enumerate(clips):
            bases.append(idle)
            sequence = sequences[clip.sequence_index]
            if not sequence.flags & 4 and _is_real_idle_activity(sequence.theActivityName):
                idle = ordinal
        self._idle_bases = tuple(bases)

    def state(self, seconds, loop=False):
        elapsed = float(seconds)
        if not math.isfinite(elapsed):
            raise ValueError('Playback time must be finite')
        elapsed = elapsed % self.duration if loop else max(0., min(self.duration, elapsed))
        ordinal = next((i-1 for i, clip in enumerate(self.clips) if clip.start > elapsed), len(self.clips)-1)
        clip = self.clips[ordinal]
        local = max(0., min(clip.duration, elapsed-clip.start))
        frame = local/clip.duration*max(0, clip.frame_count-1)
        fade = self._transitions[ordinal]
        weight = min(1., local/fade) if fade else 1.
        return PlaybackState(ordinal, frame, elapsed, weight, fade)

    def _cycle(self, ordinal, seconds):
        clip = self.clips[ordinal]
        if clip.frame_count <= 1:
            return 0.
        local = max(0., seconds-clip.start)
        sequence = self.model.data.theSequenceDescs[clip.sequence_index]
        # Keep the exact end of the cycle at the entry's own final boundary;
        # continue looping only once it has become an outgoing participant.
        if sequence.flags & 1 and local > clip.duration:
            return (local/clip.duration) % 1.
        return min(1., local/clip.duration)

    def sample(self, seconds, loop=False):
        state = self.state(seconds, loop)
        entries = []
        for ordinal, clip in enumerate(self.clips[:state.clip_index+1]):
            fade = self._transitions[ordinal]
            weight = min(1., max(0., state.elapsed-clip.start)/fade) if fade else 1.
            base_ordinal = self._idle_bases[ordinal]
            base = None if base_ordinal is None else (
                self.clips[base_ordinal].sequence_index, self._cycle(base_ordinal, state.elapsed))
            entries.append((clip.sequence_index, self._cycle(ordinal, state.elapsed), weight, base))
        return self.model.sample_continuous(entries)


def is_idle_sequence(sequence, profile_id=''):
    """Classify the compiled Activity, including ADS/MIXED idle domains."""
    from .core.subtract_correction import _is_real_idle_activity
    return _is_real_idle_activity(str(getattr(sequence, 'theActivityName', '')))


def is_shove_sequence(sequence, profile_id=''):
    """Weapon melee Activities mean shove; HIT Activities remain attacks."""
    from .action_rules import _base_activity
    from .weapon_catalog import MELEE_IDS
    activity = _base_activity(str(getattr(sequence, 'theActivityName', '')).upper()).removesuffix('_LAYER')
    return activity == 'ACT_VM_MELEE' or (profile_id in MELEE_IDS and activity == 'ACT_VM_SECONDARYATTACK')


@dataclass(frozen=True)
class LayerState:
    active: bool
    clip_index: int | None
    frame: float
    weight: float
    transition_weight: float
    transition_duration: float


@dataclass(frozen=True)
class LayeredPlaybackState:
    elapsed: float
    main: PlaybackState
    layers: tuple[LayerState, LayerState]


def _fade_time(value):
    value = float(value)
    return value if math.isfinite(value) and value > 0 else 0.


class LayeredPlayback:
    """m_nSequence plus two independently timed m_nLayerSequence lanes.

    Each lane queues up to three complete cycles. The final base idle keeps
    cycling while overlays finish. Overlay fades return to the evolving lower
    local pose, and completed lanes contribute neither pose nor IK requests.
    Like HLMV, accumulation order is main, layer 0, layer 1 with shared IK.
    """
    def __init__(self, model, main_indexes, layer0_indexes=(), layer1_indexes=(), *,
                 rate_scales=None, layer_delays=(0., 0.), profile_id='', semantic_indexes=None):
        indexes = tuple(tuple(lane) for lane in (main_indexes, layer0_indexes, layer1_indexes))
        rates = (None, None, None) if rate_scales is None else tuple(rate_scales)
        if len(rates) != 3:
            raise ValueError('Layered playback needs rate scales for three lanes')
        delays = tuple(float(value) for value in layer_delays)
        if len(delays) != 2 or any(not math.isfinite(value) or value < 0 for value in delays):
            raise ValueError('Layer delays must be two finite nonnegative seconds')
        self.model = model
        self.main = ContinuousPlayback(model, indexes[0], rates[0])
        layers = []
        for lane, lane_rates in zip(indexes[1:], rates[1:]):
            if len(lane) > 3:
                raise ValueError('Every overlay supports at most three sequences')
            if not lane and lane_rates is not None and tuple(lane_rates):
                raise ValueError('Every playback entry needs one rate scale')
            layers.append(ContinuousPlayback(model, lane, lane_rates) if lane else None)
        self.layer_tracks = tuple(layers)
        self.layer_delays = delays
        sequences = model.data.theSequenceDescs or []
        # Draft swaps preserve the destination Activity while sampling a donor's
        # compiled animation. The caller may supply those destination identities.
        semantics = indexes if semantic_indexes is None else tuple(tuple(lane) for lane in semantic_indexes)
        if len(semantics) != 3 or any(len(a) != len(b) for a, b in zip(indexes, semantics)):
            raise ValueError('Semantic sequence identities must align with every lane')
        if any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(sequences)
               for lane in semantics for index in lane):
            raise IndexError('Invalid semantic compiled sequence index')
        if any(not is_idle_sequence(sequences[index], profile_id) for index in semantics[0]):
            raise ValueError('The main sequence lane requires idle Activities')
        if any(not is_shove_sequence(sequences[index], profile_id) for index in semantics[2]):
            raise ValueError('Layer 1 requires shove Activities for this weapon profile')
        self._fade_ins = tuple(_fade_time(sequences[track.clips[0].sequence_index].fadeInTime)
                               if track else 0. for track in layers)
        self._fade_outs = tuple(_fade_time(sequences[track.clips[-1].sequence_index].fadeOutTime)
                                if track else 0. for track in layers)
        self.duration = max([self.main.duration, *(
            delay+track.duration+fade for delay, track, fade in zip(delays, layers, self._fade_outs) if track)])

    @staticmethod
    def _clock_state(track, elapsed, *, repeat_final=False):
        state = track.state(elapsed)
        ordinal = state.clip_index
        clip = track.clips[ordinal]
        local = max(0., elapsed-clip.start)
        cycle = track._cycle(ordinal, elapsed)
        if repeat_final and ordinal == len(track.clips)-1 and local > clip.duration:
            cycle = (local/clip.duration) % 1. if clip.frame_count > 1 else 0.
        fade = track._transitions[ordinal]
        return PlaybackState(ordinal, cycle*max(0, clip.frame_count-1), elapsed,
                             min(1., local/fade) if fade else 1., fade)

    def state(self, seconds, loop=False):
        elapsed = float(seconds)
        if not math.isfinite(elapsed):
            raise ValueError('Playback time must be finite')
        elapsed = elapsed % self.duration if loop else max(0., min(self.duration, elapsed))
        main = self._clock_state(self.main, elapsed, repeat_final=True)
        layers = []
        for track, delay, fade_in, fade_out in zip(self.layer_tracks, self.layer_delays, self._fade_ins, self._fade_outs):
            local = elapsed-delay
            if track is None or local < 0 or local >= track.duration+fade_out:
                layers.append(LayerState(False, None, 0., 0., 0., 0.))
                continue
            state = self._clock_state(track, local)
            if local >= track.duration:
                state = PlaybackState(state.clip_index, max(0, track.clips[-1].frame_count-1),
                                      state.elapsed, state.transition_weight, state.transition_duration)
            presence = min(1., local/fade_in) if fade_in else 1.
            if local >= track.duration:
                presence *= max(0., 1-(local-track.duration)/fade_out) if fade_out else 0.
            layers.append(LayerState(True, state.clip_index, state.frame, presence,
                                     state.transition_weight, state.transition_duration))
        return LayeredPlaybackState(elapsed, main, tuple(layers))

    def _entries(self, track, elapsed, state, *, main=False):
        entries = []
        for ordinal, clip in enumerate(track.clips[:state.clip_index+1]):
            cycle = track._cycle(ordinal, elapsed)
            if main and ordinal == len(track.clips)-1:
                cycle = state.frame/max(1, clip.frame_count-1)
            # The final overlay holds its last frame during its fade-out.
            if not main and ordinal == len(track.clips)-1 and elapsed >= track.duration:
                cycle = 1. if clip.frame_count > 1 else 0.
            fade = track._transitions[ordinal]
            weight = min(1., max(0., elapsed-clip.start)/fade) if fade else 1.
            entries.append((clip.sequence_index, cycle, weight))
        return entries

    def sample(self, seconds, loop=False):
        state = self.state(seconds, loop)
        main = self._entries(self.main, state.elapsed, state.main, main=True)
        layers = []
        for track, delay, layer in zip(self.layer_tracks, self.layer_delays, state.layers):
            entries = self._entries(track, state.elapsed-delay, layer) if layer.active else ()
            layers.append((entries, layer.weight))
        return self.model.sample_layered(main, layers)
