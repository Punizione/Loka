"""Optional QC evidence for the default base of additive idle transitions.

Compiled MDLs do not retain subtract commands. Only a matching retained QC can
provide this evidence; missing/ambiguous declarations leave playback unchanged.
"""
from .action_rules import _QcDocument, _body, _refs


def _subtract(command):
    tokens=_body(command);declarations=[]
    for i,token in enumerate(tokens):
        if token.quoted:continue
        if token.value.lower()=='presubtract':raise ValueError('presubtract is not supported')
        if token.value.lower()!='subtract':continue
        if i+2>=len(tokens):raise ValueError('incomplete subtract')
        declarations.append((tokens[i+1].value.lower(),int(tokens[i+2].value)))
    if len(declarations)>1:raise ValueError('multiple subtract commands')
    return declarations


def build_qc_preview_bases(data,qc_path,warn):
    if qc_path is None:return {}
    document=_QcDocument(qc_path)
    sequences=data.theSequenceDescs or [];animations=data.theAnimationDescs or []
    if (len(document.sequences)!=len(sequences) or any(command.name!=s.theName
            for command,s in zip(document.sequences,sequences))):
        raise ValueError('QC sequence identity does not match the model')
    compiled_names={}
    for i,a in enumerate(animations):compiled_names.setdefault(a.theName.lower(),[]).append(i)
    records={}
    for index,(command,s) in enumerate(zip(document.sequences,sequences)):
        # Other delta actions retain their Activity/gameplay base policy.
        suffix=s.theActivityName.upper().partition('_VM_')[2]
        if not s.flags&4 or suffix not in ('IDLE_TO_LOWERED','LOWERED_TO_IDLE'):continue
        try:
            refs=_refs(command);indexes=s.theAnimDescIndexes or []
            if len(refs)!=1 or len(indexes)!=1:raise ValueError('transition blend is ambiguous')
            ref=refs[0].value.lower();animation=document.animations.get(ref)
            current=animations[indexes[0]]
            if animation is not None and current.theName.lower()!=ref:
                raise ValueError('transition animation identity mismatch')
            declarations=_subtract(command)+(_subtract(animation) if animation else [])
            if not declarations:continue
            if len(declarations)!=1:raise ValueError('subtract on both sequence and animation')
            name,frame=declarations[0]
            # A compiler may strip unreferenced animation symbols. Do not try
            # to recover those poses by name heuristics or by reading SMDs.
            if name in document.animations:
                matches=compiled_names.get(name,[])
                if len(matches)!=1:raise ValueError('subtract animation is unavailable or ambiguous')
                base_index=matches[0]
            else:
                candidates=[i for i,c in enumerate(document.sequences) if c.name.lower()==name]
                if len(candidates)!=1:raise ValueError('subtract symbol is unavailable')
                base_sequence=sequences[candidates[0]];base_command=document.sequences[candidates[0]]
                base_refs=_refs(base_command);base_indexes=base_sequence.theAnimDescIndexes or []
                if len(base_refs)!=1 or len(base_indexes)!=1 or base_sequence.flags&4:
                    raise ValueError('subtract sequence is relative or ambiguous')
                base_index=base_indexes[0]
                expected=(base_refs[0].value.lower() if base_refs[0].value.lower() in document.animations
                          else '@'+base_command.name.lower())
                if animations[base_index].theName.lower()!=expected:
                    raise ValueError('subtract sequence animation identity mismatch')
            base=animations[base_index]
            if base.flags&4 or not 0<=frame<base.frameCount:
                raise ValueError('subtract base is relative or its frame is invalid')
            records[index]=(base_index,frame)
        except (ValueError,IndexError,TypeError,AttributeError) as error:
            warn('QC transition base is unverified; keeping existing playback: '+command.name+' / '+str(error))
    return records


def configure_session_qc_preview(session,model_index,preview):
    setter=getattr(preview,'set_qc_preview_bases',None)
    if not callable(setter):return
    setter(None)
    item=session.models[model_index]
    if item.qc_path is None:return
    from .action_session import owned_path
    try:
        qc=owned_path(item.qc_path,session.work_dir)
        setter(qc)
    except (OSError,ValueError) as error:
        preview._warn('QC transition base is unavailable; keeping existing playback: '+str(error))
