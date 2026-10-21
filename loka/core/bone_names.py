"""Prepare indexed bone labels for source text without emitting diagnostics."""
from collections import defaultdict
import math
import struct
import unicodedata

from .reader import decode_ascii_bytes, EndOfStreamError
from ..model_status import UnsupportedModelError


def _unsafe(name):
    # VRD and $screenalign use unquoted tokens, unlike SMD nodes.
    return (not name or any(not (c.isalnum() or c in '_.-:') or
                           unicodedata.category(c) in ('Cc', 'Cf') for c in name))


def _validate_structure(data, reader):
    bones = data.theBones or []
    count = len(bones)
    if count != data.boneCount:
        raise UnsupportedModelError()
    if reader is not None:
        start = data.boneOffset
        if start < 0 or start > reader.length or count > (reader.length - start) // 216:
            raise UnsupportedModelError()
        saved = reader.position
        try:
            for index in range(count):
                record = start + index * 216
                reader.seek(record)
                relative = reader.read_int32()
                if relative:
                    address = record + relative
                    if not 0 <= address < reader.length:
                        raise UnsupportedModelError()
                    reader.seek(address)
                    while reader.read_byte() != 0:
                        pass
        except EndOfStreamError:
            raise UnsupportedModelError() from None
        finally:
            reader.seek(saved)
    for bone in bones:
        if not -1 <= bone.parentBoneIndex < count:
            raise UnsupportedModelError()
        for field in ('position', 'rotation', 'quat', 'positionScale', 'rotationScale',
                      'poseToBoneColumn0', 'poseToBoneColumn1', 'poseToBoneColumn2',
                      'poseToBoneColumn3', 'qAlignment'):
            vector = getattr(bone, field)
            if not all(math.isfinite(getattr(vector, axis)) for axis in ('x', 'y', 'z')):
                raise UnsupportedModelError()
            if hasattr(vector, 'w') and not math.isfinite(vector.w):
                raise UnsupportedModelError()
    complete = set()
    for index in range(count):
        chain = set()
        while index >= 0 and index not in complete:
            if index in chain:
                raise UnsupportedModelError()
            chain.add(index)
            index = bones[index].parentBoneIndex
        complete.update(chain)


def _resolve_reference(label, originals, names, candidates=None):
    matches = [i for i, name in enumerate(originals) if name.casefold() == label.casefold()]
    affected = any(names[i] != originals[i] for i in matches)
    if not affected and not _unsafe(label):
        return label
    if candidates is not None:
        matches = [i for i in matches if i in candidates]
    if len(matches) != 1:
        raise UnsupportedModelError()
    return names[matches[0]]


def normalize_bone_names(data, reader=None):
    """Alias unsafe or duplicate labels once, keeping every bone index intact."""
    bones = data.theBones or []
    if data.theOriginalBoneNames is not None:
        return
    originals = [bone.theName for bone in bones]
    reserved = {name.casefold() for name in originals if not _unsafe(name)}
    has_procedural = any(b.proceduralRuleOffset and b.proceduralRuleType in (2, 3, 4) for b in bones)
    def short_name(name):
        return name.split('.', 1)[-1].casefold()
    short_reserved = {short_name(name) for name in originals} if has_procedural else set()
    names = []; seen = set(); short_seen = set()
    for index, original in enumerate(originals):
        name = original
        if (_unsafe(name) or name.casefold() in seen or
                (has_procedural and (not short_name(name) or short_name(name) in short_seen))):
            base = f'__loka_bone_{index:03d}'
            name = base; suffix = 0
            while name.casefold() in reserved or name.casefold() in short_reserved:
                suffix += 1
                name = base + '_' + str(suffix)
            reserved.add(name.casefold())
            short_reserved.add(name.casefold())
        names.append(name); seen.add(name.casefold())
        short_seen.add(short_name(name))
    changes = {i: name for i, name in enumerate(names) if name != originals[i]}
    if not changes:
        return
    _validate_structure(data, reader)
    transforms = [(item, _resolve_reference(item.theName, originals, names))
                  for item in data.theBoneTransforms or []]
    # Commit after validating every name-only reference; originals stay in memory.
    data.theOriginalBoneNames = tuple(originals)
    data.theBoneNameRemap = changes
    for bone, name in zip(bones, names):
        bone.theName = name
    data.theBoneNameToBoneIndexMap = {name: i for i, name in enumerate(names)}
    for item, name in transforms:
        item.theName = name


def normalize_physics_bone_names(data, phy):
    """Prefer physical bone indices; reject ambiguous name-only references."""
    if not data.theBoneNameRemap or phy is None:
        return
    originals = data.theOriginalBoneNames
    bones = data.theBones; names = [bone.theName for bone in bones]
    by_physics = defaultdict(set)
    for index, bone in enumerate(bones):
        if bone.physicsBoneIndex >= 0:
            by_physics[bone.physicsBoneIndex].add(index)
    changes = []
    for model in phy.theSourcePhyPhysCollisionModels or []:
        candidates = by_physics.get(model.theIndex)
        if candidates and len(candidates) == 1:
            name = names[next(iter(candidates))]
        else:
            name = _resolve_reference(model.theName, originals, names, candidates)
        changes.append((model, 'theName', name))
        if model.theParentIsValid:
            constraint = (phy.theSourcePhyRagdollConstraintDescs or {}).get(model.theIndex)
            physical_parent = (by_physics.get(constraint.theParentIndex)
                               if constraint is not None and constraint.theChildIndex == model.theIndex else None)
            if physical_parent and len(physical_parent) == 1:
                changes.append((model, 'theParentName', names[next(iter(physical_parent))]))
                continue
            ancestors = None
            if candidates and len(candidates) == 1:
                ancestors = set(); parent = bones[next(iter(candidates))].parentBoneIndex
                while parent >= 0:
                    ancestors.add(parent); parent = bones[parent].parentBoneIndex
            parent_name = _resolve_reference(model.theParentName, originals, names, ancestors)
            changes.append((model, 'theParentName', parent_name))
    edit = phy.theSourcePhyEditParamsSection
    if edit is not None and edit.rootName:
        changes.append((edit, 'rootName', _resolve_reference(edit.rootName, originals, names)))
    for item, attribute, value in changes:
        setattr(item, attribute, value)


def validate_binary_include_bones(data, raw):
    """Reject links from renamed bones to unchanged compiled dependencies."""
    if not data.theBoneNameRemap:
        return
    affected = {data.theOriginalBoneNames[i].casefold() for i in data.theBoneNameRemap}
    aliases = {name.casefold() for name in data.theBoneNameRemap.values()}
    if len(raw) < 164:
        raise UnsupportedModelError()
    count, start = struct.unpack_from('<ii', raw, 156)
    if count < 0 or start < 0 or start > len(raw) or count > (len(raw) - start) // 216:
        raise UnsupportedModelError()
    for index in range(count):
        record = start + index * 216
        relative = struct.unpack_from('<i', raw, record)[0]
        address = record + relative
        if not relative:
            name = ''
        else:
            end = raw.find(b'\0', address) if 0 <= address < len(raw) else -1
            if end < 0:
                raise UnsupportedModelError()
            name = decode_ascii_bytes(raw[address:end])
        if name.casefold() in affected | aliases:
            raise UnsupportedModelError()
