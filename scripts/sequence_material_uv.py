"""Authoring-only SMD connectivity and UV-to-model coordinate rasterization."""
from __future__ import annotations

from collections import defaultdict, deque
import hashlib
from pathlib import Path

import numpy as np

SMD_BINDINGS = {
    'lam':('lam2.smd','Dev_LAM_PEQ25_005_MECHREVODF_vk_Specail2'),
    'weal':('weal2.smd','Dev_Weal_StrobeLight_006_MECHREVODF_vk_Specail2'),
    'fros':('fros2.smd','FroS_AM17_045_MECHREVODF_vk_Specail2'),
    'han':('han2.smd','Han_AM17_long_160_MECHREVODF_vk_Specail2'),
    'muz':('muz2.smd','Muz_AK_PBS_04_012_MECHREVODF_vk_Specail2'),
    'rec':('rec2.smd','Rec_SMG_AM17_066_MECHREVODF_vk_012'),
    'rec_reuse':('rec2.smd','Rec_SMG_AM17_066_MECHREVODF_vk_01_Reuse2'),
    'box':('zhuan2.smd','MI_props_box_AM17_MECHREVODF_vk_1P2'),
}


def read_smd_uv(smd_dir, part):
    filename,material = SMD_BINDINGS[part]
    path = Path(smd_dir)/filename
    triangles=[]
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip()=='triangles':break
        else:raise ValueError('SMD has no triangles: '+filename)
        for line in stream:
            name=line.strip().strip('"')
            if name=='end':break
            if not name:continue
            vertices=[]
            for _ in range(3):
                fields=next(stream).split()
                if len(fields)<9:raise ValueError('Invalid SMD triangle vertex')
                vertex=tuple(map(float,fields[1:4]+fields[7:9]))
                if not all(np.isfinite(vertex)):raise ValueError('Non-finite SMD coordinate')
                vertices.append(vertex)
            if name.casefold()==material.casefold():triangles.append(vertices)
    if not triangles:raise ValueError('Missing exact SMD material: '+material)
    parent=list(range(len(triangles)))
    def find(value):
        while parent[value]!=value:
            parent[value]=parent[parent[value]];value=parent[value]
        return value
    edges={}
    for index,triangle in enumerate(triangles):
        keys=[tuple(round(c,5 if k<3 else 6) for k,c in enumerate(vertex)) for vertex in triangle]
        for a,b in ((0,1),(1,2),(2,0)):
            edge=tuple(sorted((keys[a],keys[b])))
            if edge in edges:
                left,right=find(index),find(edges[edge])
                if left!=right:parent[left]=right
            else:edges[edge]=index
    roots={}
    islands=[]
    for index in range(len(triangles)):
        root=find(index)
        if root not in roots:roots[root]=len(roots)+1
        islands.append(roots[root])
    return np.asarray(triangles,dtype=np.float64),np.asarray(islands,dtype=np.uint32),{
        'file':filename,'material':material,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'triangles':len(triangles),'uv_islands':len(roots)}


def rasterize_smd(triangles,islands,size):
    labels=np.zeros((size,size),dtype=np.uint32)
    positions=np.zeros((size,size,3),dtype=np.float32)
    overlap=np.zeros((size,size),dtype=bool)
    for triangle,island in zip(triangles,islands):
        uv=triangle[:,3:5].copy()
        uv[:,0]*=size;uv[:,1]=(1-uv[:,1])*size
        low=np.maximum(np.floor(uv.min(axis=0)-.5).astype(int),0)
        high=np.minimum(np.ceil(uv.max(axis=0)-.5).astype(int),size-1)
        if np.any(low>high):continue
        a,b,c=uv
        denominator=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(denominator)<1e-10:continue
        yy,xx=np.mgrid[low[1]:high[1]+1,low[0]:high[0]+1]
        xx=xx+.5;yy=yy+.5
        wa=((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/denominator
        wb=((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/denominator
        wc=1-wa-wb
        inside=(wa>=-1e-6)&(wb>=-1e-6)&(wc>=-1e-6)
        view=labels[low[1]:high[1]+1,low[0]:high[0]+1]
        overlap[low[1]:high[1]+1,low[0]:high[0]+1]|=inside&(view!=0)&(view!=island)
        view[inside]=island
        xyz=wa[:,:,None]*triangle[0,:3]+wb[:,:,None]*triangle[1,:3]+wc[:,:,None]*triangle[2,:3]
        positions[low[1]:high[1]+1,low[0]:high[0]+1][inside]=xyz[inside]
    covered=labels!=0
    return labels,positions,{'covered_pixels':int(covered.sum()),'overlapped_pixels':int(overlap.sum()),
                             'coverage_fraction':float(covered.mean()),'used_islands':int(len(np.unique(labels[covered])))}


def fit_scan_axis(positions,labels,phases,frames):
    """Learn model-space arrival coordinate from authored material onset."""
    stage=np.frombuffer(b''.join(phases),dtype=np.uint8).reshape(frames,-1)
    material=stage==3
    # Persistent suffix avoids early isolated material-colored/wire pixels.
    settled=np.flip(np.cumprod(np.flip(material,axis=0),axis=0),axis=0).astype(bool)
    arrival=np.where(settled.any(axis=0),settled.argmax(axis=0),frames-1).astype(np.float32)
    xyz=positions.reshape(-1,3)
    valid=(labels.ravel()>0)&(arrival>0)&(arrival<frames-1)
    if valid.sum()<32:valid=labels.ravel()>0
    sample=np.flatnonzero(valid)[::max(1,int(valid.sum())//20000)]
    design=np.column_stack((xyz[sample],np.ones(len(sample))))
    target=arrival[sample]
    weights=np.ones(len(sample))
    coefficient=np.zeros(4)
    for _ in range(4):
        coefficient=np.linalg.lstsq(design*weights[:,None],target*weights,rcond=None)[0]
        error=np.abs(design@coefficient-target)
        weights=np.minimum(1,1.5/np.maximum(error,1e-6))
    axis=coefficient[:3].copy()
    norm=np.linalg.norm(axis)
    if norm<1e-8:axis=np.array([0.,0.,1.])
    else:axis/=norm
    coordinate=xyz@axis
    errors=np.abs(design@coefficient-target)
    return coordinate.reshape(labels.shape),{'axis':[float(v) for v in axis],
        'arrival_fit_median_frames':float(np.median(errors)),
        'arrival_fit_p90_frames':float(np.quantile(errors,.9))}


def _robust_plane(design,target):
    weights=np.ones(len(target))
    for _ in range(3):
        coefficients=np.linalg.lstsq(design*weights[:,None],target*weights,rcond=None)[0]
        errors=np.abs(design@coefficients-target)
        weights=np.minimum(1,1/np.maximum(errors,1e-6))
    return coefficients


def build_island_atlas(triangles,islands,size,phases,frames):
    labels,positions,coverage=rasterize_smd(triangles,islands,size)
    model_coordinate,global_fit=fit_scan_axis(positions,labels,phases,frames)
    stage=np.frombuffer(b''.join(phases),dtype=np.uint8).reshape(frames,-1)
    material=stage==3
    settled=np.flip(np.cumprod(np.flip(material,axis=0),axis=0),axis=0).astype(bool)
    arrival=np.where(settled.any(axis=0),settled.argmax(axis=0),frames-1).astype(np.float64)
    xyz=positions.reshape(-1,3)
    flat_labels=labels.ravel()
    region_ids=np.zeros(size*size,dtype=np.uint16)
    yy,xx=np.mgrid[:size,:size]
    uv=np.column_stack((xx.ravel()/size,yy.ravel()/size))
    regions=[]
    corrected_pixels=0
    for island in np.unique(flat_labels):
        if island==0:continue
        pixels=np.flatnonzero(flat_labels==island)
        target=arrival[pixels]
        valid=(target>0)&(target<frames-1)
        sample=np.flatnonzero(valid)
        if len(sample)<8:sample=np.arange(len(pixels))
        sample=sample[::max(1,len(sample)//4096)]
        center=xyz[pixels].mean(axis=0)
        coordinate=model_coordinate.ravel()[pixels]
        candidates=[]
        for kind,design in (
                ('model',np.column_stack((xyz[pixels]-center,np.ones(len(pixels))))),
                ('uv',np.column_stack((uv[pixels],np.ones(len(pixels))))),
                ('axis',np.column_stack((coordinate,np.ones(len(pixels)))))):
            if len(sample)<design.shape[1]:continue
            coefficients=_robust_plane(design[sample],target[sample])
            prediction=design@coefficients
            error=np.abs(prediction[sample]-target[sample])
            score=float(np.median(error)+.15*np.quantile(error,.9)+.01*design.shape[1])
            candidates.append((score,kind,prediction))
        if not candidates:
            prediction=np.full(len(pixels),float(np.median(target)))
            kind='constant'
        else:
            _,kind,prediction=min(candidates,key=lambda item:item[0])
        correction=valid&(np.abs(prediction-target)>1.25)
        corrected_pixels+=int(correction.sum())
        prediction[correction]=target[correction]
        prediction=np.clip(prediction,0,frames-1)
        low,high=float(prediction.min()),float(prediction.max())
        bins=min(128,len(pixels),max(1,int(np.ceil((high-low)*8))+1))
        cells=np.minimum(bins-1,((prediction-low)/max(high-low,1e-9)*bins).astype(int))
        occupied=np.unique(cells)
        for cell in occupied:
            selected=pixels[cells==cell]
            index=len(regions)+1
            if index>32766:raise ValueError('SMD regional atlas exceeds compact index range')
            region_ids[selected]=index
            regions.append({'island':int(island),'model':kind,'samples':len(selected),
                            'arrival_min':float(prediction[cells==cell].min()),
                            'arrival_max':float(prediction[cells==cell].max())})
    # Carry the nearest used UV region into a small padding border for BCN/mips.
    atlas=region_ids.reshape(size,size)
    radius=max(4,size//64)
    for _ in range(radius):
        previous=atlas.copy()
        for dy,dx in ((0,1),(0,-1),(1,0),(-1,0)):
            shifted=np.roll(previous,(dy,dx),(0,1))
            if dy>0:shifted[:dy]=0
            if dy<0:shifted[dy:]=0
            if dx>0:shifted[:,:dx]=0
            if dx<0:shifted[:,dx:]=0
            take=(atlas==0)&(shifted>0)
            atlas[take]=shifted[take]
    training=labels!=0
    return atlas,{'size':size,'regions':len(regions),'source_islands':coverage['used_islands'],
                  'covered_pixels':coverage['covered_pixels'],'overlapped_pixels':coverage['overlapped_pixels'],
                  'padding_pixels':int((atlas>0).sum())-coverage['covered_pixels'],
                  'locally_corrected_pixels':corrected_pixels,'global_scan_fit':global_fit},training
