"""Ordered shotgun reload recipes; original exports remain read-only."""
from dataclasses import replace
import re


def _role(source,profile,index):
    from .weapon_sequences import _classify
    action,layer,unused=_classify(source,profile,index)
    activity=source.activity or (profile.slots[index].activity if index<len(profile.slots) else '')
    match=re.fullmatch(r'(.+_VM_)RELOAD(?:_EMPTY|_LOOP|_END)?(?:_SNIPER)?(?:_LAYER)?',activity.upper())
    return action,layer,unused,match[1] if match else None


def compose_reload_plan(plan,sources,source_id,target_id,count):
    from .weapon_catalog import SHOTGUN_IDS
    from .weapon_sequences import get_profile,_timing,target_timing_slot
    if source_id not in SHOTGUN_IDS or target_id in SHOTGUN_IDS:return plan
    profile=get_profile(source_id);target=get_profile(target_id)
    classified=[_role(s,profile,i) for i,s in enumerate(sources)]
    by_name={s.name.lower():i for i,s in enumerate(sources)}
    excluded={n.lower() for n in plan.excluded_sequences}
    warnings=list(plan.warnings);used_parts=set();result=[]
    for row in plan.sequences:
        if row.source is None or row.source.name.lower() not in by_name:
            result.append(row);continue
        action,layer,unused,domain=classified[by_name[row.source.name.lower()]]
        if unused or action not in ('reload','reload_empty') or domain is None:
            result.append(row);continue
        parts=[row.source]
        for semantic,repetitions in (('reload_loop',count),('reload_end',1)):
            if not repetitions:continue
            found=None
            for wanted_domain in dict.fromkeys((domain,'ACT_VM_')):
                for wanted_layer in (layer,not layer):
                    found=next((s for s,role in zip(sources,classified)
                                if role==(semantic,wanted_layer,False,wanted_domain)
                                and s.name.lower() not in excluded),None)
                    if found is not None:break
                if found is not None:break
            if found is None:raise ValueError('拼接换弹缺少 '+semantic.upper()+'：'+row.source.name)
            if classified[by_name[found.name.lower()]][1:]!=(layer,False,domain):
                notice=f'拼接换弹 {row.source.name} 的 {semantic.upper()} 复用 {found.name}。'
                if notice not in warnings:warnings.append(notice)
            parts.extend([found]*repetitions);used_parts.add(found.name.lower())
        rate=row.source.fps
        frames=sum(max(2 if s.frame_count>1 else 1,round((s.frame_count-1)*rate/s.fps)+1) for s in parts)
        if frames>65535:raise ValueError('拼接换弹帧数过多，请减少 Loop 次数：'+row.source.name)
        source=replace(row.source,frame_count=frames,fps=rate,loop=False,
                       animation_references=tuple(replace(a,frame_count=frames,fps=rate)
                                                  for a in row.source.animation_references))
        slot=target.slots[row.official_index] if row.official_index is not None else None
        if slot is None:
            slot=next((s for s in target.slots if s.semantic==action and s.layer==layer and not s.unused),None)
        if slot is None and action=='reload_empty':
            slot=next((s for s in target.slots if s.semantic=='reload' and s.layer==layer and not s.unused),None)
        if slot is not None:slot=target_timing_slot(target,slot)
        if slot is not None:
            notice=f'额外动作 {row.source.name} 没有目标官方时间基准，保留原 FPS。'
            warnings=[w for w in warnings if w!=notice]
        fps,_=_timing(source,slot) if slot is not None else (rate,None)
        result.append(replace(row,source=source,fps=float(fps),reload_parts=tuple(parts),
                              source_reference_index=None))
    # Consumed loop/end clips need no independent Activity on a magazine weapon.
    # Keep symbols that a surviving layer actually references, without publishing
    # their shotgun-only Activity as an extra playable action.
    dependencies={n.lower() for row in result if row.source for n in row.source.dependencies}
    result=[replace(row,activity='') if row.official_index is None and row.source and row.source.name.lower() in used_parts and row.source.name.lower() in dependencies
            else row for row in result if not(row.official_index is None and row.source and
                row.source.name.lower() in used_parts and row.source.name.lower() not in dependencies)]
    from .weapon_sequences import PlannedSequence
    originals={s.name.lower():s for s in sources};reserved={r.name.lower() for r in result}
    helpers=[];composed=[]
    for index,row in enumerate(result):
        if not row.reload_parts:
            composed.append(row);continue
        roots=[];cached={}
        for pi,part in enumerate(row.reload_parts):
            if part.name.lower() in cached:
                roots.append(cached[part.name.lower()]);continue
            local={};pending=[part];ordered=[]
            while pending:
                source=pending.pop();key=source.name.lower()
                if key in local:continue
                name=f'loka_reload_{index}_p{pi}_s{len(local)}'
                while name.lower() in reserved:name+='x'
                reserved.add(name.lower());local[key]=name;ordered.append(source)
                for dependency in source.dependencies:
                    child=originals.get(dependency.lower())
                    if child is None:raise ValueError('拼接换弹缺少附加层：'+dependency)
                    pending.append(child)
            roots.append(local[part.name.lower()])
            cached[part.name.lower()]=roots[-1]
            for source in ordered:
                helpers.append(PlannedSequence(local[source.name.lower()],'',source,source.fps,None,
                    False,None,preserve_source_fps=True,reload_helper=True,
                    source_sequence_map=tuple(local.items())))
        composed.append(replace(row,reload_helpers=tuple(roots)))
    return replace(plan,sequences=tuple(composed+helpers),warnings=tuple(warnings))


def reload_windows(row):
    """QC frame windows, with a small inclusive terminal endpoint."""
    durations=[(s.frame_count-1)/s.fps for s in row.reload_parts]
    total=sum(durations)
    if total<=0:durations=[1.0 for s in durations];total=sum(durations)
    else:
        # A static segment still gets one sample interval rather than dividing
        # by zero or silently disappearing from the recipe.
        durations=[d if d>0 else 1/row.source.fps for d in durations];total=sum(durations)
    elapsed=0.;windows=[]
    for i,(helper,duration) in enumerate(zip(row.reload_helpers,durations)):
        begin=elapsed/total*(row.source.frame_count-1);end=(elapsed+duration)/total*(row.source.frame_count-1)
        windows.append((helper,begin,end+(.001 if i==len(durations)-1 else 0),elapsed/total,duration/total))
        elapsed+=duration
    return windows


def _transition_options(command):
    from .weapon_qc import _body_tokens
    tokens=_body_tokens(command.text);values={}
    arities={'fadein':1,'fadeout':1,'snap':0,'node':1,'transition':2,'rtransition':2,'exitphase':1}
    for i,t in enumerate(tokens):
        if not t.quoted and t.value.lower() in arities:
            key=t.value.lower();end=tokens[i+arities[key]].end
            values[key]=command.text[t.start:end]
    return values


def materialize_reload(row,index,sources,profile,variants,resources):
    """Phase-gate native sequence helpers; retain their own IK, masks and layers."""
    from .weapon_qc import (_quote,_tokens,_event_values,_placeholder,
                            _timer_slot,_activity_weight)
    first_doc,first_command=sources[row.reload_parts[0].name.lower()]
    from .weapon_qc import _leading_refs
    ref=_leading_refs(first_command.text)[0]
    named=first_doc.animations.get(ref.value.lower())
    physical=_tokens(named.text)[2].value if named else ref.value
    source=(first_doc.path.parent/physical.replace('\\','/')).resolve()
    if not source.is_relative_to(first_doc.path.parent):raise ValueError('拼接换弹动画路径越界')
    resource=f'loka_reload_{index}_timeline.smd'
    resources[resource]=_placeholder(source)
    name=f'loka_reload_{index}_timeline'
    while name.lower() in sources or name in variants:name+='x'
    variants[name]=f'$animation "{name}" "{resource}" {{\n numframes {row.source.frame_count}\n fps {float(row.fps):.9g}\n weightlist "loka_reload_zero_weights"\n noautoik\n}}\n'
    text='$sequence '+_quote(row.name)+' {\n '+_quote(name)+'\n'
    transitions=_transition_options(first_command)
    last_options=_transition_options(sources[row.reload_parts[-1].name.lower()][1])
    if 'fadeout' in last_options:transitions['fadeout']=last_options['fadeout']
    text+='\n'.join(transitions.values())+'\n'
    text+=' activity '+_quote(row.activity)+' '+_activity_weight(first_command.text)+'\n'
    events=[];previous_endpoint=False
    for part,(helper,begin,end,start_cycle,duration_cycle) in zip(row.reload_parts,reload_windows(row)):
        text+=f' blendlayer "{helper}" {begin:.12g} {begin:.12g} {end:.12g} {end:.12g}\n'
        doc,command=sources[part.name.lower()]
        clip_events=_event_values(command.text)
        for ei,event in enumerate(clip_events):
            phase=float(event[1])/max(part.frame_count-1,1)
            frame=round((start_cycle+duration_cycle*phase)*(row.source.frame_count-1))
            item=(event[0],str(frame),*event[2:])
            boundary=ei==0 and previous_endpoint and float(event[1])==0
            if not boundary or not events or item!=events[-1]:events.append(item)
        previous_endpoint=bool(clip_events and float(clip_events[-1][1])==part.frame_count-1)
    slot=profile.slots[row.official_index] if row.official_index is not None else None
    if slot is not None and _timer_slot(slot,profile):events=[]
    for event in events:
        text+=' { event '+event[0]+' '+event[1]+''.join(' '+_quote(x) for x in event[2:])+' }\n'
    text+='}\n'
    return dict(text=text,fps=float(row.fps),write_fps=True,
                refs=[dict(name=name,fps=float(row.fps),frames=row.source.frame_count,options={'numframes':row.source.frame_count})],
                options={},events=tuple(events))
