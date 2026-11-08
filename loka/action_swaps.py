"""Pure QC action permutations; slot identities remain at their original positions."""
import re

from .action_rules import _QcDocument, _body, _options, _patch, load_actions
from .weapon_catalog import MELEE_IDS
from .weapon_sequences import get_profile


_ATTACK_DOMAIN = re.compile(r'^((?:MIXED_)?ACT_(?:(?:PRIMARY|SECONDARY|TERTIARY|FOURTH)_)?VM_)(?:HIT(?:LEFT|RIGHT|CENTER)(_LAYER)?|SHOOT_LAYER)$')


def validate_swap_pair(first, second, profile_id) -> None:
    """Allow Activity peers or same-domain melee HIT/SHOOT playback attacks.

    SHOOT_LAYER joins the ordinary three-direction HIT group. The separate
    HIT*_LAYER group retains its existing boundary, keeping chained swaps
    closed under composition when the final payload permutation is saved.
    """
    get_profile(profile_id)
    left = first.activity
    right = second.activity
    if not left or not right:
        raise ValueError('无 Activity 的动作不能调换')
    if left == right:
        return
    a = _ATTACK_DOMAIN.fullmatch(left)
    b = _ATTACK_DOMAIN.fullmatch(right)
    if profile_id in MELEE_IDS and a and b and a.groups() == b.groups():
        return
    raise ValueError('动作 Activity 不同，且不属于同域近战 HIT/SHOOT 攻击组')


def validate_replace_map(assignments: dict[int, int]) -> None:
    """Validate a replacement-style mapping: unique targets, never self-mapping.

    Assignments are destination-to-donor pairs. Unlike a swap they need not be
    closed or injective: several targets may take the same donor action, and a
    donor may itself be a target (``a->b`` with ``b->c``). A dict already keeps
    every destination unique.
    """
    if not isinstance(assignments, dict):
        raise ValueError('替换式映射必须是目标顺位到来源顺位的字典')
    for destination, donor in assignments.items():
        if (isinstance(destination, bool) or isinstance(donor, bool)
                or not isinstance(destination, int) or not isinstance(donor, int)):
            raise ValueError('替换式映射的目标与来源顺位必须是整数')
        if destination < 0 or donor < 0:
            raise ValueError('替换式映射的目标与来源顺位不能为负数')
        if destination == donor:
            raise ValueError('替换式映射不允许把动作替换到自身')


def load_swap_actions(qc_path, profile_id, indexes=None):
    """Include every explicit Activity; indexes maps QC positions to official slots."""
    return load_actions(qc_path, profile_id, indexes, include_timers=True)


def permute_qc(qc_path, assignments: dict[int, int], profile_id) -> str:
    """Return one destination-to-source payload mapping; donors may repeat.

    Both swap and replacement drafts pass through here: a swap is the closed
    bijective special case, a replacement may reuse a donor and keep that donor
    slot unchanged. Every payload is cut from the baseline document text, so
    reusing one donor for several targets cannot corrupt another patch.
    """
    from types import SimpleNamespace
    get_profile(profile_id)
    document = _QcDocument(qc_path)
    validate_replace_map(assignments)
    for destination, source in assignments.items():
        for index in (destination, source):
            if not 0 <= index < len(document.sequences):
                raise ValueError('调换 sequence 顺位无效')
    # A donor need not be a destination, so both sides supply Activity lookups.
    activities = {i:_options(document.sequences[i]).get('activity', ('', 0))[0]
                  for i in set(assignments) | set(assignments.values())}
    for destination, source in assignments.items():
        validate_swap_pair(SimpleNamespace(activity=activities[destination]),
                           SimpleNamespace(activity=activities[source]), profile_id)
    patches = []
    for destination, source in assignments.items():
        if destination == source:
            continue
        target = document.sequences[destination]
        original = document.sequences[source]
        changes = [(original.tokens[1].start - original.start, original.tokens[1].end - original.start,
                    document.text[target.tokens[1].start:target.tokens[1].end])]
        body = _body(original)
        for index, token in enumerate(body):
            if not token.quoted and token.value.lower() == 'activity':
                operand = body[index + 1]
                value = activities[destination]
                if operand.quoted:
                    value = '"' + value + '"'
                changes.append((operand.start - original.start, operand.end - original.start, value))
                break
        payload = _patch(document.text[original.start:original.end], changes)
        patches.append((target.start, target.end, payload))
    return _patch(document.text, patches)
