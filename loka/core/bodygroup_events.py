"""Shared logic for the "BodyGroup 兼容" decompile feature.

L4D2 viewmodels switch parts (e.g. a scope) through the
``AE_CL_BODYGROUP_SET_VALUE`` event: the engine sets one *bodygroup* to one of
its *models* (identified by the model's index inside the bodygroup) when the
event fires.  A "mixed" model keeps two weapon states (mixed on/off × scope
on/off) as bodygroup entries. The decompiler finds the
bodygroup whose SMD (model) names start with ``mixed_``, matches the four
``ACT_VM/ACT_PRIMARY_VM MIXED_ON/MIXED_OFF`` activities plus the two ads
correction transitions (``ACT_PRIMARY_VM_IDLE_TO_LOWERED`` /
``ACT_PRIMARY_VM_LOWERED_TO_IDLE``) to those entries (by default via the
canonical names, overridable per activity by the user), and marks each matched
sequence with a ``theBodygroupEvent`` attribute that the QC writer emits --
always at frame 0 -- as::

    { event AE_CL_BODYGROUP_SET_VALUE <frame> "<bodygroup_name> <smd_index>" }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .naming import get_file_name, get_file_name_without_extension

__all__ = [
    "AE_CL_BODYGROUP_SET_VALUE",
    "MIXED_ACTIVITIES",
    "DEFAULT_MIXED_SMD_NAMES",
    "MIXED_SUPPLEMENT_SEQUENCE_NAMES",
    "BodygroupOverview",
    "clean_model_name",
    "collect_bodygroup_overviews",
    "apply_bodygroup_event_setup",
    "append_mixed_transition_sequences",
]

#: The engine event that sets a bodygroup entry at runtime.
AE_CL_BODYGROUP_SET_VALUE = "AE_CL_BODYGROUP_SET_VALUE"

#: Activities receiving frame-zero bodygroup events.
MIXED_ACTIVITIES: tuple[str, ...] = (
    "ACT_VM_MIXED_OFF",
    "ACT_VM_MIXED_ON",
    "ACT_PRIMARY_VM_MIXED_OFF",
    "ACT_PRIMARY_VM_MIXED_ON",
    "ACT_PRIMARY_VM_IDLE_TO_LOWERED",
    "ACT_PRIMARY_VM_LOWERED_TO_IDLE",
)

#: Canonical entry names used by automatic matching.
#: Events fire at frame 0 for every activity.
DEFAULT_MIXED_SMD_NAMES: dict[str, str] = {
    "ACT_VM_MIXED_OFF": "mixed_off_scope_off",
    "ACT_VM_MIXED_ON": "mixed_on_scope_off",
    "ACT_PRIMARY_VM_MIXED_OFF": "mixed_off_scope_on",
    "ACT_PRIMARY_VM_MIXED_ON": "mixed_on_scope_off",
    "ACT_PRIMARY_VM_IDLE_TO_LOWERED": "mixed_off_scope_on",
    "ACT_PRIMARY_VM_LOWERED_TO_IDLE": "mixed_off_scope_off",
}


#: Names assigned to generated mixed-state transition sequences.
MIXED_SUPPLEMENT_SEQUENCE_NAMES: tuple[str, ...] = (
    "ads_mixed_lowered_to_mixed_idle",
    "mixed_lowered_to_next",
    "idle_to_mixed_idle",
)

#: activity name each supplement sequence carries.
_MIXED_SUPPLEMENT_ACTIVITIES: dict[str, str] = {
    "ads_mixed_lowered_to_mixed_idle": "MIXED_ACT_PRIMARY_VM_LOWERED_TO_MIXED_IDLE",
    "mixed_lowered_to_next": "MIXED_ACT_VM_LOWERED_TO_NEXT",
    "idle_to_mixed_idle": "ACT_PRIMARY_VM_IDLE_TO_MIXED_IDLE",
}

#: Headers for generated transition sequences.
_MIXED_SUPPLEMENT_COMMENTS: dict[str, tuple[str, ...]] = {
    "ads_mixed_lowered_to_mixed_idle": (
        "// MIXED transition",
    ),
    "mixed_lowered_to_next": (
        "// MIXED transition",
    ),
    "idle_to_mixed_idle": (
        "// MIXED transition",
    ),
}

#: source activity each supplement sequence clones its body from.
_MIXED_SUPPLEMENT_SOURCES: dict[str, str] = {
    "ads_mixed_lowered_to_mixed_idle": "ACT_PRIMARY_VM_LOWERED_TO_IDLE",
    "mixed_lowered_to_next": "ACT_PRIMARY_VM_IDLE_TO_LOWERED",
    "idle_to_mixed_idle": "ACT_PRIMARY_VM_IDLE_TO_LOWERED",
}

_SEQUENCE_BLOCK_RE = re.compile(r'\$sequence\s+"([^"]+)"\s*\{.*?\n\}', re.S)
_ACTIVITY_RE = re.compile(r'activity\s+"([^"]+)"\s*(\d+)')
_BG_EVENT_RE = re.compile(r'\{ event AE_CL_BODYGROUP_SET_VALUE \d+ "([^"]+)" \}')
_SMD_RE = re.compile(r'"([^"]+\.smd)"')


@dataclass
class BodygroupOverview:
    """One bodygroup that contains ``mixed_`` entries."""

    name: str
    body_part_index: int
    #: ``(clean smd name, model index)`` for *every* entry of the bodygroup.
    entries: list[tuple[str, int]] = field(default_factory=list)
    #: how many entries start with ``mixed_``
    mixed_count: int = 0


def clean_model_name(raw_name: str) -> str:
    """Normalise an MDL model name ("path/mixed_a.smd") to "mixed_a"."""
    return get_file_name_without_extension(get_file_name(raw_name or "")).strip()


def _collect_activity_sequences(mdl_file_data: Any) -> dict[str, list]:
    """Map activity name -> the sequence descs carrying it."""
    sequences: dict[str, list] = {}
    for a_sequence_desc in _get(mdl_file_data, "theSequenceDescs", None) or []:
        activity = _get(a_sequence_desc, "theActivityName", "")
        if activity:
            sequences.setdefault(activity, []).append(a_sequence_desc)
    return sequences


def _get(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return default if value is None else value


def collect_bodygroup_overviews(mdl_file_data: Any) -> list[BodygroupOverview]:
    """Return every bodygroup containing at least one ``mixed_`` entry.

    Sorted by ``mixed_count`` descending (the "most mixed" bodygroup first) so
    callers can simply take the first one.  Returns an empty list when the MDL
    has no body parts.
    """
    body_parts = _get(mdl_file_data, "theBodyParts", None) or []
    overviews: list[BodygroupOverview] = []
    for body_part_index, a_body_part in enumerate(body_parts):
        models = _get(a_body_part, "theModels", None) or []
        entries: list[tuple[str, int]] = []
        mixed_count = 0
        for model_index, a_model in enumerate(models):
            name = clean_model_name(_get(a_model, "name", ""))
            entries.append((name, model_index))
            if name.lower().startswith("mixed_"):
                mixed_count += 1
        if mixed_count <= 0:
            continue
        overviews.append(
            BodygroupOverview(
                name=_get(a_body_part, "theName", ""),
                body_part_index=body_part_index,
                entries=entries,
                mixed_count=mixed_count,
            )
        )
    overviews.sort(key=lambda overview: -overview.mixed_count)
    return overviews


def _resolve_entry_index(
    activity: str, entries: list[tuple[str, int]], chosen_name: str
) -> Optional[int]:
    """Resolve the smd_index for one activity.

    ``chosen_name`` (a user pick) wins when it names an entry.  Otherwise the
    canonical name from :data:`DEFAULT_MIXED_SMD_NAMES` is used when present,
    falling back to a loose match on the ``mixed_on/off`` × ``scope_on/off``
    parts of the activity's default name.
    """
    by_name = {name: index for name, index in entries}
    if chosen_name:
        return by_name.get(chosen_name)

    default_name = DEFAULT_MIXED_SMD_NAMES.get(activity, "")
    if default_name in by_name:
        return by_name[default_name]

    # Loose fallback: "mixed_on_scope_off" -> look for an entry containing
    # both the on/off head and the scope_on/off tail; tolerant to prefixes
    # and suffixes around the canonical name.
    parts = default_name.split("_")  # e.g. ["mixed", "on", "scope", "off"]
    if len(parts) >= 4:
        head = "_".join(parts[:2])  # mixed_on / mixed_off
        tail = "_".join(parts[-2:])  # scope_off / scope_on
        for name, index in entries:
            lowered = name.lower()
            if head in lowered and tail in lowered:
                return index
    return None


def apply_bodygroup_event_setup(mdl_file_data: Any, options: Any) -> list[str]:
    """Compute and attach ``theBodygroupEvent`` to the mixed sequences.

    Requires ``options.bodygroup_events``.  The matching can be overridden per
    activity through ``options.bodygroup_event_map`` (activity -> entry name;
    empty/missing = automatic).  Returns a list of human-readable warnings;
    none of them abort the decompile.
    """
    warnings: list[str] = []
    if not getattr(options, "bodygroup_events", False):
        return warnings

    body_parts = _get(mdl_file_data, "theBodyParts", None) or []
    distinct_names = {
        _get(a_body_part, "theName", "") for a_body_part in body_parts
    }
    if len(distinct_names) < 3:
        warnings.append(
            "BodyGroup 兼容: 模型的 bodygroup 数量少于 3 个，未添加 mixed 事件。"
        )
        return warnings

    overviews = collect_bodygroup_overviews(mdl_file_data)
    if not overviews:
        warnings.append(
            "BodyGroup 兼容: 没有找到包含 mixed_ 开头 SMD 的 bodygroup，未添加事件。"
        )
        return warnings

    overview = overviews[0]
    if overview.mixed_count < 4:
        warnings.append(
            f"BodyGroup 兼容: bodygroup \"{overview.name}\" 只有 {overview.mixed_count} 个 "
            "mixed_ 开头的条目（一般最少 4 个），仍继续执行。"
        )

    user_map = getattr(options, "bodygroup_event_map", None) or {}
    sequences_by_activity = _collect_activity_sequences(mdl_file_data)

    for activity in MIXED_ACTIVITIES:
        chosen = str(user_map.get(activity, "") or "").strip()
        smd_index = _resolve_entry_index(activity, overview.entries, chosen)
        if smd_index is None:
            if chosen:
                warnings.append(
                    f"BodyGroup 兼容: 活动 {activity} 手动选择的 \"{chosen}\" 不在 "
                    f"bodygroup \"{overview.name}\" 中，未添加事件。"
                )
            else:
                warnings.append(
                    f"BodyGroup 兼容: 活动 {activity} 无法自动匹配 "
                    f"bodygroup \"{overview.name}\" 的条目，未添加事件。"
                )
            continue

        matched = sequences_by_activity.get(activity, [])
        if not matched:
            warnings.append(
                f"BodyGroup 兼容: 没有找到活动为 {activity} 的 $sequence，未添加事件。"
            )
            continue

        # Apply the bodygroup selection at the sequence's first frame.
        for a_sequence_desc in matched:
            setattr(
                a_sequence_desc,
                "theBodygroupEvent",
                (overview.name, smd_index, 0),
            )

    return warnings


def _first_block_with_activity(
    blocks: list[re.Match[str]], activity: str
) -> Optional[re.Match[str]]:
    for block in blocks:
        match = _ACTIVITY_RE.search(block.group(0))
        if match is not None and match.group(1) == activity:
            return block
    return None


def _clone_supplement_block(
    source_block: str, new_name: str, new_activity: str, payload: str
) -> str:
    """Clone one $sequence block, repointing activity / delta / event.

    Keep the source body (smd, fadein/fadeout, fps, ikrules), update the
    sequence and activity names, and write
    ``delta`` + ``subtract "a_idle_1" 0`` + one mixed-value bodygroup event
    in activity / delta / subtract / event / fadein order. Drop
    every pre-existing delta/subtract/bodygroup-event line.  Line-based on
    purpose: the writer emits CRLF and the caller's text must round-trip
    byte-stable.
    """
    eol = "\r\n" if "\r\n" in source_block else "\n"
    lines = source_block.split(eol)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "delta" or stripped.startswith("subtract "):
            continue
        if stripped.startswith("{ event AE_CL_BODYGROUP_SET_VALUE "):
            continue
        if stripped.startswith("$sequence "):
            out.append(re.sub(r'"[^"]+"', f'"{new_name}"', line, count=1))
            continue
        if stripped.startswith("activity "):
            out.append(_ACTIVITY_RE.sub(
                lambda match: f'activity "{new_activity}" {match.group(2)}',
                line,
                count=1,
            ))
            indent = line[: len(line) - len(line.lstrip("\t"))]
            out.append(f"{indent}delta")
            out.append(f'{indent}subtract "a_idle_1" 0')
            out.append(
                f'{indent}{{ event AE_CL_BODYGROUP_SET_VALUE 0 "{payload}" }}'
            )
            continue
        out.append(line)
    return eol.join(out)


def append_mixed_transition_sequences(qc_text: str) -> tuple[str, list[str]]:
    """Append missing mixed-state transition sequences to a QC.

    Fires only when the QC declares ``ACT_PRIMARY_VM_MIXED_ON`` +
    ``ACT_PRIMARY_VM_MIXED_OFF`` without any ``MIXED_ACT_PRIMARY_VM_IDLE``
    **and** ``ACT_VM_MIXED_ON`` + ``ACT_VM_MIXED_OFF`` without any
    ``MIXED_ACT_VM_IDLE``. The new
    sequences reuse the existing ads transition SMDs and write the mixed
    bodygroup value taken from the ``ACT_VM_MIXED_ON`` sequence's
    ``AE_CL_BODYGROUP_SET_VALUE`` event.  Returns ``(new_text, notes)``;
    notes are informational lines, none abort the decompile.
    """
    notes: list[str] = []
    blocks = list(_SEQUENCE_BLOCK_RE.finditer(qc_text))
    names = {block.group(1) for block in blocks}

    # idempotent: nothing to do when the supplements are already present
    if any(name in names for name in MIXED_SUPPLEMENT_SEQUENCE_NAMES):
        return qc_text, notes

    has_primary = (
        _first_block_with_activity(blocks, "ACT_PRIMARY_VM_MIXED_ON") is not None
        and _first_block_with_activity(blocks, "ACT_PRIMARY_VM_MIXED_OFF") is not None
    )
    has_vm = (
        _first_block_with_activity(blocks, "ACT_VM_MIXED_ON") is not None
        and _first_block_with_activity(blocks, "ACT_VM_MIXED_OFF") is not None
    )
    if not (has_primary or has_vm):
        return qc_text, notes  # not a Necola-style mixed model

    missing = []
    if not has_primary:
        missing.append("ACT_PRIMARY_VM_MIXED_ON/OFF")
    if '"MIXED_ACT_PRIMARY_VM_IDLE"' in qc_text:
        missing.append("MIXED_ACT_PRIMARY_VM_IDLE 已存在")
    if not has_vm:
        missing.append("ACT_VM_MIXED_ON/OFF")
    if '"MIXED_ACT_VM_IDLE"' in qc_text:
        missing.append("MIXED_ACT_VM_IDLE 已存在")
    if missing:
        notes.append(
            "BodyGroup 兼容: 检测到 MIXED 活动但条件不完整（"
            + "；".join(missing)
            + "），未补充 MIXED 过渡序列。"
        )
        return qc_text, notes

    mixed_block = _first_block_with_activity(blocks, "ACT_VM_MIXED_ON")
    if mixed_block is None:
        mixed_block = _first_block_with_activity(blocks, "ACT_PRIMARY_VM_MIXED_ON")
    match = _BG_EVENT_RE.search(mixed_block.group(0))
    if match is None:
        notes.append(
            "BodyGroup 兼容: MIXED_ON 序列没有 AE_CL_BODYGROUP_SET_VALUE 事件，"
            "未补充 MIXED 过渡序列。"
        )
        return qc_text, notes
    payload = match.group(1)  # e.g. "scope_transform 2"
    value = payload.split()[-1] if payload.split() else ""

    lowered_to_idle = _first_block_with_activity(
        blocks, _MIXED_SUPPLEMENT_SOURCES["ads_mixed_lowered_to_mixed_idle"]
    )
    idle_to_lowered = _first_block_with_activity(
        blocks, _MIXED_SUPPLEMENT_SOURCES["mixed_lowered_to_next"]
    )
    if lowered_to_idle is None or idle_to_lowered is None:
        missing_sources = []
        if lowered_to_idle is None:
            missing_sources.append("ACT_PRIMARY_VM_LOWERED_TO_IDLE")
        if idle_to_lowered is None:
            missing_sources.append("ACT_PRIMARY_VM_IDLE_TO_LOWERED")
        notes.append(
            "BodyGroup 兼容: 没有找到活动为 "
            + "、".join(missing_sources)
            + " 的 $sequence，未补充 MIXED 过渡序列。"
        )
        return qc_text, notes

    def source_label(block: re.Match[str]) -> str:
        smd_match = _SMD_RE.search(block.group(0))
        if smd_match is None:
            return "现有过渡序列"
        return get_file_name_without_extension(get_file_name(smd_match.group(1)))

    eol = "\r\n" if "\r\n" in qc_text else "\n"

    def build(name: str, source: re.Match[str]) -> str:
        comment = _MIXED_SUPPLEMENT_COMMENTS[name][0]
        block = _clone_supplement_block(
            source.group(0), name, _MIXED_SUPPLEMENT_ACTIVITIES[name], payload
        )
        return f"{comment}{eol}// 动作复用 {source_label(source)}，事件写 {value}{eol}{block}"

    insertion = eol.join(
        [
            "",
            "// --- Necola mode B 补充序列（视觉回退收口） ---",
            "// 目的：让 MIXED 态的回退路径命中主选活动，由事件写入正确 bodygroup 值"
            f"（{value}），",
            "// 消除回退过渡序列写错 bodygroup 的折衷。动作复用现有 SMD，仅换活动名与事件。",
            "",
            build("ads_mixed_lowered_to_mixed_idle", lowered_to_idle),
            "",
            build("mixed_lowered_to_next", idle_to_lowered),
            "",
            build("idle_to_mixed_idle", idle_to_lowered),
        ]
    )

    anchor = _first_block_with_activity(blocks, "ACT_PRIMARY_VM_MIXED_OFF")
    if anchor is not None:
        new_text = qc_text[: anchor.end()] + insertion + qc_text[anchor.end() :]
    else:
        new_text = qc_text + insertion

    notes.append(
        "BodyGroup 兼容: 已补充 3 个 MIXED 过渡序列。"
    )
    return new_text, notes
