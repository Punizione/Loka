"""Explicit conversion records for read-only composed-reload preview bases."""
from dataclasses import dataclass
import json
import math


@dataclass(frozen=True)
class ReloadPreviewStage:
    sequence_index: int
    start: float
    end: float
    source_activity: str


def build_reload_previews(data,rows,origins,warn):
    sequences=data.theSequenceDescs or [];animations=data.theAnimationDescs or []
    if not isinstance(rows,(list,tuple)) or not isinstance(origins,dict):return {}
    names={s.theName:i for i,s in enumerate(sequences)}
    if len(names)!=len(sequences) or len(rows)!=len(sequences):return {}
    if any(not isinstance(row,dict) or row.get('name')!=s.theName for row,s in zip(rows,sequences)):return {}
    records={}
    for index,row in enumerate(rows):
        if not row.get('reload_parts'):continue
        try:
            parent=sequences[index];parts=row['reload_parts'];helpers=row['reload_helpers']
            expected=origins[row['name']]['reload_layers'];layers=parent.theAutoLayers or []
            if not(len(parts)==len(helpers)==len(expected)==len(layers)>0):raise ValueError('layer count')
            refs=parent.theAnimDescIndexes or []
            if len(refs)!=1 or parent.flags&20:raise ValueError('parent timeline')
            animation=animations[refs[0]]
            if animation.frameCount!=row['source']['frame_count'] or animation.theIkRules:raise ValueError('parent reference')
            weights=parent.theBoneWeights
            if weights is None or len(weights)!=len(data.theBones or []) or any(not math.isfinite(w) or abs(w)>1e-6 for w in weights):
                raise ValueError('parent weights')
            stages=[];previous=0.
            for part,name,origin,layer in zip(parts,helpers,expected,layers):
                child_index=names[name];child=sequences[child_index];child_row=rows[child_index]
                if (not child_row.get('reload_helper') or child.theActivityName
                        or child_row['source']['name']!=part['name']
                        or child_row['source']['activity']!=part['activity']):raise ValueError('helper identity')
                if child.flags&20!=origins[name]['delta_post']:raise ValueError('helper semantics')
                if animations[child.theAnimDescIndexes[0]].frameCount!=part['frame_count']:raise ValueError('helper frames')
                start=float(origin['start']);end=float(origin['end'])
                if (origin['name']!=name or layer.iSequence!=child_index or layer.flags!=0
                        or not math.isfinite(start) or not math.isfinite(end)
                        or start<0 or not start<end<=1.01 or abs(start-previous)>1e-5
                        or any(not math.isfinite(getattr(layer,key)) or abs(getattr(layer,key)-value)>1e-5 for key,value in
                               (('start',start),('peak',start),('tail',end),('end',end)))):
                    raise ValueError('helper window')
                activity=part['activity'] or parent.theActivityName
                if not isinstance(activity,str):raise ValueError('helper activity')
                stages.append(ReloadPreviewStage(child_index,layer.start,layer.end,activity));previous=end
            if previous<1:raise ValueError('incomplete timeline')
            records[index]=tuple(stages)
        except (KeyError,IndexError,TypeError,ValueError,AttributeError,OverflowError) as error:
            warn('Composed reload preview record does not match the model; keeping existing playback: '+str(row.get('name',''))+' / '+str(error))
    return records


def configure_session_reload_preview(session,model_index,preview):
    """Only retained shotgun conversions opt in; raw/standalone models do not."""
    from .weapon_catalog import SHOTGUN_IDS
    setter=getattr(preview,'set_reload_previews',None)
    if not callable(setter):return
    setter((),{})
    result=getattr(session,'result',None)
    paths=getattr(result,'sequence_plan_files',())
    origin_path=getattr(result,'animation_semantics_file',None)
    if not paths or not origin_path:return
    try:
        from .action_session import owned_path
        item=session.models[model_index]
        if item.profile_id in SHOTGUN_IDS or item.qc_path is None:return
        origins=json.loads(owned_path(origin_path,session.work_dir).read_text(encoding='utf-8'))
        targets=[]
        for path in paths:
            plan=json.loads(owned_path(path,session.work_dir).read_text(encoding='utf-8'))
            if plan.get('source_profile') not in SHOTGUN_IDS:continue
            targets.extend(t for t in plan['targets'] if t['target_profile']==item.profile_id)
        if len(targets)!=1:return
        setter(targets[0]['sequences'],origins.get(str(item.qc_path),{}))
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as error:
        preview._warn('Composed reload preview metadata is unavailable; keeping existing playback: '+str(error))
