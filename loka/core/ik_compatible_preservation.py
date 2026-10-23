"""Optional original touch curves; rejected candidates keep native output intact."""
import struct

from .ik_curve_preservation import capture_touch_curves, preserve_compiled_touch_curves, _read_model


# Data/IO failures invalidate only the optional restoration. Cancellation and
# resource exhaustion are deliberately not swallowed.
_UNAVAILABLE=(ValueError,OSError,OverflowError,struct.error,IndexError,TypeError,AttributeError)


def capture_compatible_touch_curve(data,animation):
    """Return a payload or a diagnostic, independently of numerical thresholds."""
    try:
        if animation.flags&4 and not getattr(animation,'thePreserveSourceTouchCurves',False):
            raise ValueError('DELTA reference has no verified full-pose reconstruction')
        return capture_touch_curves(data,animation,require_delta=False),None
    except _UNAVAILABLE as error:
        return None,str(error)


def preserve_compatible_touch_curves(target_mdl,origins,*,staging_root,qc=None):
    """Restore independent shared-descriptor groups, retaining native fallbacks.

    The existing writer validates a candidate before atomic replacement. Each
    call contains one group, so a failed group cannot discard a successful one
    or partially replace either hand's constraints.
    """
    receipts=[];skipped=[]
    if not origins:return receipts,skipped
    for name,origin in origins.items():
        for slot,ref in enumerate(origin['references']):
            if ref.get('touch_curve_skip_reason'):
                skipped.append({'sequence':name,'reference':slot,'reason':ref['touch_curve_skip_reason']})
    if not any('touch_curves' in ref for origin in origins.values() for ref in origin['references']):
        return receipts,skipped
    try:
        data=_read_model(target_mdl)
        sequences={s.theName:s for s in data.theSequenceDescs or []}
        groups={}
        for name,origin in origins.items():
            indexes=sequences[name].theAnimDescIndexes
            if len(indexes)!=len(origin['references']):raise ValueError('reference mapping changed: '+name)
            for slot,(index,ref) in enumerate(zip(indexes,origin['references'])):
                if ref.get('touch_curves') is not None:groups.setdefault(index,[]).append((name,slot))
    except _UNAVAILABLE+(KeyError,) as error:
        skipped.append({'reason':str(error)})
        return receipts,skipped
    for index,members in groups.items():
        # Payloads are read-only. Do not repeatedly copy every RLE sample in
        # a large animation library just to select one descriptor group.
        candidate={}
        selected=set(members)
        for name,origin in origins.items():
            row=dict(origin);row['references']=[]
            for slot,ref in enumerate(origin['references']):
                copied=dict(ref)
                if (name,slot) not in selected:copied.pop('touch_curves',None)
                row['references'].append(copied)
            candidate[name]=row
        try:
            restored=preserve_compiled_touch_curves(target_mdl,candidate,staging_root=staging_root,
                                                   qc=qc,require_delta=False)
            receipts.extend(restored)
        except _UNAVAILABLE+(KeyError,) as error:
            skipped.append({'descriptor':index,'reason':str(error)})
    return receipts,skipped
