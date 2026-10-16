"""Exact source touch curves for independently witnessed DELTA poses.

Only the internal native compile stage may call the append-only writer. The
reader remains tolerant; every fact used for writing is checked here.
"""
from pathlib import Path
import math
import os
import struct
import tempfile


def _fail(detail):
    raise ValueError('IK curve preservation: '+detail)


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail('invalid '+label)
    return value


def _skeleton(data):
    bones=data.theBones or []
    names=[bone.theName for bone in bones]
    if len(set(names))!=len(names):_fail('ambiguous skeleton')
    result=[]
    for bone in bones:
        parent=bone.parentBoneIndex
        if type(parent) is not int or not -1<=parent<len(bones):_fail('invalid bone parent')
        result.append([bone.theName,names[parent] if parent>=0 else None])
    return sorted(result)


def _identity(data,rule):
    chains=data.theIkChains or [];bones=data.theBones or []
    _integer(rule.chain,0,len(chains)-1,'chain')
    _integer(rule.bone,0,len(bones)-1,'target bone')
    links=chains[rule.chain].theLinks or []
    if not links:_fail('missing chain links')
    names=[]
    for link in links:
        _integer(link.boneIndex,0,len(bones)-1,'chain link')
        names.append(bones[link.boneIndex].theName)
    return {'type':rule.type,'chain':chains[rule.chain].theName,'links':names,
            'bone':bones[rule.bone].theName,'slot':rule.slot}


def _covered_frames(frames,start,ranges):
    if len(ranges)!=4 or not all(math.isfinite(x) and 0<=x<=1 for x in ranges):
        _fail('invalid influence range')
    low,high=ranges[0],ranges[3]
    result=[]
    for frame in range(frames):
        phase=frame/max(frames-1,1)
        if not ((low<=phase<=high) if low<=high else (phase>=low or phase<=high)):continue
        index=frame-start
        if index<0 and low>high:index+=max(frames-1,1)
        if index<0:continue
        result.append(index)
    if not result:_fail('no covered source frames')
    return result


def _validate_payload(item,frames):
    _integer(frames,1,1000000,'frame count')
    start=_integer(item['start'],0,frames-1,'error start')
    covered=_covered_frames(frames,start,item['range'])
    try:scale_bytes=bytes.fromhex(item['scale_bits'])
    except (TypeError,ValueError):_fail('invalid scale bits')
    if len(scale_bytes)!=24:_fail('invalid scale count')
    scales=struct.unpack('<6f',scale_bytes)
    if not all(math.isfinite(x) and x>=0 for x in scales):_fail('nonfinite/negative scale')
    channels=item['channels']
    if len(channels)!=6:_fail('invalid channel count')
    encoded=[]
    for channel,scale in zip(channels,scales):
        if channel is None:
            encoded.append(None);continue
        if not isinstance(channel,list) or not channel:_fail('missing RLE')
        values=[_integer(word,-32768,32767,'RLE word') for word in channel]
        index=0;total_frames=0
        while index<len(values):
            header=values[index]&65535;valid=header&255;total=header>>8
            if not 0<valid<=total or index+valid>=len(values):_fail('malformed RLE')
            total_frames+=total;index+=valid+1
        if max(covered)>=total_frames:_fail('uncovered source sample')
        # The native reader decodes frameCount entries, irrespective of start.
        if total_frames<frames:_fail('incomplete native RLE span')
        if any(not math.isfinite(word*scale) for word in values):_fail('nonfinite decoded value')
        encoded.append(struct.pack('<'+'h'*len(values),*values))
    offsets=[];body=bytearray()
    for channel in encoded:
        if channel is None:offsets.append(0);continue
        offset=36+len(body)
        _integer(offset,36,32767,'channel offset')
        offsets.append(offset);body.extend(channel)
    return scale_bytes+struct.pack('<6h',*offsets)+body


def _internal(data,animation):
    if data.version!=49:_fail('unsupported MDL version')
    if animation.animBlock or animation.animblockIkRuleOffset:_fail('external animation data')
    if not animation.flags&4:_fail('reference is not DELTA')
    if not math.isfinite(animation.fps) or animation.fps<=0:_fail('invalid FPS')
    if animation.ikRuleCount!=len(animation.theIkRules or []):_fail('incomplete rule table')


def capture_touch_curves(data,animation):
    """Capture serialized facts only for a descriptor proven by full poses."""
    _internal(data,animation)
    rules=[];seen=set();spans=[]
    size=data.fileSize
    for ordinal,rule in enumerate(animation.theIkRules or []):
        if rule.type!=1:continue
        offset=rule.theOffsetStart;end=rule.theOffsetEnd
        if not 80<=offset<end<=size or end-offset!=152:_fail('unrecorded physical rule span')
        spans.append((offset,end))
        if rule.ikErrorOffset or rule.compressedIkErrorOffset<=0:_fail('legacy/missing curve')
        error=rule.theCompressedIkError
        if error is None or len(error.offset)!=6 or len(error.theAnimValues)!=6:_fail('missing compressed data')
        header=error.theOffsetStart
        if header!=offset+rule.compressedIkErrorOffset or not 80<=header<=size-36:_fail('invalid compressed span')
        spans.append((header,header+36))
        item=_identity(data,rule)
        key=tuple((k,tuple(v) if isinstance(v,list) else v) for k,v in item.items())
        if key in seen:_fail('nonunique rule mapping')
        seen.add(key)
        item.update(ordinal=ordinal,start=rule.ikErrorIndexStart,
                    range=[rule.influenceStart,rule.influencePeak,rule.influenceTail,rule.influenceEnd],
                    scale_bits=struct.pack('<6f',*error.scale).hex(),channels=[])
        for index,channel in enumerate(error.theAnimValues):
            pointer=error.offset[index];span=error.theChannelSpans[index]
            if pointer==0 and channel is None:
                item['channels'].append(None);continue
            if pointer<36 or channel is None or span is None:_fail('invalid channel pointer')
            if span[0]!=header+pointer or not header+36<=span[0]<span[1]<=size or span[1]-span[0]!=2*len(channel):
                _fail('invalid channel span')
            item['channels'].append([word.value for word in channel])
            spans.append(span)
        _validate_payload(item,animation.frameCount)
        rules.append(item)
    if not rules:_fail('no touch rules')
    ordered=sorted(spans)
    if any(a[1]>b[0] for a,b in zip(ordered,ordered[1:])):_fail('overlapping curve spans')
    return {'version':1,'source':[data.theModelName,data.checksum,animation.theOffsetStart],
            'frames':animation.frameCount,'fps':animation.fps,'delta':bool(animation.flags&4),
            'rule_count':len(animation.theIkRules),'skeleton':_skeleton(data),'rules':rules}


def _read_model(path):
    from ..decompiler import MdlDecompiler,DecompileOptions
    from .mdl_data import SourceMdlFileData49
    dec=MdlDecompiler(DecompileOptions(handle_vmodel_includes=False))
    dec.mdl_file_data=SourceMdlFileData49();dec._read_mdl_file(str(path))
    return dec.mdl_file_data


def _plan(data,origins,qc=None):
    planned=None
    if qc is not None:
        from ..weapon_compile import _validated_qc_references
        planned=_validated_qc_references(data,qc)
    sequences={s.theName:s for s in data.theSequenceDescs or []}
    if len(sequences)!=len(data.theSequenceDescs or []):_fail('duplicate target sequence')
    destinations={};sources={};users={}
    for name,origin in origins.items():
        sequence=sequences.get(name)
        if sequence is None:_fail('missing target sequence '+name)
        indexes=sequence.theAnimDescIndexes or []
        if len(indexes)!=len(origin['references']):_fail('reference count '+name)
        for slot,(index,reference) in enumerate(zip(indexes,origin['references'])):
            _integer(index,0,len(data.theAnimationDescs)-1,'target reference')
            payload=reference.get('touch_curves') if origin['ik_guard'] else None
            users.setdefault(index,[]).append(payload)
            if payload is None:continue
            animation=data.theAnimationDescs[index];_internal(data,animation)
            # Replacement is not a repair mechanism for malformed native data.
            capture_touch_curves(data,animation)
            if payload.get('version')!=1 or payload['frames']!=animation.frameCount or payload['delta']!=bool(animation.flags&4):
                _fail('reference frames/FPS/DELTA mismatch')
            if not math.isfinite(payload['fps']) or payload['fps']<=0:_fail('invalid source FPS')
            if planned is None and payload['fps']!=animation.fps:_fail('unplanned reference FPS change')
            if planned is not None:
                reference_plan=planned[(name,slot)]
                if reference_plan['descriptor']!=index or reference_plan['frames']!=payload['frames']:
                    _fail('planned reference mismatch')
            if payload['skeleton']!=_skeleton(data):_fail('skeleton mismatch')
            if payload['rule_count']!=len(animation.theIkRules):_fail('rule count mismatch')
            source=tuple(payload['source'])
            if source in sources:
                previous_index,previous_payload=sources[source]
                if previous_payload!=payload:_fail('source copy payload conflict')
                if previous_index!=index and planned is None:_fail('nonunique source descriptor mapping without QC plan')
            sources[source]=(index,payload)
            if index in destinations and destinations[index]!=payload:_fail('shared source payload conflict')
            destinations[index]=payload
    for index in destinations:
        if any(item!=destinations[index] for item in users[index]):_fail('shared reference preservation conflict')
        if any(index in (sequence.theAnimDescIndexes or []) and name not in origins
               for name,sequence in sequences.items()):_fail('untracked shared target reference')
    plan=[]
    for index,payload in destinations.items():
        animation=data.theAnimationDescs[index]
        ordinals=[i for i,r in enumerate(animation.theIkRules) if r.type==1]
        if ordinals!=[r['ordinal'] for r in payload['rules']]:_fail('rule ordering mismatch')
        for item in payload['rules']:
            rule=animation.theIkRules[item['ordinal']]
            if any(item[k]!=v for k,v in _identity(data,rule).items()):_fail('rule linkage/order mismatch')
            if rule.ikErrorOffset or rule.compressedIkErrorOffset<=0 or rule.theCompressedIkError is None:_fail('missing/legacy target curve')
            if item['start']!=rule.ikErrorIndexStart:_fail('error start mismatch')
            tolerance=.5/max(animation.frameCount-1,1)+1e-6
            ranges=[rule.influenceStart,rule.influencePeak,rule.influenceTail,rule.influenceEnd]
            if not all(math.isfinite(v) for v in ranges) or any(abs(a-b)>tolerance for a,b in zip(item['range'],ranges)):_fail('phase mismatch')
            blob=_validate_payload(item,animation.frameCount)
            plan.append((index,item,rule,blob))
    return plan


def preserve_compiled_touch_curves(target_mdl,origins,*,staging_root,qc=None):
    """Append, reparse and validate before atomic replacement of private MDL."""
    if not origins or not any(o['ik_guard'] and any('touch_curves' in r for r in o['references']) for o in origins.values()):return []
    target=Path(target_mdl);root=Path(staging_root).resolve()
    if not root.name.startswith('.loka_compile_models_') or not target.resolve().is_relative_to(root) or target.is_symlink() or target.suffix.lower()!='.mdl':
        _fail('target is not private native compile staging')
    original=target.read_bytes()
    if len(original)<80 or len(original)>2147483647 or original[:4]!=b'IDST' or struct.unpack_from('<i',original,4)[0]!=49 or struct.unpack_from('<i',original,76)[0]!=len(original):
        _fail('unsupported MDL header/size')
    data=_read_model(target);plan=_plan(data,origins,qc);candidate=bytearray(original);patches=set();receipts=[]
    for index,item,rule,blob in plan:
        offset=rule.theOffsetStart;end=rule.theOffsetEnd
        if not 80<=offset<end<=len(original) or end-offset!=152:_fail('rule offset out of bounds')
        field=offset+60
        if field in patches:_fail('overlapping physical rule')
        patches.add(field)
        if struct.unpack_from('<i',original,field)[0]!=rule.compressedIkErrorOffset:_fail('physical pointer mismatch')
        old_header=offset+rule.compressedIkErrorOffset
        if old_header!=rule.theCompressedIkError.theOffsetStart or not 80<=old_header<=len(original)-36:_fail('target curve offset out of bounds')
        candidate.extend(b'\0'*((-len(candidate))%4));position=len(candidate)
        pointer=_integer(position-offset,1,2147483647,'rule-relative offset')
        if position+len(blob)>2147483647:_fail('MDL size overflow')
        candidate.extend(blob);struct.pack_into('<i',candidate,field,pointer)
        receipts.append({'descriptor':index,'rule':item['ordinal'],'rule_offset':offset,'payload_offset':position,'bytes':len(blob),'frames':data.theAnimationDescs[index].frameCount})
    struct.pack_into('<i',candidate,76,len(candidate))
    fd,temporary=tempfile.mkstemp(prefix='.curve_verify_',suffix='.mdl',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as handle:handle.write(candidate);handle.flush();os.fsync(handle.fileno())
        restored=_read_model(temporary)
        for index,item,rule,blob in _plan(restored,origins,qc):
            actual=capture_touch_curves(restored,restored.theAnimationDescs[index])
            found=next(x for x in actual['rules'] if x['ordinal']==item['ordinal'])
            if found['scale_bits']!=item['scale_bits'] or found['channels']!=item['channels']:_fail('exact curve readback mismatch')
        from ..weapon_compile import validate_animation_semantics
        validate_animation_semantics(restored,origins)
        # Byte identity outside the explicit pointer/size fields proves raw
        # animation, geometry, checksums and every other rule field untouched.
        allowed=set(range(76,80))|{p+i for p in patches for i in range(4)}
        if any(a!=b and i not in allowed for i,(a,b) in enumerate(zip(original,candidate))):_fail('unexpected MDL mutation')
        if target.read_bytes()!=original:_fail('staged target changed during verification')
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return receipts
