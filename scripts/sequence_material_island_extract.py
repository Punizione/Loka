"""Authoring-only UV-island operators with exact categorical event residuals."""
import struct
import zlib

import numpy as np

from loka.sequence_material_rules import FIELD_NAMES, GAIN_STEPS
from loka.sequence_material_islands import ATLAS_MAGIC, ISLAND_MAGIC


def encode_atlas(atlas,regions):
    raw=atlas.astype('<u2').tobytes()
    return struct.pack('<8sIII',ATLAS_MAGIC,atlas.shape[0],regions,len(raw))+zlib.compress(raw,9)


def scaled_atlas(atlas,size):
    if atlas.shape[0]==size:return atlas
    if atlas.shape[0]%size:raise ValueError('Output resolution is incompatible with SMD atlas')
    scale=atlas.shape[0]//size
    return atlas[scale//2::scale,scale//2::scale]


def pool_island_parameters(palette,fields,atlas,regions,rule,atlas_digest,training):
    mapping=scaled_atlas(atlas,rule.size).ravel().astype(np.uint32)
    scale=atlas.shape[0]//rule.size
    used=(training.reshape(rule.size,scale,rule.size,scale).any(axis=(1,3)).ravel()
          if scale>1 else training.ravel())
    visible=(mapping>0)&used
    pixels=rule.size**2
    modes=np.frombuffer(b''.join(fields[0]),dtype=np.uint8).reshape(rule.frames,pixels).T.copy()
    alpha=np.frombuffer(b''.join(fields[1]),dtype=np.uint8).reshape(rule.frames,pixels).T.copy()
    histories=np.concatenate((modes,alpha),axis=1).tobytes()
    index=np.zeros(pixels,dtype=np.uint32)
    lookup={}
    descriptors=[None]
    profile_regions=[0]
    for pixel in np.flatnonzero(visible):
        at=int(pixel)*rule.frames*2
        history=histories[at:at+rule.frames*2]
        key=(int(mapping[pixel]),history)
        profile=lookup.get(key)
        if profile is None:
            profile=len(descriptors)
            lookup[key]=profile
            descriptors.append(history)
            profile_regions.append(int(mapping[pixel]))
        index[pixel]=profile
    profiles=len(descriptors)
    if profiles<2:raise ValueError('SMD atlas contains no source samples')
    counts=np.bincount(index[visible],minlength=profiles)
    defaults=np.zeros(regions+1,dtype=np.uint32)
    for profile,region in enumerate(profile_regions[1:],1):
        if counts[profile]>counts[defaults[region]]:defaults[region]=profile
    # Synthesis padding inherits its nearest observed categorical profile.
    # Padding never votes in phase, alpha, color or normal statistics.
    image=index.reshape(rule.size,rule.size)
    region_image=mapping.reshape(rule.size,rule.size)
    for _ in range(max(4,rule.size//64)):
        previous=image.copy()
        for dy,dx in ((0,1),(0,-1),(1,0),(-1,0)):
            shifted=np.roll(previous,(dy,dx),(0,1))
            shifted_regions=np.roll(region_image,(dy,dx),(0,1))
            if dy>0:shifted[:dy]=0
            if dy<0:shifted[dy:]=0
            if dx>0:shifted[:,:dx]=0
            if dx<0:shifted[:,dx:]=0
            take=(image==0)&(shifted>0)&(region_image>0)&(shifted_regions==region_image)
            image[take]=shifted[take]
    empty=(index==0)&(mapping>0)
    index[empty]=defaults[mapping[empty]]
    table=np.zeros((len(FIELD_NAMES),profiles,rule.frames),dtype=np.uint8)
    table[0,:,:]=3 if rule.layer=='normal' else 4
    table[1,:,:]=255 if rule.layer=='glow' else 254
    table[2:5,:,:]=GAIN_STEPS
    for profile,history in enumerate(descriptors[1:],1):
        table[0,profile]=np.frombuffer(history[:rule.frames],dtype=np.uint8)
        table[1,profile]=np.frombuffer(history[rule.frames:],dtype=np.uint8)
    for frame in range(rule.frames):
        for field in (2,3,4,5,6,8):
            values=np.frombuffer(fields[field][frame],dtype=np.uint8)
            total=np.bincount(index[visible],weights=values[visible],minlength=profiles)
            table[field,:,frame]=np.rint(total/np.maximum(counts,1)).astype(np.uint8)
        hues=np.frombuffer(fields[7][frame],dtype=np.uint8)
        votes=np.bincount(index[visible]*len(palette)+hues[visible],
                          minlength=profiles*len(palette)).reshape(profiles,len(palette))
        table[7,:,frame]=votes.argmax(axis=1).astype(np.uint8)
    # Unused space retains source RGB for mip padding, with zero visibility.
    table[0,0,:]=3
    table[1,0,:]=255 if rule.layer=='glow' else 254
    table[2:5,0,:]=0 if rule.layer=='glow' else GAIN_STEPS
    table[8,0,:]=0
    residual=np.where(index==defaults[mapping],0,index+1).astype('<u4')
    residual_bytes=residual.tobytes()
    payload=bytearray(struct.pack('<H',len(palette)))
    payload.extend(bytes(c for rgb in palette for c in rgb))
    payload.extend(defaults.astype('<u4').tobytes())
    for lane in range(4):payload.extend(residual_bytes[lane::4])
    payload.extend(table.tobytes())
    header=struct.pack('<8sIIIII',ISLAND_MAGIC,pixels,rule.frames,regions,profiles,len(payload))
    blob=header+bytes.fromhex(atlas_digest)+zlib.compress(payload,9)
    return blob,{'regional_profiles':profiles,'operator_rows':len(FIELD_NAMES),
                 'categorical_histories':'exact mode and alpha on SMD-covered samples',
                 'residual_pixels':int(np.count_nonzero(residual)),
                 'training_pixels':int(visible.sum()),'payload_bytes':len(payload)}
