"""Shared SMD UV-island atlases and compact regional construction operators."""
from __future__ import annotations

from array import array
from functools import lru_cache
import hashlib
import struct
import sys
import zlib

from .sequence_material_rules import (EFFECT, FIELD_NAMES, MaterialProgram,
                                       ParameterCurves, SHARED_MATERIAL, WIRE_COLOR)

ATLAS_MAGIC=b'LKATL5\0\0'
ISLAND_MAGIC=b'LKISL5\0\0'
ISLAND_METHOD='smd-uv-island-regional-operators-v1'
_MAX_PAYLOAD=128*1024*1024


def _inflate(data,length):
    if not 0<length<=_MAX_PAYLOAD:
        raise ValueError('材质规则解压长度无效。')
    decoder=zlib.decompressobj()
    try:
        raw=decoder.decompress(data,length+1)
    except zlib.error as error:
        raise ValueError('材质规则解压失败。') from error
    if (len(raw)!=length or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data):
        raise ValueError('材质规则解压长度无效。')
    return raw


@lru_cache(maxsize=16)
def _decode_atlas(blob):
    if len(blob)<20:raise ValueError('材质 UV 岛索引无效。')
    magic,size,regions,length=struct.unpack_from('<8sIII',blob)
    if (magic!=ATLAS_MAGIC or size<1 or size>1024 or size&(size-1)
            or not 1<=regions<=32766 or length!=size*size*2):
        raise ValueError('材质 UV 岛索引无效。')
    raw=_inflate(blob[20:],length)
    indices=array('H');indices.frombytes(raw)
    if sys.byteorder!='little':indices.byteswap()
    if max(indices)>regions:raise ValueError('材质 UV 岛区域索引超出范围。')
    return size,regions,indices


def decode_island_program(blob,atlas_blob,rule,*,cancelled=lambda:False):
    from .sequence_material_vtf import check_cancel
    if len(blob)<60:raise ValueError('材质规则资源长度无效。')
    magic,pixels,frames,regions,profiles,length=struct.unpack_from('<8sIIIII',blob)
    if (magic!=ISLAND_MAGIC or pixels!=rule.size**2 or frames!=rule.frames
            or not 1<=regions<=32766 or not 1<=profiles<=pixels+1):
        raise ValueError('材质规则数据头无效。')
    if hashlib.sha256(atlas_blob).digest()!=blob[28:60]:
        raise ValueError('材质规则与 UV 岛索引不匹配。')
    check_cancel(cancelled)
    atlas_size,atlas_regions,mapping=_decode_atlas(atlas_blob)
    if atlas_regions!=regions or atlas_size<rule.size or atlas_size%rule.size:
        raise ValueError('材质规则与 UV 岛索引不匹配。')
    raw=_inflate(blob[60:],length)
    if len(raw)<2:raise ValueError('材质规则数据截断。')
    palette_count=struct.unpack_from('<H',raw)[0]
    if not 1<=palette_count<=256:raise ValueError('材质规则颜色表无效。')
    colors_end=2+palette_count*3
    defaults_end=colors_end+(regions+1)*4
    indices_end=defaults_end+pixels*4
    table_size=profiles*frames
    if length!=indices_end+len(FIELD_NAMES)*table_size:
        raise ValueError('材质规则数据截断。')
    colors=raw[2:colors_end]
    palette=tuple(tuple(colors[i:i+3]) for i in range(0,len(colors),3))
    defaults=array('I');defaults.frombytes(raw[colors_end:defaults_end])
    if sys.byteorder!='little':defaults.byteswap()
    encoded=raw[defaults_end:indices_end]
    packed=bytearray(pixels*4)
    for lane in range(4):packed[lane::4]=encoded[lane*pixels:(lane+1)*pixels]
    residual=array('I');residual.frombytes(packed)
    if sys.byteorder!='little':residual.byteswap()
    if max(defaults)>=profiles or max(residual)>profiles:
        raise ValueError('材质 UV 岛区域索引超出范围。')
    indices=array('I')
    scale=atlas_size//rule.size
    for y in range(rule.size):
        if y%16==0:check_cancel(cancelled)
        row=(y*scale+scale//2)*atlas_size
        for x in range(rule.size):
            pixel=y*rule.size+x
            region=mapping[row+x*scale+scale//2]
            profile=residual[pixel]-1 if residual[pixel] else defaults[region]
            if not region and profile!=0:raise ValueError('材质修正索引超出 UV 岛区域。')
            indices.append(profile)
    fields=[]
    limits=(31,255,126,126,126,127,127,palette_count-1,127 if rule.layer=='normal' else 126)
    for field,(name,limit) in enumerate(zip(FIELD_NAMES,limits)):
        check_cancel(cancelled)
        values=raw[indices_end+field*table_size:indices_end+(field+1)*table_size]
        if max(values)>limit or (name=='alpha' and 127 in values):
            raise ValueError('材质规则参数值无效。')
        if name=='mode':
            unique=set(values)
            if (any(v&WIRE_COLOR and ((v&3)!=0 or not v&EFFECT) for v in unique)
                    or any(v&EFFECT and v&SHARED_MATERIAL for v in unique)
                    or (rule.layer=='normal' and any((v&15)!=3 for v in unique))):
                raise ValueError('材质规则参数值无效。')
        curves=tuple(values[start:start+frames] for start in range(0,len(values),frames))
        fields.append(ParameterCurves(indices,curves))
    return MaterialProgram(frames,palette,tuple(fields))
