"""Verified playback roles, factual timing references and isolated QC speed edits.

All operations are read-only: ``retime_qc`` returns a patched document for a
caller-owned staging directory. Callers must restrict edits to ``load_actions``
indexes, since retiming alone has no target profile or runtime context.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import re

from .weapon_catalog import MELEE_IDS, SHOTGUN_IDS
from .weapon_sequences import get_profile
from .fps_values import round_fps, fps_text, scaled_fps


@dataclass(frozen=True)
class ActionInfo:
    index: int
    name: str
    activity: str
    official_index: int | None
    fps: float
    frame_count: int
    smd_frames: tuple[int, ...]
    loop: bool
    display_name: str
    official_duration: float | None
    official_frame_count: int | None
    official_fps: float | None
    timing_note: str
    role: str

    @property
    def label(self):
        return f'{self.display_name}[顺位：{self.index}](' + ('官方动作' if self.official_index is not None else '额外动作') + ')'

    @property
    def duration(self):
        return max(self.frame_count - 1, 0) / self.fps


@lru_cache(maxsize=1)
def _rules():
    return json.loads((Path(__file__).parent / 'data/action_activities.json').read_text(encoding='utf-8'))


def activity_name(activity: str, profile_id: str) -> str | None:
    """Look up exact approved names, resolving weapon-dependent wording."""
    get_profile(profile_id)
    item = _rules()['activities'].get(activity)
    if item is None:
        return None
    label = item['display_name']
    label = label.replace('开火／近战主攻击', '近战主攻击' if profile_id in MELEE_IDS else '开火')
    label = label.replace('左手开火／近战推击', '近战推击' if profile_id in MELEE_IDS else '左手开火')
    return label.replace('换弹／霰弹枪换弹起始', '霰弹枪换弹起始' if profile_id in SHOTGUN_IDS else '换弹')


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    quoted: bool = False


def _lex(text):
    result = []
    i = 0
    while i < len(text):
        if text[i].isspace() or text[i] == '\ufeff':
            i += 1
            continue
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise ValueError('QC 注释未闭合')
            i = end + 2
            continue
        start = i
        if text[i] == '"':
            # Source QC uses literal backslashes, including a directory's
            # trailing backslash before its closing quote. Match the existing
            # converted-QC tokenizer rather than applying JSON/C escapes.
            i = text.find('"', i + 1)
            if i < 0:
                i = len(text)
            if i == len(text):
                raise ValueError('QC 字符串未闭合')
            result.append(_Token(text[start + 1:i], start, i + 1, True))
            i += 1
        elif text[i] in '{}':
            i += 1
            result.append(_Token(text[start:i], start, i))
        else:
            while i < len(text) and not text[i].isspace() and text[i] not in '{}"':
                i += 1
            result.append(_Token(text[start:i], start, i))
    return tuple(result)


@dataclass(frozen=True)
class _Command:
    kind: str
    name: str
    start: int
    end: int
    tokens: tuple[_Token, ...]


def _commands(text):
    tokens = _lex(text)
    starts = []
    depth = 0
    for i, t in enumerate(tokens):
        if not t.quoted:
            if t.value == '{':
                depth += 1
            elif t.value == '}':
                depth -= 1
                if depth < 0:
                    raise ValueError('QC 多余的右花括号')
            elif depth == 0 and t.value.startswith('$'):
                starts.append(i)
    if depth:
        raise ValueError('QC 花括号未闭合')
    result = []
    for start, end in zip(starts, starts[1:] + [len(tokens)]):
        part = tokens[start:end]
        kind = part[0].value[1:].lower()
        if kind in {'animation', 'sequence'} and len(part) < 2:
            raise ValueError('QC 指令缺少参数')
        if kind in {'include', 'declaresequence', 'declareanimation', 'cd', 'pushd', 'popd'}:
            raise ValueError('动作矫正不支持未展开的 QC include/declare 或动态来源目录')
        result.append(_Command(kind, part[1].value if len(part) > 1 else '', part[0].start, part[-1].end, part))
    return tuple(result)


def _body(command):
    """Top-level options only; event blocks and quoted payloads are inert."""
    tokens = command.tokens
    result = []
    depth = 0
    first = 3 if command.kind == 'animation' else 2
    block = len(tokens) > first and not tokens[first].quoted and tokens[first].value == '{'
    for t in tokens[2:]:
        if not t.quoted and t.value == '{':
            depth += 1
        elif not t.quoted and t.value == '}':
            depth -= 1
        elif depth == (1 if block else 0):
            result.append(t)
    return tuple(result)


_OPTION_NAMES = frozenset('fps frames numframes activity loop hidden delta predelta post snap worldspace worldspaceblend autoplay realtime posecycle blend blendwidth blendref blendcomp blendcenter blendtype node transition rtransition exitphase fadein fadeout addlayer blendlayer weightlist subtract presubtract alignto align match matchblend reverse walkframe walkalignto walkalignrot rotateto origin rotate scale angles ikrule iklock lockmotion lineardelta splinedelta compress decimate blockname noanimation'.split())


def _refs(command):
    if command.kind == 'animation':
        if len(command.tokens) < 3 or command.tokens[2].value == '{':
            raise ValueError('QC animation 缺少来源引用')
        return (command.tokens[2],)
    result = []
    for token in _body(command):
        if not token.quoted and token.value.lower() in _OPTION_NAMES:
            break
        if not token.quoted and token.value.startswith('ACT_'):
            break
        result.append(token)
    if not result:
        raise ValueError('QC sequence 缺少动画引用: ' + command.name)
    return tuple(result)


def _options(command):
    body = _body(command)
    # Inline animation operands precede options; the block form naturally omits them.
    result = {}
    for i, token in enumerate(body):
        key = token.value.lower()
        if not token.quoted and key in {'compress', 'decimate', 'resample', 'fudgeloop', 'append', 'prepend', 'continue'}:
            raise ValueError('动作矫正暂不支持 QC 时间轴变换: ' + key)
        if token.quoted or key not in {'fps', 'numframes', 'frames', 'loop', 'activity'}:
            continue
        if key in result:
            raise ValueError('QC 选项重复: ' + key)
        arity = 0 if key == 'loop' else 2 if key in {'activity', 'frames'} else 1
        if i + arity >= len(body):
            raise ValueError('QC 选项缺少参数: ' + key)
        if key == 'fps':
            value = _positive(body[i + 1].value)
        elif key == 'frames':
            value = tuple(int(t.value) for t in body[i + 1:i + 3])
            if value[0] < 0 or value[1] < value[0]:
                raise ValueError('QC frames 无效')
        elif key == 'numframes':
            value = int(body[i + 1].value)
            if value <= 0:
                raise ValueError('QC numframes 无效')
        elif key == 'activity':
            value = (body[i + 1].value, float(body[i + 2].value))
            if not math.isfinite(value[1]):
                raise ValueError('QC Activity 权重无效')
        else:
            value = True
        result[key] = value
    return result


def _positive(value):
    if isinstance(value, bool):
        raise ValueError('FPS 必须为有限正数')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('FPS 必须为有限正数') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError('FPS 必须为有限正数')
    return number


def _effective(count, options):
    if 'frames' in options:
        first, last = options['frames']
        if last >= count:
            raise ValueError('QC frames 越出来源时间轴')
        count = last - first + 1
    return options.get('numframes', count)


@dataclass(frozen=True)
class _Reference:
    token: _Token
    animation: _Command | None
    fps: float
    frame_count: int
    smd_count: int


class _QcDocument:
    def __init__(self, qc_path):
        self.path = Path(qc_path).resolve()
        with self.path.open(encoding='utf-8', newline='') as handle:
            self.text = handle.read()
        self.commands = _commands(self.text)
        self.sequences = tuple(c for c in self.commands if c.kind == 'sequence')
        self.animations = {}
        names = set()
        for c in self.commands:
            if c.kind not in {'animation', 'sequence'}:
                continue
            key = c.name.lower()
            if key in names:
                raise ValueError('QC 动画/sequence 名称重复: ' + c.name)
            names.add(key)
            if c.kind == 'animation':
                self.animations[key] = c
        self._smd_counts = {}

    def smd_counts(self, filename):
        filename = filename.replace('\\', '/')
        source = (self.path.parent / filename).resolve()
        if source.suffix.lower() != '.smd' or not source.is_relative_to(self.path.parent):
            raise ValueError('不支持或越界的动画来源: ' + filename)
        if source not in self._smd_counts:
            active = False
            times = []
            with source.open(encoding='utf-8-sig') as handle:
                for line in handle:
                    line = line.strip()
                    if line == 'skeleton':
                        active = True
                    elif active and line == 'end':
                        break
                    elif active and line.startswith('time '):
                        times.append(int(line.split()[1]))
            if not times or times[0] != 0 or any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError('SMD 时间轴无效: ' + filename)
            self._smd_counts[source] = (len(times), times[-1] + 1)
        return self._smd_counts[source]

    def references(self, sequence):
        result = []
        opts = _options(sequence)
        tokens = _refs(sequence)
        first_key = tokens[0].value.lower()
        first_animation = self.animations.get(first_key)
        sequence_rate = opts.get('fps', _options(first_animation).get('fps', 30.0) if first_animation else 30.0)
        for i, token in enumerate(tokens):
            animation = self.animations.get(token.value.lower())
            if animation:
                source = _refs(animation)[0].value
                options = _options(animation)
            else:
                source = token.value
                options = {}
            raw, span = self.smd_counts(source)
            frames = _effective(span, options)
            fps = options.get('fps', 30.0)
            if animation is None or token.value.lower() == first_key:
                fps = sequence_rate
            if animation is None:
                # Source applies the sequence crop to its primary implicit
                # animation; secondary direct sources only inherit numframes.
                frame_options = opts if i == 0 else {k: v for k, v in opts.items() if k != 'frames'}
                frames = _effective(frames, frame_options)
            elif token.value.lower() == first_key:
                frames = _effective(frames, opts)
            result.append(_Reference(token, animation, fps, frames, raw))
        return tuple(result)


def _base_activity(activity):
    name = activity.removeprefix('MIXED_')
    name = re.sub(r'^ACT_(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_VM_', 'ACT_VM_', name)
    return name.replace('_SNIPER', '')


def _official_slot(command, activity, index, profile, explicit):
    if explicit is not None:
        value = explicit.get(index) if hasattr(explicit, 'get') else (explicit[index] if index < len(explicit) else None)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(profile.slots):
            raise ValueError('官方 sequence 顺位无效')
        slot = profile.slots[value]
        if slot.activity != activity:
            raise ValueError('官方 sequence 身份与 Activity 不匹配')
        return slot
    return next((s for s in profile.slots if s.name.lower() == command.name.lower() and s.activity == activity), None)


def _authored_empty_reload(activity,profile_id):
    # Necola's EmptyReloadController replaces the ordinary visible reload on
    # magazine weapons; it does not make their timer or shotgun stages playable.
    expected='ACT_VM_RELOAD_EMPTY_SNIPER_LAYER' if profile_id=='hunting_rifle' else 'ACT_VM_RELOAD_EMPTY_LAYER'
    return profile_id not in MELEE_IDS|SHOTGUN_IDS and activity==expected


def _playback_role(activity, profile_id, slot):
    if activity not in _rules()['activities']:
        return None
    base = _base_activity(activity)
    melee = profile_id in MELEE_IDS
    if base.startswith(('ACT_VM_HIT', 'ACT_VM_SHOOT', 'ACT_VM_SWINGHARD')) and not melee:
        # Hunting rifles use the explicit SHOOT_SNIPER_LAYER Activity.
        if activity != 'ACT_VM_SHOOT_SNIPER_LAYER':
            return None
    if 'SWINGHARD' in base or activity in {'ACT_FOURTH_VM_IDLE_TO_NEXT', 'MIXED_ACT_FOURTH_VM_IDLE_TO_NEXT', 'MIXED_ACT_FOURTH_VM_LOWERED_TO_NEXT'}:
        return None
    if slot is not None:
        role = _rules()['profiles'][profile_id][slot.index]['role']
        return role if role in {'playback', 'playback_timer', 'conditional_playback'} else None
    conditional = activity.startswith(('MIXED_', 'ACT_PRIMARY_', 'ACT_SECONDARY_', 'ACT_TERTIARY_', 'ACT_FOURTH_')) or activity in {'ACT_VM_FIDGET', 'ACT_VM_MIXED_ON', 'ACT_VM_MIXED_OFF'}
    if conditional:
        return 'conditional_playback'
    candidates = [s for s in get_profile(profile_id).slots if s.activity == activity]
    roles = {_rules()['profiles'][profile_id][s.index]['role'] for s in candidates}
    if 'playback_timer' in roles:
        return 'playback_timer'
    if 'playback' in roles:
        return 'playback'
    if _authored_empty_reload(activity,profile_id):
        return 'conditional_playback'
    return None


def _timing_activity(activity, profile):
    normalized = _base_activity(activity).removesuffix('_LAYER')
    if normalized == 'ACT_VM_SHOOT':
        normalized = 'ACT_VM_PRIMARYATTACK'
    if normalized == 'ACT_VM_DRAW' and profile.id not in MELEE_IDS:
        normalized = 'ACT_VM_DEPLOY'
    return normalized


def _timing(activity, profile, slot, loop):
    if loop:
        return None, None, None, '循环动作：一轮播放长度；无已确认固定结束计时'
    normalized = _timing_activity(activity, profile)
    candidates = [slot] if slot is not None else [s for s in profile.slots if _timing_activity(s.activity, profile) == normalized and not s.unused]
    empty_fallback=slot is None and not candidates and _authored_empty_reload(activity,profile.id)
    if empty_fallback:
        candidates=[s for s in profile.slots if _timing_activity(s.activity,profile)=='ACT_VM_RELOAD' and not s.unused]
    timers = []
    for candidate in candidates:
        timer_index = _rules()['profiles'][profile.id][candidate.index]['timer_index']
        if timer_index is None:
            continue
        timer = profile.slots[timer_index]
        if not timer.loop and not timer.flexible and timer.end_frame and timer.fps:
            timers.append((timer.end_frame / timer.fps, timer.end_frame + 1, timer.fps))
    if timers and len(set(timers)) == 1:
        note = ('作者额外空仓换弹：沿用目标普通换弹的已确认计时限制' if empty_fallback else
                '目标普通动作的已确认计时限制（非官方 ADS/MIXED 动画）' if activity.startswith(('MIXED_', 'ACT_PRIMARY_', 'ACT_SECONDARY_', 'ACT_TERTIARY_', 'ACT_FOURTH_')) else '官方实际计时槽（零起始 End Frame / FPS）')
        return (*timers[0], note)
    if not candidates:
        return None, None, None, '无官方对应项；依赖 Necola 的实际调用路径'
    return None, None, None, '尚未确认操作限制时长；原版动画长度不能直接作为操作限制'


def load_actions(qc_path, profile_id: str, official_indexes=None, *, include_timers=False) -> tuple[ActionInfo, ...]:
    profile = get_profile(profile_id)
    document = _QcDocument(qc_path)
    result = []
    for index, command in enumerate(document.sequences):
        options = _options(command)
        activity, weight = options.get('activity', ('', 0))
        slot = _official_slot(command, activity, index, profile, official_indexes)
        role = _playback_role(activity, profile_id, slot)
        if not role and (not include_timers or not activity):
            continue
        role=role or 'activity_only'
        refs = document.references(command)
        loop = options.get('loop', False) or any(r.animation and _options(r.animation).get('loop', False) for r in refs)
        duration, count, fps, note = _timing(activity, profile, slot, loop)
        if weight == 0:
            role = 'conditional_playback'
            note += '；Activity 权重为 0，依赖引擎回退或显式调用，不视为禁用'
        result.append(ActionInfo(index, command.name, activity, slot.index if slot else None,
                                 refs[0].fps, refs[0].frame_count, tuple(r.smd_count for r in refs),
                                 bool(loop), activity_name(activity, profile_id) or activity, duration, count, fps, note, role))
    return tuple(result)


def _patch(text, patches):
    for first, last, value in sorted(patches, reverse=True):
        text = text[:first] + value + text[last:]
    return text


def _number(value):
    return fps_text(value)


def _fps_patch(command, value, newline):
    body = _body(command)
    for i, token in enumerate(body):
        if not token.quoted and token.value.lower() == 'fps':
            return (body[i + 1].start, body[i + 1].end, _number(value))
    last = command.tokens[-1]
    first = 3 if command.kind == 'animation' else 2
    if len(command.tokens) > first and not command.tokens[first].quoted and command.tokens[first].value == '{':
        return (last.start, last.start, newline + '\tfps ' + _number(value) + newline)
    return (last.end, last.end, ' fps ' + _number(value))


# These options act on an implicit animation. A direct reference that needs
# its own rate (a static secondary) must retain them on its new declaration.
# Sequence-only options/events remain solely on the original sequence.
_ANIMATION_LOCAL_OPTIONS = frozenset('frames numframes loop delta predelta post worldspace worldspaceblend reverse origin rotate scale angles weightlist subtract presubtract alignto align match matchblend walkframe walkalignto walkalignrot rotateto ikrule iklock lockmotion lineardelta splinedelta noanimation'.split())


def _direct_animation_options(document, sequence):
    body = _body(sequence)[len(_refs(sequence)):]
    starts = [i for i, t in enumerate(body) if not t.quoted and t.value.lower() in _OPTION_NAMES]
    parts = []
    for first, last in zip(starts, starts[1:] + [len(body)]):
        if body[first].value.lower() in _ANIMATION_LOCAL_OPTIONS - {'frames'}:
            # Only secondary static direct references migrate here. Their
            # original implicit descriptor never inherited the primary crop.
            parts.append(document.text[body[first].start:body[last - 1].end])
    return ' '.join(parts)


def retime_qc(qc_path, updates: dict[int, float]) -> str:
    """Patch speeds without changing source files, events or shared references.

    Named references are cloned before the first sequence. Non-primary rates
    scale by the primary timeline ratio; static references retain their rates.
    Sequence identity, constraints, ordering, comments and unedited commands are
    preserved byte-for-byte in the returned Unicode text.
    """
    document = _QcDocument(qc_path)
    normalized = {}
    for index, value in updates.items():
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(document.sequences):
            raise ValueError('sequence 顺位无效')
        normalized[index] = _positive(value)
        round_fps(value)
    if not normalized:
        return document.text
    newline = '\r\n' if '\r\n' in document.text else '\n'
    names = {c.name.lower() for c in document.commands if c.kind in {'animation', 'sequence'}}
    patches = []
    clones = []
    for index, value in normalized.items():
        sequence = document.sequences[index]
        refs = document.references(sequence)
        old = refs[0].fps
        if value == old:
            continue
        patches.append(_fps_patch(sequence, value, newline))
        clone_names = {}
        for i, ref in enumerate(refs):
            if ref.frame_count <= 1:
                rate = ref.fps
            elif ref.animation is None or ref.token.value.lower() == refs[0].token.value.lower():
                rate = value
            else:
                rate = scaled_fps(ref.fps, value, old)
            rate = round_fps(rate)
            # Direct references already have independent compiler descriptors.
            # Keep their full authored semantics whenever sequence FPS suffices;
            # moving crop/origin/IK commands to a named declaration changes how
            # the compiler applies them, even when frame counts appear equal.
            if ref.animation is None and (i == 0 or rate == round_fps(value)):
                continue
            key = (ref.token.value.lower(), rate)
            name = clone_names.get(key)
            if name is None:
                suffix = 0
                while True:
                    name = f'loka_action_{index}_{i}_{suffix}'
                    if name.lower() not in names:
                        break
                    suffix += 1
                names.add(name.lower())
                clone_names[key] = name
                if ref.animation:
                    original = ref.animation
                    changes = [_fps_patch(original, rate, newline), (original.tokens[1].start, original.tokens[1].end, '"' + name + '"')]
                    relative = [(a - original.start, b - original.start, v) for a, b, v in changes]
                    clone = _patch(document.text[original.start:original.end], relative)
                else:
                    source = document.text[ref.token.start:ref.token.end]
                    options = _direct_animation_options(document, sequence)
                    clone = f'$animation "{name}" {source} {{ {options} fps {_number(rate)} }}'
                clones.append(clone + newline)
            patches.append((ref.token.start, ref.token.end, '"' + name + '"'))
    if clones:
        first = document.sequences[0].start
        patches.append((first, first, ''.join(clones)))
    return _patch(document.text, patches)
