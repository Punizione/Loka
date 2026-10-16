"""Patch typed PCF material references without reserializing particle values."""
import re,struct
from .material_text import resource_key


def _parse(data):
    header=re.match(rb'<!--\s*dmx encoding binary ([2345]) format pcf [12]\s*-->\r?\n\0',data)
    if not header:raise ValueError('尚不支持此 PCF/DMX 编码，不能安全分离粒子材质')
    version=int(header[1]);position=header.end();count_fmt='<i' if version>=4 else '<h';index_fmt='<i' if version>=5 else '<h'
    def take(size):
        nonlocal position
        if size<0 or position+size>len(data):raise ValueError('PCF 数据越界或不完整')
        start=position;position+=size;return data[start:position]
    def number(fmt):return struct.unpack(fmt,take(struct.calcsize(fmt)))[0]
    def count(fmt='<i'):
        value=number(fmt)
        if value<0 or value>len(data):raise ValueError('PCF 数量字段无效')
        return value
    def cstring():
        end=data.find(b'\0',position)
        if end<0:raise ValueError('PCF 字符串未闭合')
        return take(end-position+1)[:-1]
    dictionary_start=position;strings=[cstring() for _ in range(count(count_fmt))];dictionary_end=position
    def indexed():
        index=number(index_fmt)
        if index==-1:return b''
        if not 0<=index<len(strings):raise ValueError('PCF 字符串索引无效')
        return strings[index]
    elements=count();types=[]
    for _ in range(elements):
        types.append(indexed());indexed() if version>=4 else cstring();take(16)
    sizes={2:4,3:4,4:1,7:4,8:4,9:8,10:12,11:16,12:12,13:16,14:64}
    attributes=[]
    for element in range(elements):
        for _ in range(count()):
            key=indexed();code=number('<B');array=code>14;kind=code-14 if array else code
            if kind not in range(1,15) or (version==2 and kind==7):raise ValueError('PCF 属性类型不支持')
            payload_start=position;length=count() if array else 1;values=[];spans=[]
            for _ in range(length):
                start=position
                if kind==1:
                    index=number('<i')
                    if index==-2:cstring()
                    elif index<-2 or index>=elements:raise ValueError('PCF 元素索引无效')
                elif kind==5:
                    value=indexed() if version>=4 and not array else cstring()
                    values.append(value);spans.append((start,position))
                elif kind==6:take(count())
                else:take(sizes[kind])
            attributes.append({'element':element,'key':key,'code':code,'values':values,'spans':spans,
                               'payload':data[payload_start:position]})
    if position!=len(data):raise ValueError('PCF 存在未解析的尾部数据')
    return attributes,strings,dictionary_start,dictionary_end,count_fmt,index_fmt,version


def _text(raw):
    for codec in ('utf-8','gb18030','latin1'):
        try:return raw.decode(codec),codec
        except UnicodeDecodeError:continue


def pcf_attributes(data):
    attributes,*_= _parse(data)
    return [dict(a,key=_text(a['key'])[0],values=[_text(v)[0] for v in a['values']]) for a in attributes]


def patch_pcf_materials(data,mapping):
    attributes,strings,start,end,count_fmt,index_fmt,version=_parse(data)
    changes=[];edits=[];added=[]
    for attribute in attributes:
        if attribute['key'].lower()!=b'material' or attribute['code'] not in (5,19):continue
        for raw,span in zip(attribute['values'],attribute['spans']):
            text,codec=_text(raw);key=resource_key(text,'.vmt')
            if key not in mapping:continue
            target=mapping[key].removeprefix('materials/')
            if not text.lower().endswith('.vmt'):target=target[:-4]
            new=target.encode(codec)
            if version>=4 and attribute['code']==5:
                if new not in added:added.append(new)
                index=len(strings)+added.index(new)
                try:replacement=struct.pack(index_fmt,index)
                except struct.error as error:raise ValueError('PCF 字符串字典容量不足') from error
            else:replacement=new+b'\0'
            changes.append((*span,replacement));edits.append({'key':'PCF.material','element':attribute['element'],'source':text,'target':target})
    if not changes:return data,[]
    for begin,finish,replacement in sorted(changes,reverse=True):data=data[:begin]+replacement+data[finish:]
    if added:
        dictionary=struct.pack(count_fmt,len(strings)+len(added))+data[start+struct.calcsize(count_fmt):end]+b'\0'.join(added)+b'\0'
        data=data[:start]+dictionary+data[end:]
    _parse(data)
    return data,edits
