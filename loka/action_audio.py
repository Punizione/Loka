"""Read-only sound lookup and event timing for the current compiled viewmodel."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import struct

from .action_materials import _case_path, _game_roots, _validate_resolved_target
from .material_text import encoding, material_pairs
from .vpk import safe_archive_path

MAX_WAVE_BYTES = 32*1024*1024
MAX_BANK_BYTES = 256*1024*1024


@dataclass(frozen=True)
class SoundCue:
    cycle: float
    name: str


def sequence_sound_cues(sequence):
    cues = []
    for event in sequence.theEvents or ():
        # Numeric client/server legacy sound events and compiled named events.
        name = str(event.theName).lower()
        if name not in ('ae_cl_playsound', 'ae_sv_playsound'):
            if event.eventType & event.NEW_EVENT_STYLE or event.eventIndex not in (4, 5004):
                continue
        cycle = float(event.cycle)
        sound = event.options.split('\0', 1)[0].strip().strip('"')
        if math.isfinite(cycle) and 0 <= cycle <= 1 and sound:
            cues.append(SoundCue(cycle, sound))
    return tuple(sorted(cues, key=lambda cue: cue.cycle))


def due_cues(cues, previous, current, duration, *, loop=False, include_start=False):
    """Crossings on the current model's clock, independent of official timing."""
    if not all(math.isfinite(v) for v in (previous, current, duration)):
        return ()
    if duration <= 0 or current < previous or previous < 0:
        return ()
    first = max(0, int(previous/duration)-1) if loop else 0
    last = int(current/duration) if loop else 0
    # Avoid a suspended application producing an unbounded backlog of sounds.
    first = max(first, last-8)
    crossings = []
    for turn in range(first, last+1):
        for order, cue in enumerate(cues):
            at = (turn+cue.cycle)*duration
            if previous < at <= current or include_start and at == previous == current:
                crossings.append((at, -cue.cycle, order, cue))
    crossings.sort(key=lambda item: item[:3])  # end event precedes next loop's frame 0
    return tuple(item[3] for item in crossings[:256])


@dataclass(frozen=True)
class WaveClip:
    source: str
    format: bytes
    samples: bytes
    rate: int
    channels: int
    bits: int

    @classmethod
    def from_bytes(cls, raw, source):
        from .wav_normalize import _prepare_wave
        if len(raw) > MAX_WAVE_BYTES:
            raise ValueError('音频超过预览大小限制')
        raw, parsed, _ = _prepare_wave(raw)
        fmt = None
        position = 12
        while position+8 <= len(raw):
            kind = raw[position:position+4]
            size = struct.unpack_from('<I', raw, position+4)[0]
            if position+8+size > len(raw):
                raise ValueError('音频数据不完整')
            if kind == b'fmt ':
                fmt = raw[position+8:position+8+size]
                break
            position += 8+size+(size & 1)
        if fmt is None or len(fmt) < 16:
            raise ValueError('音频缺少格式信息')
        _, channels, rate, _, _, _, samples, _ = parsed
        bits = struct.unpack_from('<H', fmt, 14)[0]
        # WAVEFORMATEX includes cbSize; plain PCM fmt usually has only 16 bytes.
        if len(fmt) == 16:
            fmt += b'\0\0'
        return cls(str(source), fmt, samples, rate, channels, bits)


def _wave_path(value):
    value = value.replace('\\', '/').lstrip(')*#@^><!?')
    value = re.sub('/+', '/', value)
    if not value.lower().endswith('.wav'):
        return None
    if not value.lower().startswith('sound/'):
        value = 'sound/'+value
    return safe_archive_path(value).lower()


class _Layer:
    def __init__(self, path, *, official=False):
        self.path = Path(path).resolve()
        self.official = official

    def read(self, key):
        limit = MAX_WAVE_BYTES if key.endswith('.wav') else 4*1024*1024
        if self.path.is_dir():
            target = _case_path(self.path, key, exclude_addons=self.official)
            if target is not None:
                if target.stat().st_size > limit:
                    raise ValueError('音频资源超过大小限制: '+key)
                return target.read_bytes()
        return None

    def scripts(self):
        names = set()
        if self.path.is_dir():
            for prefix in ('scripts', 'l4n/scripts/sound'):
                folder = self.path/prefix
                if folder.is_dir():
                    _validate_resolved_target(self.path, folder.resolve(), exclude_addons=self.official)
                    for directory, directories, files in os.walk(folder, followlinks=False):
                        directories[:] = [name for name in directories if name.lower() not in
                            ('talker', 'vscripts', 'vscript', 'addons') and
                            (Path(directory)/name).resolve().is_relative_to(self.path)]
                        for name in files:
                            path = Path(directory)/name
                            if path.suffix.lower()=='.txt' and path.resolve().is_relative_to(self.path):
                                names.add(path.relative_to(self.path).as_posix().lower())
                            if len(names) > 4096:
                                raise ValueError('音频脚本数量超过预览限制')
        if self.official:
            names = {key for key in names if 'sound' in key.rsplit('/', 1)[-1]}
        return sorted(names)


class SoundResolver:
    def __init__(self, local_roots, l4n_paths=(), game_path='', *, cancelled=None, requested_names=None):
        self.layers = [_Layer(p) for p in dict.fromkeys(Path(p).resolve() for p in (*local_roots, *l4n_paths)) if p.is_dir()]
        self.layers.extend(_Layer(p, official=True) for p in _game_roots(game_path) if p.is_dir())
        self.cancelled = cancelled
        self.notes = []
        self._scripts = {}
        self._named_paths = {}
        self._requested_names = {name.lower() for name in requested_names} if requested_names is not None else None
        self._clips = {}
        self._bytes = 0

    def _check(self):
        if self.cancelled and self.cancelled():
            raise InterruptedError('音效载入已取消')

    def _layer_scripts(self,layer):
        if layer in self._scripts:
            return self._scripts[layer]
        scripts = {}
        try:
            names = layer.scripts()
        except (OSError, ValueError) as error:
            self.notes.append(str(error));names=()
        for key in names:
            self._check()
            try:
                raw = layer.read(key)
                if raw is None:continue
                if self._requested_names is not None:
                    text=raw.decode(encoding(raw)).lower()
                    if not any(name in text for name in self._requested_names):continue
                pairs, _ = material_pairs(raw)
                grouped = {}
                for field, value, context in pairs:
                    if context and field.value.lower() == 'wave':
                        try:path = _wave_path(value.value)
                        except ValueError:continue
                        if path:
                            grouped.setdefault(context[0], []).append(path)
                for name, paths in grouped.items():
                    scripts.setdefault(name, tuple(dict.fromkeys(paths)))
                if self._requested_names and self._requested_names <= scripts.keys():break
            except (OSError, ValueError):
                # Other .txt files under scripts/ need not be sound scripts.
                continue
        self._scripts[layer] = scripts
        return scripts

    def _named(self,name):
        name=name.lower()
        if name in self._named_paths:return self._named_paths[name]
        for layer in self.layers:
            if layer.official:continue
            paths=self._layer_scripts(layer).get(name)
            if paths:
                self._named_paths[name]=paths;return paths
        # Known official events already have verified paths bundled with Loka.
        # They need neither official script discovery nor VPK access.
        from .weapon_audio import sound_reference
        record=sound_reference().events.get(name)
        if record is not None:
            self._named_paths[name]=record.paths;return record.paths
        for layer in self.layers:
            if not layer.official:continue
            paths=self._layer_scripts(layer).get(name)
            if paths:
                self._named_paths[name]=paths;return paths
        self._named_paths[name]=();return ()

    def resolve(self, name):
        self._check()
        try:
            direct = _wave_path(name)
            paths = (direct,) if direct else self._named(name)
        except ValueError:
            return ()
        result = []
        for key in paths:
            self._check()
            if key not in self._clips:
                clip = None
                for layer in self.layers:
                    try:
                        raw = layer.read(key)
                        if raw is None:
                            continue
                        if self._bytes+len(raw) > MAX_BANK_BYTES:
                            raise ValueError('音效缓存超过预览大小限制')
                        clip = WaveClip.from_bytes(raw, str(layer.path)+'/'+key)
                        self._bytes += len(raw)
                    except (OSError, ValueError) as error:
                        self.notes.append(f'{key}: {error}')
                    # An existing but unsupported/corrupt replacement must not
                    # silently substitute the lower-priority game's sound.
                    break
                self._clips[key] = clip
            if self._clips[key] is not None:
                result.append(self._clips[key])
        return tuple(result)


@dataclass(frozen=True)
class SoundBank:
    cues: dict
    clips: dict
    notes: tuple


def _matching_l4n(path, identifiers):
    markers = tuple(key for identifier in identifiers for key in (
        'l4n/scripts/sound/game_sounds_'+identifier+'.txt',
        'scripts/necola_sound_'+identifier+'.txt'))
    return path.is_dir() and any(_case_path(path, key) is not None for key in markers)


def load_sound_bank(session, previews, *, cancelled=None):
    cues, clips, notes = {}, {}, []
    extra = []
    if session.work_dir:
        extra.append(Path(session.work_dir)/'l4n/pack')
    for model_index, (model, preview) in enumerate(zip(session.models, previews)):
        local = [model.model_path.parent, Path(session.pack_dir)]
        original = getattr(session, '_original_model', None)
        if original is not None:
            from .action_standalone import _pack_root
            local.extend((Path(original).parent, _pack_root(Path(original))))
        local = tuple(dict.fromkeys(local))
        sidecars = list(extra)
        model_cues = {action.index: sequence_sound_cues(preview.data.theSequenceDescs[action.index])
                      for action in model.swap_actions}
        identifiers = {match[1].lower() for values in model_cues.values() for cue in values
                       if (match := re.fullmatch(r'loka\.([a-z0-9]{8})\.\d+', cue.name, re.I))}
        # Discover companions through the ID actually referenced by this MDL.
        # Merely sharing a parent folder does not make another addon its L4N.
        if getattr(session, 'standalone', False):
            candidates = [root.parent/'l4n/pack' for root in local]
            for folder in dict.fromkeys((*local, *(root.parent for root in local))):
                if folder.is_dir():
                    candidates.extend(sorted(p for p in folder.glob('*_L4N音频') if p.is_dir())[:64])
            for path in dict.fromkeys(p for p in candidates if p.is_dir()):
                if cancelled and cancelled():raise InterruptedError('音效载入已取消')
                try:
                    if _matching_l4n(path, identifiers):sidecars.append(path)
                except (OSError, ValueError) as error:
                    notes.append(f'{path.name}: {error}')
        resolver = SoundResolver(local, tuple(p for p in sidecars if p.is_dir()),
                                 session.options.game_path, cancelled=cancelled,
                                 requested_names={cue.name for values in model_cues.values() for cue in values
                                                  if not cue.name.lower().endswith('.wav')})
        for action in model.swap_actions:
            sound_cues = model_cues[action.index]
            cues[model_index, action.index] = sound_cues
            for cue in sound_cues:
                key = (model_index, cue.name)
                if key not in clips:
                    clips[key] = resolver.resolve(cue.name)
        notes.extend(resolver.notes)
    return SoundBank(cues, clips, tuple(dict.fromkeys(notes)))
