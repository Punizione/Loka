"""Byte-preserving Valve material key/value edits for owned resource references."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .vpk import safe_archive_path

# Texture-valued shader parameters, not scalar flags or proxy variables.
# Valve SDK: materialsystem/stdshaders/vertexlitgeneric_dx9.cpp.
TEXTURES=frozenset(('$basetexture $basetexture2 $basetexture3 $basetexture4 $texture1 $texture2 $texture3 $bumpmap $bumpmap2 '
    '$normalmap $detail $detail2 $envmap $envmapmask $envmapmask2 $selfillummask $phongexponenttexture '
    '$lightwarptexture $phongwarptexture $blendmodulatetexture $dudvmap $flowmap $flow_noise_texture '
    '$refracttinttexture $flashlighttexture $ambientocclusiontexture $albedo $compress $stretch '
    '$bumpcompress $bumpstretch $lightmap $emissiveblendbasetexture $emissiveblendtexture '
    '$emissiveblendflowtexture $sheenmap $sheenmapmask $fleshinteriortexture $fleshinteriornoisetexture '
    '$fleshbordertexture1d $fleshnormaltexture $fleshsubsurfacetexture $fleshcubetexture').split())
MATERIALS=frozenset(('include','#include','#base','$fallbackmaterial','$bottommaterial','$underwateroverlay'))
NON_TEXTURES=frozenset(('$surfaceprop','$surfaceprop2','$keywords','$model','$color','$color2'))


@dataclass(frozen=True)
class MaterialToken:
    value: str
    start: int
    end: int
    quoted: bool=False


def encoding(data):
    for codec in ('utf-8-sig','gb18030','latin1'):
        try:data.decode(codec);return 'utf-8' if codec=='utf-8-sig' else codec
        except UnicodeDecodeError:continue


def material_tokens(data):
    codec=encoding(data);tokens=[];i=3 if data.startswith(b'\xef\xbb\xbf') else 0
    while i<len(data):
        if data[i] in b' \t\r\n':i+=1;continue
        if data.startswith(b'//',i):
            end=data.find(b'\n',i);i=len(data) if end<0 else end+1;continue
        if data.startswith(b'/*',i):
            end=data.find(b'*/',i+2)
            if end<0:raise ValueError('VMT 注释未闭合')
            i=end+2;continue
        start=i
        if data[i]==34:
            i+=1
            while i<len(data):
                if data[i]==34:break
                if data[i]==92 and i+1<len(data) and data[i+1]==34:i+=2
                else:i+=1
            if i==len(data):raise ValueError('VMT 字符串未闭合')
            tokens.append(MaterialToken(data[start+1:i].decode(codec),start,i+1,True));i+=1
        elif data[i] in b'{}':
            tokens.append(MaterialToken(chr(data[i]),i,i+1));i+=1
        else:
            while i<len(data) and data[i] not in b' \t\r\n{}"':i+=1
            tokens.append(MaterialToken(data[start:i].decode(codec),start,i))
    return tokens,codec


def material_pairs(data):
    tokens,codec=material_tokens(data);pairs=[]
    def block(index,context,nested):
        while index<len(tokens):
            key=tokens[index]
            if not key.quoted and key.value=='}':
                if not nested:raise ValueError('VMT 多余的右花括号')
                return index+1
            if not key.quoted and key.value.startswith('['):index+=1;continue
            if index+1>=len(tokens):raise ValueError('VMT 键缺少值: '+key.value)
            value=tokens[index+1]
            if not value.quoted and value.value=='{':
                if key.value.lower()=='proxies':
                    depth=1;index+=2
                    while index<len(tokens) and depth:
                        token=tokens[index]
                        if not token.quoted:
                            if token.value=='{':depth+=1
                            elif token.value=='}':depth-=1
                        index+=1
                    if depth:raise ValueError('VMT Proxies 花括号未闭合')
                else:index=block(index+2,context+(key.value.lower(),),True)
            elif not value.quoted and value.value=='}':raise ValueError('VMT 键缺少值: '+key.value)
            else:
                pairs.append((key,value,context));index+=2
        if nested:raise ValueError('VMT 花括号未闭合')
        return index
    block(0,(),False)
    return pairs,codec


def resource_key(value,extension):
    value=value.replace('\\','/').lower()
    if value.startswith(('$', '_rt_','[','{')) or value=='env_cubemap':return None
    if value.startswith('materials/'):value=value[10:]
    if not value.endswith(extension):value+=extension
    try:return safe_archive_path('materials/'+value)
    except ValueError:return None


def patch_material(data,mapping):
    pairs,codec=material_pairs(data);changes=[];decisions=[]
    for key,value,context in pairs:
        name=key.value.lower()
        if 'proxies' in context:continue
        kind='.vtf' if name in TEXTURES else '.vmt' if name in MATERIALS else None
        if kind is None:
            candidate=resource_key(value.value,'.vtf')
            if (name.startswith('$') and name not in NON_TEXTURES and candidate in mapping
                    and not re.fullmatch(r'[-+]?\d+(?:\.\d+)?',value.value)):
                raise ValueError('未知 VMT 参数引用了待移动纹理，无法安全改写: '+key.value)
            continue
        old=resource_key(value.value,kind)
        if old not in mapping:continue
        target=mapping[old].removeprefix('materials/')
        if not value.value.lower().endswith(kind):target=target[:-len(kind)]
        new=('"'+target+'"') if value.quoted or any(c.isspace() for c in target) else target
        changes.append((value.start,value.end,new.encode(codec)))
        decisions.append({'key':key.value,'source':value.value,'target':target})
    for start,end,new in sorted(changes,reverse=True):data=data[:start]+new+data[end:]
    return data,decisions
