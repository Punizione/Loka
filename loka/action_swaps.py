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


def load_swap_actions(qc_path, profile_id, indexes=None):
    """Include every explicit Activity; indexes maps QC positions to official slots."""
    return load_actions(qc_path, profile_id, indexes, include_timers=True)


def permute_qc(qc_path, assignments: dict[int, int], profile_id) -> str:
    """Return one closed, bijective destination-to-source payload permutation."""
    from types import SimpleNamespace
    get_profile(profile_id)
    document = _QcDocument(qc_path)
    for destination, source in assignments.items():
        for index in (destination, source):
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(document.sequences):
                raise ValueError('调换 sequence 顺位无效')
    if set(assignments) != set(assignments.values()) or len(set(assignments.values())) != len(assignments):
        raise ValueError('动作调换必须为闭合双射映射')
    activities = {i:_options(document.sequences[i]).get('activity', ('', 0))[0] for i in assignments}
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
