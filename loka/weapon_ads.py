"""Known Necola ADS activities and precise QC layer-reference removal."""
from functools import lru_cache
from pathlib import Path
import json
import math


@lru_cache(maxsize=1)
def ads_activities():
    data=json.loads((Path(__file__).parent/'data/weapon_ads.json').read_text(encoding='utf-8'))
    return frozenset(data['activities'])


def is_ads_activity(activity):
    return activity.upper() in ads_activities()


def is_ads_or_mixed_activity(activity):
    return is_ads_activity(activity) or activity.upper().startswith('MIXED_ACT_')


def remove_ads_layers(text,removed_names):
    # Lazy import avoids coupling Activity classification to the QC/planner imports.
    from .weapon_qc import _body_tokens,_patch
    removed_names={name.lower() for name in removed_names}
    if not removed_names:return text,[]
    tokens=_body_tokens(text);changes=[];removed=[];i=0
    while i<len(tokens):
        token=tokens[i];key=token.value.lower()
        if token.quoted or key not in ('addlayer','blendlayer'):
            i+=1;continue
        if i+1>=len(tokens):raise ValueError('QC layer 缺少引用')
        reference=tokens[i+1]
        if reference.value.lower() not in removed_names:
            i+=2;continue
        last=i+1
        if key=='blendlayer':
            if last+4>=len(tokens):raise ValueError('QC blendlayer 缺少四个范围参数')
            for number in tokens[last+1:last+5]:
                try:value=float(number.value)
                except ValueError as error:raise ValueError('QC blendlayer 范围参数无效') from error
                if not math.isfinite(value):raise ValueError('QC blendlayer 范围参数无效')
            last+=4
        while last+1<len(tokens) and not tokens[last+1].quoted:
            option=tokens[last+1].value.lower()
            if option in ('xfade','spline','noblend','local'):last+=1
            elif option=='poseparameter':
                if last+2>=len(tokens):raise ValueError('QC layer poseparameter 缺少名称')
                last+=2
            else:break
        changes.append((token.start,tokens[last].end,''))
        removed.append({'directive':key,'source_sequence':reference.value})
        i=last+1
    return _patch(text,changes),removed
