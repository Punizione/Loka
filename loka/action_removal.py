"""Remove extra destination slots without dropping retained QC dependencies."""
from dataclasses import dataclass
from .action_rules import _commands,_body,_refs,_options,_patch


@dataclass(frozen=True)
class SequenceSlot:
    index: int
    name: str
    activity: str
    official_index: int | None

    @property
    def display_name(self):return self.name

    @property
    def removable(self):return self.official_index is None and bool(self.activity.strip())


def qc_slots(document,indexes):
    return tuple(SequenceSlot(i,c.name,_options(c).get('activity',('',0))[0],indexes.get(i))
                 for i,c in enumerate(document.sequences))


def compiled_slots(data,profile_id):
    from .weapon_sequences import get_profile
    profile=get_profile(profile_id)
    return tuple(SequenceSlot(i,s.theName,s.theActivityName,next((p.index for p in profile.slots
        if p.name.lower()==s.theName.lower() and p.activity==s.theActivityName),None))
        for i,s in enumerate(data.theSequenceDescs or []))


def prune_sequences(text,removed,official_indexes):
    commands=_commands(text);sequences=[c for c in commands if c.kind=='sequence']
    for i in removed:
        if type(i) is not int or not 0<=i<len(sequences):raise ValueError('剔除 sequence 顺位无效。')
        if i not in official_indexes or official_indexes[i] is not None:
            raise ValueError('不能剔除官方动作槽位：'+sequences[i].name)
        if not _options(sequences[i]).get('activity',('',0))[0].strip():
            raise ValueError('不能剔除未声明明确 Activity 的序列：'+sequences[i].name)
    deleted={sequences[i].name.lower() for i in removed}
    removed_commands={id(sequences[i]) for i in removed}
    mapping={old:new for new,old in enumerate(i for i in range(len(sequences)) if i not in removed)}
    if not removed:return text,mapping
    symbols={c.name.lower() for c in commands if c.kind in ('animation','sequence')}
    explicit={c.name.lower() for c in commands if c.kind=='animation'}
    for i in removed:
        alias='@'+sequences[i].name.lower()
        if alias not in explicit and any(t.value.lower() not in symbols for t in _refs(sequences[i])):
            deleted.add(alias)
    references={name:1 for name in ('addlayer','blendlayer','subtract','presubtract','blendref','blendcomp','blendcenter',
                                   'alignto','align','match','matchblend','worldspaceblend','worldspaceblendloop')}
    references.update({name:2 for name in ('alignboneto','alignbone','walkalignto','walkalign')})
    blockers=[]
    for c in commands:
        if c.kind not in ('sequence','animation') or id(c) in removed_commands:continue
        refs=[t.value for t in _refs(c)]
        tokens=_body(c)
        for i,t in enumerate(tokens):
            offset=references.get(t.value.lower()) if not t.quoted else None
            if offset is not None and i+offset<len(tokens):refs.append(tokens[i+offset].value)
        for ref in refs:
            if ref.lower() in deleted:blockers.append(c.name+' → '+ref)
    if blockers:raise ValueError('待剔除序列仍被保留内容引用：'+'；'.join(dict.fromkeys(blockers)))
    return _patch(text,[(sequences[i].start,sequences[i].end,'') for i in removed]),mapping


def stage_removals(session,model_index,indices):
    from .action_swaps import permute_qc
    if type(model_index) is not int or not 0<=model_index<len(session.models):raise ValueError('无效的模型顺位。')
    model=session.models[model_index];indices=set(indices)
    slots=model.sequence_slots
    for i in indices:
        if type(i) is not int or not 0<=i<len(slots):raise ValueError('剔除 sequence 顺位无效。')
        if slots[i].official_index is not None:raise ValueError('不能剔除官方动作槽位：'+slots[i].name)
        if not slots[i].removable:raise ValueError('不能剔除未声明明确 Activity 的序列：'+slots[i].name)
    if model.qc_path is not None:
        assignments={d:s for (m,d),s in session.assignments.items() if m==model_index}
        text=permute_qc(model.qc_path,assignments,model.profile_id)
        prune_sequences(text,indices,{s.index:s.official_index for s in slots})
    else:
        # Compiled standalone dependencies are checked again against exported QC
        # during save. This early check covers local sequence auto-layers.
        preview=session._preview_models.get(model_index)
        if preview is not None:
            for slot in slots:
                if slot.index in indices:continue
                donor=session.source_index(model_index,slot.index)
                for layer in preview.data.theSequenceDescs[donor].theAutoLayers or []:
                    if layer.iSequence in indices:raise ValueError('待剔除序列仍被保留内容引用：'+slot.name)
    session.removed_sequences.difference_update({key for key in session.removed_sequences if key[0]==model_index})
    session.removed_sequences.update((model_index,i) for i in indices)
