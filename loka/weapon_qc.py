"""Materialize conversion plans while leaving exported QC/SMD sources intact."""
from dataclasses import dataclass, replace
from pathlib import Path
import math
import shutil

from .weapon_catalog import get_weapon
from .weapon_sequences import get_profile, validate_sequence_layout, _classify, _timer_slot, target_timing_slot
from .vpk import safe_archive_path
from .weapon_ads import remove_ads_layers,is_ads_or_mixed_activity
from .fps_values import round_fps, fps_text, round_qc_fps, scaled_fps


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int
    quoted: bool = False


@dataclass(frozen=True)
class QcCommand:
    kind: str
    name: str
    text: str


def _tokens(text):
    result=[];i=0
    while i<len(text):
        if text[i].isspace():i+=1;continue
        if text.startswith('//',i):
            end=text.find('\n',i);i=len(text) if end<0 else end+1;continue
        if text.startswith('/*',i):
            end=text.find('*/',i+2)
            if end<0:raise ValueError('QC 注释未闭合')
            i=end+2;continue
        start=i
        if text[i]=='"':
            end=text.find('"',i+1)
            if end<0:raise ValueError('QC 字符串未闭合')
            result.append(Token(text[i+1:end],i,end+1,True));i=end+1
        elif text[i] in '{}':
            result.append(Token(text[i],i,i+1));i+=1
        else:
            while i<len(text) and not text[i].isspace() and text[i] not in '{}"':i+=1
            result.append(Token(text[start:i],start,i))
    return result


def parse_qc(text):
    tokens=_tokens(text);starts=[];depth=0
    for t in tokens:
        if not t.quoted and t.value=='{':depth+=1
        elif not t.quoted and t.value=='}':
            depth-=1
            if depth<0:raise ValueError('QC 多余的右花括号')
        elif depth==0 and not t.quoted and t.value.startswith('$'):starts.append(t.start)
    if depth:raise ValueError('QC 花括号未闭合')
    commands=[]
    for i,start in enumerate(starts):
        part=text[start:starts[i+1] if i+1<len(starts) else len(text)].rstrip()
        values=_tokens(part)
        kind=values[0].value[1:].lower()
        commands.append(QcCommand(kind,values[1].value if len(values)>1 else '',part))
    return tuple(commands)


def _patch(text,changes):
    for start,end,value in sorted(changes,reverse=True):text=text[:start]+value+text[end:]
    return text


def _quote(value):
    allowed_label='attack_03_`layer'
    tick_allowed=value==allowed_label or (value.replace('\\','/').split('/')[-1]==allowed_label+'.smd')
    if any(c in value for c in '\r\n\x00"') or ('`' in value and not tick_allowed):
        raise ValueError(f'QC 名称/引用无效: {value!r}')
    return '"'+value+'"'


def _rename(text,name):
    token=_tokens(text)[1]
    return _patch(text,[(token.start,token.end,_quote(name))])


def _body_tokens(text):
    result=[];depth=0
    for t in _tokens(text):
        if not t.quoted and t.value=='{':depth+=1
        elif not t.quoted and t.value=='}':depth-=1
        elif depth==1:result.append(t)
    return result


def sequence_dependencies(text):
    """Read repaired QC sequence layers without interpreting events as syntax."""
    graph={}
    for command in parse_qc(text):
        if command.kind!='sequence':continue
        dependencies=[];tokens=_body_tokens(command.text)
        for i,token in enumerate(tokens):
            if token.quoted or token.value.lower() not in ('addlayer','blendlayer'):continue
            if i+1>=len(tokens):raise ValueError('QC layer 缺少引用: '+command.name)
            dependencies.append(tokens[i+1].value)
        graph.setdefault(command.name.lower(),tuple(dict.fromkeys(dependencies)))
    return graph


def _options(text,values):
    tokens=_body_tokens(text);changes=[];skip=set()
    for i,t in enumerate(tokens):
        key=t.value.lower()
        if i in skip or t.quoted or key not in values:continue
        arity=3 if key=='blend' else 2 if key in ('activity','frames') else (0 if key in ('loop','hidden') else 1)
        if i+arity>=len(tokens):raise ValueError(f'QC 选项缺少参数: {key}')
        changes.append((t.start,tokens[i+arity].end,''));skip.update(range(i,i+arity+1))
    text=_patch(text,changes)
    tokens=_tokens(text)
    if not tokens or tokens[-1].value!='}':raise ValueError('转换只支持反编译输出的块式 animation/sequence')
    lines=[]
    for key,value in values.items():
        if value is None:continue
        lines.append('\t'+key+((' '+str(value)) if value is not True else ''))
    return text[:tokens[-1].start].rstrip()+'\n'+'\n'.join(lines)+'\n}\n'


def _leading_refs(text):
    result=[]
    for t in _body_tokens(text):
        if not t.quoted:break
        result.append(t)
    return result


def _resource_tokens(text):
    """Only file operands, excluding event options and other quoted payloads."""
    tokens=_tokens(text);starts=[];depth=0
    for index,token in enumerate(tokens):
        if not token.quoted and token.value=='{':depth+=1
        elif not token.quoted and token.value=='}':depth-=1
        elif depth==0 and not token.quoted and token.value.startswith('$'):starts.append(index)
    for start,end in zip(starts,starts[1:]+[len(tokens)]):
        part=tokens[start:end];kind=part[0].value.lower();operands=[]
        if kind in ('$animation','$body','$model') and len(part)>2:operands.append(part[2])
        elif kind in ('$collisionmodel','$collisionjoints','$proceduralbones') and len(part)>1:operands.append(part[1])
        elif kind=='$sequence':
            depth=0
            for token in part:
                if not token.quoted and token.value=='{':depth+=1
                elif not token.quoted and token.value=='}':depth-=1
                elif depth==1:
                    if not token.quoted:break
                    operands.append(token)
        keys={'$bodygroup':{'studio':1},'$model':{'flexfile':1},
              '$lod':{'replacemodel':2},'$shadowlod':{'replacemodel':2}}.get(kind,{})
        for i,token in enumerate(part):
            if not token.quoted and token.value.lower() in keys:
                operands.extend(part[i+1:i+1+keys[token.value.lower()]])
            if kind=='$model' and not token.quoted and token.value.lower()=='eyelid' and i+2<len(part):
                operands.append(part[i+2])
        for token in operands:
            if token.quoted and token.value.lower().endswith(('.smd','.vta','.vrd')):yield token


def _single_source_reference(text,index):
    refs=_leading_refs(text)
    if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(refs):
        raise ValueError('指定的来源动画引用不存在')
    selected=refs[index]
    text=_patch(text,[(refs[0].start,refs[-1].end,_quote(selected.value))])
    if len(refs)>1:
        tokens=_body_tokens(text)
        for i,token in enumerate(tokens):
            if token.quoted:continue
            key=token.value.lower()
            if key=='blend':
                try:
                    if i+3>=len(tokens) or not tokens[i+1].value:raise ValueError()
                    limits=[float(tokens[i+j].value) for j in (2,3)]
                    if not all(math.isfinite(value) for value in limits):raise ValueError()
                except (ValueError,OverflowError):raise ValueError('QC blend 参数无效') from None
            elif key=='blendwidth':
                try:
                    if i+1>=len(tokens) or int(tokens[i+1].value)<=0:raise ValueError()
                except ValueError:raise ValueError('QC blendwidth 参数无效') from None
            elif key in ('blendref','blendcomp','blendcenter'):
                if i+1>=len(tokens) or not tokens[i+1].quoted:
                    raise ValueError('QC '+key+' 动画引用无效')
        text=_options(text,{'blend':None,'blendwidth':None,'blendref':None,'blendcomp':None,'blendcenter':None})
    return text


def _number(value):return format(value,'.9g')


def _activity_weight(text):
    tokens=_body_tokens(text)
    for i,t in enumerate(tokens):
        if not t.quoted and t.value.lower()=='activity':
            if i+2>=len(tokens):raise ValueError('QC Activity 缺少权重')
            int(tokens[i+2].value)
            return tokens[i+2].value
    return '1'


def _drop_events(text):
    tokens=_tokens(text);changes=[]
    for i,t in enumerate(tokens[:-1]):
        if t.quoted or t.value!='{' or tokens[i+1].quoted or tokens[i+1].value.lower()!='event':continue
        depth=1
        for last in tokens[i+1:]:
            if not last.quoted:
                if last.value=='{':depth+=1
                elif last.value=='}':depth-=1
            if depth==0:
                changes.append((t.start,last.end,''));break
    return _patch(text,changes)


def _event_values(text):
    tokens=_tokens(text);events=[]
    for i,t in enumerate(tokens[:-1]):
        if t.quoted or t.value!='{' or tokens[i+1].quoted or tokens[i+1].value.lower()!='event':continue
        values=[]
        for item in tokens[i+2:]:
            if not item.quoted and item.value=='}':break
            if not item.quoted and item.value=='{':raise ValueError('QC event 块嵌套无效')
            values.append(item.value)
        if len(values)<2 or not math.isfinite(float(values[1])):raise ValueError('QC event 无效')
        events.append(tuple(values))
    return tuple(events)


def _read_options(text):
    result={};tokens=_body_tokens(text)
    for i,t in enumerate(tokens):
        key=t.value.lower()
        if t.quoted or key not in ('fps','numframes','frames','hidden','loop'):continue
        if key in result:raise ValueError('QC 选项重复: '+key)
        arity=2 if key=='frames' else 0 if key in ('hidden','loop') else 1
        if i+arity>=len(tokens):raise ValueError('QC 选项缺少参数: '+key)
        if key=='fps':
            value=float(tokens[i+1].value)
            if not math.isfinite(value) or value<=0:raise ValueError('QC FPS 无效')
        elif key=='numframes':
            value=int(tokens[i+1].value)
            if value<1:raise ValueError('QC numframes 无效')
        elif key=='frames':
            value=tuple(int(x.value) for x in tokens[i+1:i+3])
            if value[0]<0 or value[1]<value[0]:raise ValueError('QC frames 无效')
        else:value=True
        result[key]=value
    return result


def _effective_frames(count,options):
    if 'frames' in options:
        start,end=options['frames']
        if end>=count:raise ValueError('QC frames 越出来源时间轴')
        count=end-start+1
    return options.get('numframes',count)


def _retime(text,row,*,preserve_numframes=False,write_fps=True):
    # Authored crop/padding and event frames stay at their original site.
    tokens=_tokens(text)
    for i,t in enumerate(tokens):
        if not t.quoted and t.value.lower()=='event':
            if i+2>=len(tokens) or not math.isfinite(float(tokens[i+2].value)):
                raise ValueError('QC event 帧数无效')
    return _options(text,{'fps':fps_text(row.fps)} if write_fps else {})


class _Document:
    def __init__(self,path,index):
        self.path=Path(path).resolve();self.index=index
        self.commands=parse_qc(self.path.read_text(encoding='utf-8-sig'))
        if any(c.kind in ('include','declaresequence','declareanimation') for c in self.commands):
            raise ValueError(f'转换源含未展开的 include/declare 指令: {self.path}')
        groups={};discarded=set()
        for command in self.commands:
            if command.kind=='animation':groups.setdefault(command.name.lower(),[]).append(command)
        for name,definitions in groups.items():
            if len(definitions)<2:continue
            refs=[_tokens(c.text)[2].value.replace('\\','/').lower() for c in definitions]
            full=[c for c in definitions if any(t.value not in ('{','}') for t in _tokens(c.text)[3:])]
            if len(set(refs))!=1 or len(full)>1:
                raise ValueError('重复 animation 定义有冲突: '+name)
            chosen=full[0] if full else definitions[0]
            discarded.update(id(c) for c in definitions if c is not chosen)
        self.commands=tuple(c for c in self.commands if id(c) not in discarded)
        self.animations={c.name.lower():c for c in self.commands if c.kind=='animation'}
        self.weights={c.name.lower():f'loka_d{index}_w{i}' for i,c in enumerate(self.commands) if c.kind=='weightlist'}
        self.animation_names={name:f'loka_d{index}_a{i}' for i,name in enumerate(self.animations)}
        self.resources={}
        self.frame_counts={}
        self.excluded_sequences=()
        self.ads_removals=None

    def animation_frames(self,command):
        values=_tokens(command.text)
        if len(values)<3:raise ValueError('animation 缺少 SMD 引用')
        source=(self.path.parent/values[2].value.replace('\\','/')).resolve()
        if not source.is_relative_to(self.path.parent) or not source.is_file():raise ValueError('动画来源路径缺失/越界: '+str(source))
        if source not in self.frame_counts:
            active=False;last=-1
            with source.open(encoding='utf-8-sig') as handle:
                for line in handle:
                    line=line.strip()
                    if line=='skeleton':active=True;continue
                    if active and line=='end':break
                    if active and line.startswith('time '):last=max(last,int(line.split()[1]))
            if last<0:raise ValueError(f'动画 SMD 没有时间轴: {source}')
            self.frame_counts[source]=last+1
        return _effective_frames(self.frame_counts[source],_read_options(command.text))

    def include_resources(self):
        for command in self.commands:
            if command.kind!='includemodel':continue
            relative=safe_archive_path(command.name.replace('\\','/'))
            if relative.lower().startswith('models/'):relative=relative[7:]
            if not (self.path.parent/'models'/relative).is_file():
                raise ValueError(f'W 模转换源缺少 include 依赖: {command.name}')
        library=self.path.parent/'models'
        if library.is_dir():
            for source in library.rglob('*'):
                if source.is_file():
                    if not source.resolve().is_relative_to(library.resolve()):raise ValueError('include 路径越界')
                    self.resources[source.relative_to(self.path.parent).as_posix()]=source

    def rebase(self,text):
        changes=[]
        for t in _resource_tokens(text):
            source=(self.path.parent/t.value.replace('\\','/')).resolve()
            if not source.is_relative_to(self.path.parent) or not source.is_file():
                raise ValueError(f'转换源资源缺失或越出导出目录: {source}')
            relative=f'src_{self.index}/'+source.relative_to(self.path.parent).as_posix()
            if source.name.lower() in ('.smd','.vta','.vrd'):
                # Empty compiled mesh names export as .smd; studiomdl appends
                # another extension to that basename. Rename only the copy.
                relative=next((key for key,value in self.resources.items() if value==source),None)
                if relative is None:
                    counter=0
                    while True:
                        name=f'loka_unnamed_{counter}'+source.name.lower()
                        relative=f'src_{self.index}/'+name
                        if relative not in self.resources and not (self.path.parent/name).exists():break
                        counter+=1
            self.resources[relative]=source
            changes.append((t.start,t.end,_quote(relative)))
        return _patch(text,changes)

    def symbols(self,text,sequence_map=None):
        text,removed=remove_ads_layers(text,self.excluded_sequences)
        if removed and self.ads_removals is not None:
            self.ads_removals.extend(dict(item,source_qc=str(self.path),command=_tokens(text)[1].value) for item in removed)
        tokens=_tokens(text);changes=[]
        for i,t in enumerate(tokens):
            if t.quoted or i+1>=len(tokens):continue
            key=t.value.lower();ref=tokens[i+1];name=ref.value.lower()
            if key in ('subtract','alignto','match','matchblend') and name in self.animation_names:
                changes.append((ref.start,ref.end,_quote(self.animation_names[name])))
            elif key=='weightlist' and name in self.weights:
                changes.append((ref.start,ref.end,_quote(self.weights[name])))
            elif key in ('addlayer','blendlayer') and sequence_map is not None:
                if name not in sequence_map:raise ValueError(f'转换动作引用了被排除/不存在的 sequence: {ref.value}')
                changes.append((ref.start,ref.end,_quote(sequence_map[name])))
        return _patch(text,changes)


def _placeholder(resource):
    text=resource.read_text(encoding='utf-8-sig');lines=text.splitlines()
    try:
        node_start=next(i for i,s in enumerate(lines) if s.strip()=='nodes')
        node_end=next(i for i in range(node_start+1,len(lines)) if lines[i].strip()=='end')
        skel=next(i for i,s in enumerate(lines) if s.strip()=='skeleton')
        frame=next(i for i in range(skel+1,len(lines)) if lines[i].strip().startswith('time '))
        end=next(i for i in range(frame+1,len(lines)) if lines[i].strip()=='end' or lines[i].strip().startswith('time '))
    except StopIteration as error:raise ValueError('SMD 缺少 nodes 或首帧 skeleton，无法生成占位') from error
    return '\n'.join(['version 1']+lines[node_start:node_end+1]+['skeleton','time 0']+lines[frame+1:end]+['end',''])


def _nodes(resource):
    lines=resource.read_text(encoding='utf-8-sig').splitlines();nodes={};active=False
    for line in lines:
        if line.strip()=='nodes':active=True;continue
        if active and line.strip()=='end':break
        if active:
            t=_tokens(line)
            if len(t)!=3:raise ValueError(f'SMD nodes 无效: {resource}')
            nodes[int(t[0].value)]=(t[1].value,int(t[2].value))
    if not nodes:raise ValueError(f'SMD 缺少骨骼: {resource}')
    return {name:nodes[parent][0] if parent>=0 else '' for name,parent in nodes.values()}


def _validate_include_bones(documents):
    if len(documents)<2:return
    primary={}
    for resource in documents[0].resources.values():
        if resource.suffix.lower()=='.smd':primary.update(_nodes(resource))
    for command in documents[0].commands:
        if command.kind=='definebone':
            tokens=_tokens(command.text)
            if len(tokens)>=3:primary[tokens[1].value]=tokens[2].value
    for doc in documents[1:]:
        for resource in doc.resources.values():
            if resource.suffix.lower()!='.smd':continue
            nodes=_nodes(resource)
            if not nodes.keys() & primary.keys():raise ValueError(f'include 与主模型没有共同骨骼: {resource}')
            # Libraries may contain extra, non-mesh control bones; shared hierarchies must agree.
            for name,parent in nodes.items():
                if name in primary and primary[name]!=parent:
                    raise ValueError(f'include 骨骼与主模型不兼容: {name} / {resource}')


def _write_output(output_dir,name,text,documents,placeholder=None,generated_resources=None):
    output_dir=Path(output_dir).resolve()
    if output_dir.exists():raise FileExistsError(f'转换源输出目录已存在: {output_dir}')
    # Resolve and validate everything before the first write; originals remain read-only.
    output_dir.mkdir(parents=True)
    for doc in documents:
        for relative,source in doc.resources.items():
            target=output_dir/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
    if placeholder is not None:(output_dir/'loka_placeholder.smd').write_text(placeholder,encoding='utf-8')
    for relative,payload in (generated_resources or {}).items():
        (output_dir/relative).write_text(payload,encoding='utf-8')
    result=output_dir/name
    result.write_text(text,encoding='utf-8')
    return result


def validate_converted_qc(text,plan,target_id,*,origin_expectations=None):
    commands=parse_qc(text)
    animations={c.name.lower():c for c in commands if c.kind=='animation'}
    if len(animations)!=sum(c.kind=='animation' for c in commands):raise ValueError('转换 QC animation 名称重复')
    validated_rates={}
    def animation_rate(name):
        key=name.lower()
        if key in validated_rates:return validated_rates[key]
        animation=animations.get(key)
        if animation is None:raise ValueError(f'转换 QC animation 引用不存在: {name}')
        options=_body_tokens(animation.text);rates=[]
        for i,token in enumerate(options):
            if not token.quoted and token.value.lower()=='fps':
                if i+1>=len(options):raise ValueError('animation FPS 缺少参数')
                rates.append(float(options[i+1].value))
        if len(rates)>1:raise ValueError('animation FPS 重复')
        rate=rates[0] if rates else 30.0
        if not math.isfinite(rate) or rate<=0:raise ValueError(f'animation FPS 无效: {name}')
        validated_rates[key]=rate
        return rate
    if any(c.kind=='includemodel' for c in commands):raise ValueError('转换 V QC 仍包含 include')
    sequences=[c for c in commands if c.kind=='sequence']
    if len(sequences)!=len(plan.sequences):raise ValueError('转换 QC sequence 数量与计划不符')
    actual=[]
    if origin_expectations is not None and len(origin_expectations)!=len(sequences):raise ValueError('QC 来源约束数量不符')
    for index,(expected,command) in enumerate(zip(plan.sequences,sequences)):
        values={};tokens=_body_tokens(command.text)
        for i,t in enumerate(tokens):
            if t.quoted or t.value.lower() not in ('fps','numframes','activity'):continue
            if i+1>=len(tokens):raise ValueError('转换 QC 选项缺少参数')
            key=t.value.lower()
            if key in values:raise ValueError(f'转换 QC 选项重复: {key}')
            values[key]=tokens[i+1].value
        refs=_leading_refs(command.text)
        if not refs:raise ValueError(f'转换 QC 缺少动画引用: {command.name}')
        rates=[30.0 if ref.value.lower().endswith('.smd') else animation_rate(ref.value) for ref in refs]
        fps=float(values['fps']) if 'fps' in values else rates[0]
        frames=int(values['numframes']) if 'numframes' in values else None
        activity=values.get('activity','')
        if not plan.keep_ads and is_ads_or_mixed_activity(activity):raise ValueError('转换 QC 仍包含 ADS/MIXED Activity: '+activity)
        origin=origin_expectations[index] if origin_expectations is not None else None
        expected_frames=origin['options'].get('numframes') if origin else expected.numframes
        if origin:
            authored={k:v for k,v in _read_options(command.text).items() if k!='fps'}
            if authored!=origin['options']:raise ValueError('转换 QC 改变作者约束: '+command.name)
            if _event_values(command.text)!=origin['events']:raise ValueError('转换 QC 改变来源事件: '+command.name)
            if [r.value for r in refs]!=[r['name'] for r in origin['refs']]:raise ValueError('转换 QC 引用与来源绑定不符')
            for ref,binding in zip(refs,origin['refs']):
                if not ref.value.lower().endswith('.smd'):
                    authored_animation={k:v for k,v in _read_options(animations[ref.value.lower()].text).items() if k!='fps'}
                    if authored_animation!=binding.get('options',{}):raise ValueError('转换 QC 改变命名动画作者约束')
                    if not math.isclose(animation_rate(ref.value),binding['fps'],rel_tol=1e-7,abs_tol=1e-7):raise ValueError('转换 QC 命名动画 FPS 与来源绑定不符')
            effective_rates=[fps if r.value.lower().endswith('.smd') or r.value.lower()==refs[0].value.lower() else rates[i] for i,r in enumerate(refs)]
            if any(not math.isclose(rate,binding['fps'],rel_tol=1e-7,abs_tol=1e-7) for rate,binding in zip(effective_rates,origin['refs'])):raise ValueError('转换 QC 引用 FPS 与来源绑定不符')
        if command.name!=expected.name or activity!=expected.activity or frames!=expected_frames:
            raise ValueError(f'转换 QC 名称/Activity/帧数与计划不符: {command.name}')
        if not math.isfinite(fps) or not math.isclose(fps,origin['fps'] if origin else expected.fps,rel_tol=1e-7,abs_tol=1e-7):
            raise ValueError(f'转换 QC FPS 与计划不符: {command.name}')
        actual.append(replace(expected,name=command.name,activity=activity,fps=fps,numframes=frames))
    issues=validate_sequence_layout(actual,target_id)
    if issues:raise ValueError('转换 QC 顺序校验失败: '+'; '.join(issues))


def _timing_reference(row,profile):
    if row.preserve_source_fps:return None
    if row.official_index is not None:slot=target_timing_slot(profile,profile.slots[row.official_index])
    else:
        activity=row.activity or row.inferred_source_activity or ''
        if not activity:return None
        role=_classify(replace(row.source,name='__loka_role',activity=activity),profile,0)
        slot=next((replace(s,hidden=False) for s in profile.slots if (s.semantic,s.layer)==role[:2] and not s.unused),None)
    if slot and not slot.flexible and slot.semantic!='look_poses' and slot.end_frame and slot.fps:return slot
    return None


def rewrite_viewmodel_qc(primary_qc,include_qcs,plan,target_id,output_dir,*,ads_removals=None,fps_matches=None):
    if plan.missing_actions:raise ValueError('不能生成转换 QC，缺少必需动作: '+', '.join(plan.missing_actions))
    issues=validate_sequence_layout(plan.sequences,target_id)
    if issues:raise ValueError('sequence 计划无效: '+'; '.join(issues))
    for row in plan.sequences:_quote(row.name)
    profile=get_profile(target_id)
    documents=[_Document(p,i) for i,p in enumerate([primary_qc]+list(include_qcs))]
    for doc in documents:
        doc.excluded_sequences=plan.excluded_sequences
        doc.ads_removals=ads_removals
    sources={}
    for doc in documents:
        for command in doc.commands:
            if command.kind=='sequence':sources.setdefault(command.name.lower(),(doc,command))
    sequence_map={};map_scores={}
    def normalize_activity(value):
        return value.upper().replace('_SNIPER','').replace('ACT_VM_SHOOT','ACT_VM_PRIMARYATTACK')
    for row in plan.sequences:
        if row.source and not row.reload_helper:
            key=row.source.name.lower()
            original=normalize_activity(row.source.activity or row.inferred_source_activity or '')
            matched=(profile.slots[row.official_index].activity if row.official_index is not None else row.activity)
            score=2*int(bool(original) and original==normalize_activity(matched)) + int(row.name.lower()==key)
            if key not in sequence_map or score>map_scores[key]:
                sequence_map[key]=row.name;map_scores[key]=score
    records=[];demands={};variants={}
    generated_resources={}
    for index,row in enumerate(plan.sequences):
        slot=profile.slots[row.official_index] if row.official_index is not None else None
        if row.reload_parts:
            from .weapon_reload import materialize_reload
            records.append(materialize_reload(row,index,sources,profile,variants,generated_resources))
            continue
        if row.source is None:
            records.append(dict(text='$sequence '+_quote(row.name)+' {\n\t"loka_placeholder.smd"\n}\n',
                fps=row.fps,write_fps=True,refs=[dict(name='loka_placeholder.smd',fps=row.fps,frames=1)],
                options={'loop':True} if slot and slot.loop else {},events=()));continue
        if row.source.name.lower() not in sources:raise ValueError('QC 中找不到来源 sequence: '+row.source.name)
        doc,command=sources[row.source.name.lower()]
        if row.source_reference_index is not None:
            command=replace(command,text=_single_source_reference(command.text,row.source_reference_index))
        if row.idle_fallback:
            ref=_leading_refs(command.text)[0]
            named=doc.animations.get(ref.value.lower())
            physical=_tokens(named.text)[2].value if named else ref.value
            # Borrow the SMD itself, not the named animation's modifiers or the
            # idle sequence's layers, blends, events and IK directives.
            body=_quote(physical)+'\n\tfps '+fps_text(row.source.fps)
            if slot.loop:body+='\n\tloop'
            command=replace(command,text='$sequence '+_quote(command.name)+' {\n '+body+'\n}')
        opts=_read_options(command.text)
        source_refs=_leading_refs(command.text)
        if not source_refs:raise ValueError('QC sequence 没有动画引用')
        if any(k in opts for k in ('frames','numframes')) and any(r.value.lower() in doc.animations for r in source_refs):
            raise ValueError('sequence 上的裁帧与命名动画组合需进一步核对，不能丢弃原约束')
        local_map={**sequence_map,**dict(row.source_sequence_map)}
        text=doc.rebase(doc.symbols(_rename(command.text,row.name),local_map));refs=_leading_refs(text)
        if row.reload_helper:text=_options(_drop_events(text),{'hidden':True})
        bindings=[];reference=_timing_reference(row,profile)
        first_named=doc.animations.get(source_refs[0].value.lower())
        source_rate=opts.get('fps',_read_options(first_named.text).get('fps',30.0) if first_named else 30.0)
        for ai,(original,ref) in enumerate(zip(source_refs,refs)):
            key=original.value.lower();named=doc.animations.get(key)
            if named:
                count=doc.animation_frames(named);rate=_read_options(named.text).get('fps',30.0)
                # Repeated uses of the first named symbol share its overridden descriptor.
                if key==source_refs[0].value.lower():rate=source_rate
            elif key.endswith('.smd'):
                implicit=QcCommand('animation','implicit','$animation "implicit" '+_quote(original.value)+' { }')
                count=_effective_frames(doc.animation_frames(implicit),opts);rate=source_rate
            else:raise ValueError('QC animation 引用不存在: '+original.value)
            original_rate=rate
            if row.fps_scale is not None and count>1:
                ratio=row.fps_scale_ratio or (row.fps_scale,1)
                rate=scaled_fps(rate,*ratio)
            elif not row.source.loop and reference and count>1:
                rate=scaled_fps(count-1,reference.fps,reference.end_frame)
            rate=round_fps(rate)
            if fps_matches is not None and row.fps_scale is not None:
                fps_matches.append(dict(sequence=row.name,source=row.source.name,reference=original.value,
                                        original_fps=original_rate,scale=row.fps_scale if count>1 else 1.0,
                                        fps=rate,frames=count,
                                        fallback_reason='单帧动画引用，保留原 FPS' if count==1 else None))
            if not math.isfinite(rate) or rate<=0:raise ValueError('动画 FPS 无效')
            binding=dict(name=ref.value,fps=rate,frames=count,options={k:v for k,v in _read_options(named.text).items() if k!='fps'} if named else {})
            if named:
                binding['demand']=(doc.index,key,fps_text(rate));demands.setdefault((doc.index,key),{})[fps_text(rate)]=rate
            bindings.append(binding)
        common=bindings[0]['fps']
        # Named-first without a sequence FPS supplies the implicit/direct common rate too.
        write_fps=('fps' in opts or 'demand' not in bindings[0])
        implicit_rate=common
        for ai,(ref,binding) in enumerate(zip(refs,bindings)):
            if 'demand' not in binding and not math.isclose(binding['fps'],implicit_rate,rel_tol=1e-9,abs_tol=1e-9):
                name=f'loka_s{index}_a{ai}'
                clone='$animation '+_quote(name)+' '+_quote(ref.value)+' {\n\tfps '+fps_text(binding['fps'])+'\n}\n'
                variants[name]=clone;binding['name']=name
        # Rebuild references after demand allocation below, so shared named symbols are chosen once.
        source_activity=row.source.activity or row.inferred_source_activity or ''
        source_layer=row.source_layer if row.source_layer is not None else source_activity.upper().endswith('_LAYER')
        if slot and _timer_slot(slot,profile) and (source_layer or row.source_visible is True):text=_drop_events(text)
        records.append(dict(text=text,fps=common,write_fps=write_fps,
            refs=bindings,options={k:v for k,v in _read_options(text).items() if k!='fps'},events=_event_values(text)))
    names={};named_text={}
    for doc in documents:
        for key,command in doc.animations.items():
            requested=demands.get((doc.index,key),{});original_rate=_read_options(command.text).get('fps',30.0)
            base_rate=requested.get(fps_text(original_rate),next(iter(requested.values()),round_fps(original_rate)))
            base=doc.animation_names[key]
            base_text=_rename(command.text,base)
            if 'fps' in _read_options(command.text) or requested:base_text=_options(base_text,{'fps':fps_text(base_rate)})
            named_text[base]=doc.rebase(doc.symbols(base_text))
            for ri,(rate_key,rate) in enumerate(requested.items()):
                name=base if math.isclose(rate,base_rate,rel_tol=1e-9,abs_tol=1e-9) else base+'_t'+str(ri)
                names[(doc.index,key,rate_key)]=name
                if name!=base:named_text[name]=doc.rebase(doc.symbols(_options(_rename(command.text,name),{'fps':fps_text(rate)})))
    header=[];sequences=[];merged=set();origin_expectations=[]
    for doc in documents:
        for command in doc.commands:
            if command.kind in ('sequence','includemodel','modelname','animation'):continue
            if command.kind=='weightlist':
                header.append(_rename(command.text,doc.weights[command.name.lower()]));continue
            if doc.index:
                geometry=('body','bodygroup','model','cdmaterials','texturegroup','lod','shadowlod',
                          'cbox','bbox','illumposition','eyeposition','surfaceprop','contents',
                          'opaque','mostlyopaque','staticprop','sectionframes','sequencegroupsize')
                if command.kind in geometry:continue
                metadata=('poseparameter','ikchain','ikautoplaylock','attachment','definebone',
                          'bonemerge','jigglebone','proceduralbones')
                if command.kind not in metadata:raise ValueError(f'include 库中尚不支持合并的 QC 指令: ${command.kind}')
                key=(command.kind,command.name.lower())
                if key in merged:continue
            merged.add((command.kind,command.name.lower()));header.append(doc.rebase(command.text))
    for row,record in zip(plan.sequences,records):
        text=record['text'];changes=[]
        for ref,binding in zip(_leading_refs(text),record['refs']):
            if 'demand' in binding:binding['name']=names[binding.pop('demand')]
            if ref.value!=binding['name']:changes.append((ref.start,ref.end,_quote(binding['name'])))
        text=_patch(text,changes)
        text=_retime(text,replace(row,fps=record['fps']),write_fps=record['write_fps'])
        values={'activity':(_quote(row.activity)+' '+_activity_weight(text)) if row.activity else None}
        if row.source is None:values['loop']=record['options'].get('loop')
        text=_options(text,values);sequences.append(text)
        origin_expectations.append({k:record[k] for k in ('fps','refs','options','events')})
    if any(row.reload_parts for row in plan.sequences):
        bones={name for doc in documents for path in doc.resources.values()
               if path.suffix.lower()=='.smd' for name in _nodes(path)}
        bones.update(_tokens(c.text)[1].value for doc in documents for c in doc.commands if c.kind=='definebone')
        header.append('$weightlist "loka_reload_zero_weights" {\n'+''.join(_quote(bone)+' 0\n' for bone in sorted(bones))+'}\n')
    animations=list(named_text.values())+list(variants.values())
    placeholder=None
    if any(row.placeholder for row in plan.sequences):
        resource=next((p for d in documents for p in d.resources.values() if p.suffix.lower()=='.smd'),None)
        if resource is None:raise ValueError('没有可用于占位的 SMD')
        placeholder=_placeholder(resource)
    model=('v_models/v_dual_pistola.mdl' if target_id=='dual_pistol' else
           get_weapon(target_id).view_models[0].removeprefix('models/'))
    text='// Generated by Loka: conversion source; original export is preserved.\n'
    text+='$modelname '+_quote(model)+'\n\n'+'\n\n'.join(header+animations+sequences)+'\n'
    text=round_qc_fps(text)
    validate_converted_qc(text,plan,target_id,origin_expectations=origin_expectations)
    _validate_include_bones(documents)
    return _write_output(output_dir,Path(model).stem+'.qc',text,documents,placeholder,generated_resources)


def rewrite_worldmodel_qc(source_qc,target_modelname,output_dir):
    doc=_Document(source_qc,0)
    target_modelname=safe_archive_path(target_modelname.replace('\\','/'))
    if not target_modelname.lower().endswith('.mdl'):
        raise ValueError('目标模型名称无效')
    text='\n\n'.join(doc.rebase(_rename(c.text,target_modelname) if c.kind=='modelname' else c.text)
                       for c in doc.commands)+'\n'
    text=round_qc_fps(text)
    doc.include_resources()
    result=_write_output(output_dir,Path(target_modelname).stem+'.qc',text,[doc])
    from .world_physics import MANIFEST,SOURCE_PHY
    manifest=Path(source_qc).parent/MANIFEST
    if manifest.is_file():
        shutil.copy2(manifest,result.parent/MANIFEST)
        shutil.copy2(manifest.parent/SOURCE_PHY,result.parent/SOURCE_PHY)
    return result
