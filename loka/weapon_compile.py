"""Compile conversion sources in owned stages, verify all models, then fill pack."""
from dataclasses import dataclass
from pathlib import Path
import hashlib,math,os,queue,re,shutil,struct,subprocess,tempfile,threading,time

from .compile_game import prepare_compile_game
from .decompiler import MdlDecompiler,DecompileOptions
from .core.mdl_data import SourceMdlFileData49
from .core.reader import BinaryReader
from .core.vtx import SourceVtxFile07
from .core.vtx_data import SourceVtxFileData07
from .core.vvd import SourceVvdFile04,SourceVvdFileData04
from .vmodel import read_reference_data
from .vpk import safe_archive_path
from .weapon_qc import parse_qc,_tokens,_body_tokens,_leading_refs,_Document,_read_options,_effective_frames,_event_values
from .weapon_ads import is_ads_or_mixed_activity
from .material_text import resource_key
from .core.ani import extract_anim_value
from .core.vectors import SourceVector
from .core.writers.animation import _stored_euler_to_quaternion

SUFFIXES=('.mdl','.vvd','.dx90.vtx','.dx80.vtx','.sw.vtx','.dx11.vtx','.vtx','.phy','.ani')


@dataclass
class CompileResult:
    models: list[dict]
    compiled_files: list[str]
    retired_files: list[str]
    archive_names: dict[str,str]
    stage_dir: str


def _fingerprint(path):
    with path.open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


def run_compiler(command,cwd,log_file,*,cancelled=None,on_log=None):
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    process=subprocess.Popen(command,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=flags)
    messages=queue.Queue();done=threading.Event();captured=[];reader_errors=[]
    def read():
        try:
            with Path(log_file).open('wb') as log:
                for raw in iter(process.stdout.readline,b''):
                    log.write(raw);messages.put(raw)
        except BaseException as error:reader_errors.append(error)
        finally:done.set()
    reader=threading.Thread(target=read,daemon=True);reader.start()
    def drain():
        while True:
            try:raw=messages.get_nowait()
            except queue.Empty:break
            line=raw.decode('utf-8',errors='replace').rstrip();captured.append(line)
            if on_log:on_log(line)
    try:
        while process.poll() is None or not done.is_set():
            if reader_errors:raise reader_errors[0]
            if cancelled and cancelled():raise InterruptedError('模型编译已取消')
            drain();time.sleep(0.03)
        if reader_errors:raise reader_errors[0]
        drain();return process.returncode,'\n'.join(captured)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=2)
        if done.is_set():process.stdout.close()


def _model_name(qc):
    commands=[c for c in parse_qc(qc.read_text(encoding='utf-8-sig')) if c.kind=='modelname']
    if len(commands)!=1:raise ValueError('转换 QC 必须只有一个 modelname')
    name=safe_archive_path(commands[0].name.replace('\\','/'))
    if not name.lower().endswith('.mdl') or name.lower().startswith('models/'):raise ValueError('modelname 必须是 models 下的相对 MDL 路径')
    return 'models/'+name.lower()


def _options(command):
    result={};tokens=_body_tokens(command.text)
    for i,t in enumerate(tokens):
        if t.quoted:continue
        key=t.value.lower()
        if key in ('fps','numframes','activity'):
            if i+1>=len(tokens):raise ValueError('QC 选项缺少参数')
            result[key]=tokens[i+1].value
        elif key=='loop':result[key]=True
    return result


def _validate_vtx_tables(payload,info,data):
    """Strict bounds checks for fresh compiler output; core readers are tolerant."""
    def table(base,count,offset,size,*,signed_offset=False):
        start=base+offset
        if count<0 or (count and (offset==0 or (offset<0 and not signed_offset) or start<36 or start+count*size>len(payload))):
            raise ValueError('编译 VTX 数据表越界')
        return range(start,start+count*size,size) if count else ()
    if not 1<=info.lodCount<=8:raise ValueError('编译 VTX LOD 数量无效')
    if info.materialReplacementListOffset:
        for pos in table(0,info.lodCount,info.materialReplacementListOffset,8):
            count,offset=struct.unpack_from('<ii',payload,pos)
            # Replacement records/strings use signed relative offsets. Neko
            # may place them before the list when names contain path segments.
            for item in table(pos,count,offset,6,signed_offset=True):
                index,name_offset=struct.unpack_from('<hi',payload,item);name=item+name_offset
                if not 0<=index<data.textureCount or name_offset==0 or not 36<=name<len(payload) or payload.find(b'\0',name)<0:
                    raise ValueError('编译 VTX 材质引用无效')
    # Source's two packed strip layouts differ by eight optional bytes.
    def geometry(extra):
        bodies=list(table(0,info.bodyPartCount,info.bodyPartOffset,8))
        for pos,body in zip(bodies,data.theBodyParts or []):
            count,offset=struct.unpack_from('<ii',payload,pos)
            if count!=body.modelCount:raise ValueError('编译 VTX model 数量不符')
            for model_pos,model in zip(table(pos,count,offset,8),body.theModels or []):
                lods,lod_offset=struct.unpack_from('<ii',payload,model_pos)
                if lods!=info.lodCount:raise ValueError('编译 VTX model LOD 数量不符')
                for lod in table(model_pos,lods,lod_offset,12):
                    meshes,mesh_offset=struct.unpack_from('<ii',payload,lod)
                    if meshes!=model.meshCount:raise ValueError('编译 VTX mesh 数量不符')
                    for mesh in table(lod,meshes,mesh_offset,9):
                        groups,group_offset=struct.unpack_from('<ii',payload,mesh)
                        for group in table(mesh,groups,group_offset,25+extra):
                            vertices,vertex_offset,indexes,index_offset,strips,strip_offset=struct.unpack_from('<6i',payload,group)
                            table(group,vertices,vertex_offset,9)
                            index_positions=table(group,indexes,index_offset,2)
                            if any(struct.unpack_from('<H',payload,p)[0]>=vertices for p in index_positions):
                                raise ValueError('编译 VTX 顶点索引越界')
                            if extra:
                                topology,topology_offset=struct.unpack_from('<ii',payload,group+25)
                                table(group,topology,topology_offset,2)
                            for strip in table(group,strips,strip_offset,27+extra):
                                ic,io,vc,vo,bones,flags,changes,change_offset=struct.unpack_from('<4ihBii',payload,strip)
                                if min(ic,io,vc,vo,bones)<0 or io+ic>indexes or vo+vc>vertices:
                                    raise ValueError('编译 VTX strip 引用越界')
                                table(strip,changes,change_offset,8)
    try:geometry(0)
    except ValueError as normal_error:
        try:geometry(8)
        except ValueError:raise normal_error


def _ik_samples(animation,rule,source_samples=None):
    error=rule.theCompressedIkError
    if rule.ikErrorOffset or error is None:
        raise ValueError('IK origin error data unavailable: '+animation.theName)
    scales=list(error.scale)
    if len(scales)!=6 or len(error.offset)!=6 or len(error.theAnimValues)!=6:
        raise ValueError('IK origin invalid channel table: '+animation.theName)
    start=rule.influenceStart;end=rule.influenceEnd
    if not (0<=start<=1 and 0<=end<=1):raise ValueError('IK origin phase range unsupported: '+animation.theName)
    if start<=end:
        start=max(start,rule.ikErrorIndexStart/max(animation.frameCount-1,1))
        if start>end:raise ValueError('IK origin has no covered active samples: '+animation.theName)
        phases=[start,(start+end)/2,end]
    else:
        phases=[start,(start+(1-start+end)/2)%1,end]
    frames=[round((animation.frameCount-1)*phase) for phase in phases]
    if source_samples is not None:
        phases=source_samples['phases']
        frames=source_samples.get('frames',[round((animation.frameCount-1)*p) for p in phases])
        if len(phases)!=3 or len(frames)!=3 or len(source_samples['values'])!=3:
            raise ValueError('IK origin invalid source sample coordinates')
        for phase,frame in zip(phases,frames):
            if not math.isfinite(phase) or not 0<=phase<=1 or type(frame) is not int or frame!=round((animation.frameCount-1)*phase):
                raise ValueError('IK origin invalid source sample coordinate')
            low=round((animation.frameCount-1)*rule.influenceStart)
            high=round((animation.frameCount-1)*rule.influenceEnd)
            if not ((low<=frame<=high) if low<=high else (frame>=low or frame<=high)):
                raise ValueError('IK origin source sample outside target coverage')
    values=[]
    for frame_index in frames:
        frame=frame_index-rule.ikErrorIndexStart
        if frame<0 and start>end:frame+=max(animation.frameCount-1,1)
        if frame<0:raise ValueError('IK origin sample precedes error data: '+animation.theName)
        sample=[]
        for i,channel in enumerate(error.theAnimValues):
            if channel is None and not error.offset[i]:sample.append(0.);continue
            # The general the parser decoder returns zero on malformed RLE. A
            # provenance gate must reject unknown data instead of blessing it.
            k=frame;index=0
            while True:
                if not channel or index>=len(channel):raise ValueError('IK origin RLE unavailable: '+animation.theName)
                span=channel[index]
                if not 0<span.valid<=span.total or index+span.valid>=len(channel):raise ValueError('IK origin RLE invalid: '+animation.theName)
                if k<span.total:break
                k-=span.total;index+=span.valid+1
            sample.append(extract_anim_value(frame,channel,scales[i]))
        if not all(math.isfinite(v) for v in sample+scales):raise ValueError('IK origin is not finite: '+animation.theName)
        values.append(sample)
    return {'phases':phases,'frames':frames,'values':values,'scales':scales}


def capture_animation_semantics(data,*,ik_rule_handling='default',validate_ik_errors=True,_sample_origins=None):
    """Cheap original-MDL facts; no bone-pose or all-frame evaluation."""
    origins={}
    for sequence in data.theSequenceDescs or []:
        refs=[]
        check_errors=(_sample_origins[sequence.theName].get('ik_error_guard',True)
                      if _sample_origins and sequence.theName in _sample_origins else validate_ik_errors)
        for ref_slot,index in enumerate(sequence.theAnimDescIndexes or []):
            animation=data.theAnimationDescs[index];rules=[]
            source_ref=None
            if _sample_origins and sequence.theName in _sample_origins:
                source_refs=_sample_origins[sequence.theName]['references']
                if ref_slot<len(source_refs):source_ref=source_refs[ref_slot]
            if ik_rule_handling=='default':
                for rule_slot,rule in enumerate(animation.theIkRules or []):
                    item={'type':rule.type,'chain':data.theIkChains[rule.chain].theName,
                          'bone':data.theBones[rule.bone].theName if rule.type==1 and rule.bone>=0 else None,
                          'slot':rule.slot,'range':[rule.influenceStart,rule.influencePeak,rule.influenceTail,rule.influenceEnd],
                          'pos':[rule.pos.x,rule.pos.y,rule.pos.z],'q':[rule.q.x,rule.q.y,rule.q.z,rule.q.w]}
                    if not all(math.isfinite(v) for k in ('range','pos','q') for v in item[k]):
                        raise ValueError('IK origin fields are not finite: '+animation.theName)
                    if rule.type==1 and check_errors:
                        samples=None
                        if source_ref and rule_slot<len(source_ref['ik']):samples=source_ref['ik'][rule_slot].get('samples')
                        item['samples']=_ik_samples(animation,rule,samples)
                    rules.append(item)
            reference={'delta':bool(animation.flags&4),'ik':rules,'frames':animation.frameCount}
            if ik_rule_handling=='default' and check_errors and getattr(animation,'thePreserveSourceTouchCurves',False):
                from .core.ik_curve_preservation import capture_touch_curves
                reference['touch_curves']=capture_touch_curves(data,animation)
            refs.append(reference)
        origins[sequence.theName]={'delta_post':sequence.flags&20,'references':refs,'ik_guard':ik_rule_handling=='default',
                                  'ik_error_guard':check_errors}
    return origins


def validate_animation_semantics(data,origins):
    actual=capture_animation_semantics(data,ik_rule_handling='default' if any(o['ik_guard'] for o in origins.values()) else 'usesource',_sample_origins=origins)
    for name,origin in origins.items():
        context='编译动画来源语义不符: '+name
        current=actual.get(name)
        if current is None or current['delta_post']!=origin['delta_post']:raise ValueError(context+' DELTA/POST')
        if len(current['references'])!=len(origin['references']):raise ValueError(context+' references')
        for before,after in zip(origin['references'],current['references']):
            if before['delta']!=after['delta']:raise ValueError(context+' reference DELTA')
            if before['frames']!=after['frames']:raise ValueError(context+' reference frames')
            if not origin['ik_guard']:continue
            if len(before['ik'])!=len(after['ik']):raise ValueError(context+' IK count')
            for x,y in zip(before['ik'],after['ik']):
                if any(x[k]!=y[k] for k in ('type','chain','bone','slot')):raise ValueError(context+' IK linkage')
                if not origin.get('ik_error_guard',True):continue
                # QC emits rounded authored frame indices for phase ranges.
                phase_tolerance=.5/(before['frames']-1)+1e-6 if before['frames']>1 else 1e-6
                if any(abs(p-q)>phase_tolerance for p,q in zip(x['range'],y['range'])):raise ValueError(context+' IK phase')
                if x['type']!=1:continue
                if any(abs(p-q)>.011 for p,q in zip(x['pos'],y['pos'])):raise ValueError(context+' IK fakeorigin')
                # Rule q is stored as full xyzw; zero sentinel is not a unit quaternion.
                dot=sum(p*q for p,q in zip(x['q'],y['q']))
                old_norm=sum(p*p for p in x['q']);new_norm=sum(p*p for p in y['q'])
                if old_norm<1e-8:
                    # Compiled zero is the unauthored fakerotate sentinel. The
                    # reconstructed-source modifier materializes its identity
                    # rotation; accept only zero or that same identity.
                    if new_norm>1e-8 and abs(y['q'][3])/math.sqrt(new_norm)<math.cos(math.radians(.02)/2):
                        raise ValueError(context+' IK rotation')
                elif new_norm<1e-8 or abs(dot)/math.sqrt(old_norm*new_norm)<math.cos(math.radians(.02)/2):raise ValueError(context+' IK rotation')
                if len(x['samples']['values'])!=3 or any(len(v)!=6 or not all(math.isfinite(t) for t in v) for v in x['samples']['values']):raise ValueError(context+' IK source samples')
                for old,new in zip(x['samples']['values'],y['samples']['values']):
                    for i,(p,q) in enumerate(zip(old[:3],new[:3])):
                        difference=abs(p-q)
                        tolerance=max(.025,4*max(x['samples']['scales'][i],y['samples']['scales'][i]))
                        if difference>tolerance:raise ValueError(context+f' IK error channel {i}')
                    # Compressed error rotation uses the same RadianEuler XYZ
                    # convention as the core animation decoder. Compare the
                    # orientation, so equivalent gimbal triples remain valid.
                    q=_stored_euler_to_quaternion(SourceVector(*old[3:]))
                    r=_stored_euler_to_quaternion(SourceVector(*new[3:]))
                    angle=2*math.acos(min(1.,abs(sum(getattr(q,c)*getattr(r,c) for c in 'xyzw'))))
                    tolerance=max(.001,4*sum(max(x['samples']['scales'][i],y['samples']['scales'][i]) for i in range(3,6)))
                    if angle>tolerance:raise ValueError(context+' IK error rotation')


def _validated_qc_references(data,qc,*,doc=None):
    """Resolve physical destinations against the converted QC, including retiming."""
    doc=doc or _Document(qc,0)
    sequences=[c for c in doc.commands if c.kind=='sequence']
    actual=data.theSequenceDescs or [];animations=data.theAnimationDescs or []
    if [s.theName for s in actual]!=[s.name for s in sequences]:
        raise ValueError('Compiled sequence order/count differs from QC')
    result={};identities={};destinations={}
    for source,sequence in zip(sequences,actual):
        options=_options(source);source_options=_read_options(source.text)
        refs=_leading_refs(source.text);indexes=sequence.theAnimDescIndexes or []
        first_named=doc.animations.get(refs[0].value.lower()) if refs else None
        sequence_rate=float(options.get('fps',_read_options(first_named.text).get('fps',30) if first_named else 30))
        if len(refs)!=len(indexes):raise ValueError('Compiled reference count differs from QC: '+source.name)
        for slot,(ref,index) in enumerate(zip(refs,indexes)):
            if not 0<=index<len(animations):raise ValueError('Compiled animation index is invalid')
            key=ref.value.lower();command=doc.animations.get(key)
            if command is None:
                if not key.endswith('.smd'):raise ValueError('Unknown QC animation reference: '+ref.value)
                command=type(source)('animation','implicit','$animation "implicit" "'+ref.value+'" { }')
            expected_frames=doc.animation_frames(command);rate=float(_options(command).get('fps',30))
            if key.endswith('.smd'):
                rate=sequence_rate;expected_frames=_effective_frames(expected_frames,source_options)
                identity=('direct',source.name,slot)
            else:
                if key==refs[0].value.lower():rate=sequence_rate
                identity=('named',key)
                if animations[index].theName.lower()!=key:
                    raise ValueError('Compiled named reference identity differs from QC: '+source.name)
            animation=animations[index]
            if not math.isfinite(rate) or rate<=0 or animation.frameCount!=expected_frames or not math.isclose(animation.fps,rate,rel_tol=1e-5,abs_tol=1e-4):
                raise ValueError('Compiled animation frames/FPS differ from QC: '+source.name+' / '+ref.value)
            if identity in identities and identities[identity]!=index:
                raise ValueError('One QC reference maps to multiple physical descriptors')
            if index in destinations and destinations[index]!=identity:
                raise ValueError('Multiple QC identities share one physical descriptor')
            identities[identity]=index;destinations[index]=identity
            result[(source.name,slot)]={'descriptor':index,'identity':identity,'frames':expected_frames,'fps':rate}
    return result


def validate_compiled_model(model,qc,*,keep_ads=True,bindings=(),semantic_origins=None):
    raw=model.read_bytes();header=read_reference_data(raw)
    if header.version>49 or header.fileSize!=len(raw):raise ValueError('编译 MDL 版本/长度不正确')
    from .core.animation_sections import validate_animation_sections
    section_verification=validate_animation_sections(raw)
    expected=_model_name(qc).removeprefix('models/')
    if header.theModelName.replace('\\','/').lower()!=expected:raise ValueError('编译 MDL 内部名称与目标不符')
    dec=MdlDecompiler(DecompileOptions(handle_vmodel_includes=False));dec.mdl_file_data=SourceMdlFileData49();dec._read_mdl_file(str(model));data=dec.mdl_file_data
    for count,values in ((data.boneCount,data.theBones),(data.localAnimationCount,data.theAnimationDescs),
                         (data.localSequenceCount,data.theSequenceDescs),(data.textureCount,data.theTextures)):
        if count!=len(values or []):raise ValueError('编译 MDL 描述表不完整')
    if semantic_origins is not None:validate_animation_semantics(data,semantic_origins)
    doc=_Document(qc,0);sequences=[c for c in doc.commands if c.kind=='sequence']
    actual=data.theSequenceDescs or []
    if [s.theName for s in actual]!=[s.name for s in sequences]:raise ValueError('编译 sequence 顺序/数量与 QC 不符')
    references=_validated_qc_references(data,qc,doc=doc)
    animations=data.theAnimationDescs or [];reference_count=len(references)
    for source,sequence in zip(sequences,actual):
        options=_options(source);source_options=_read_options(source.text);activity=options.get('activity','')
        if sequence.theActivityName!=activity:raise ValueError('编译 Activity 与 QC 不符: '+source.name)
        if not keep_ads and is_ads_or_mixed_activity(activity):raise ValueError('编译模型残留 ADS/MIXED')
        refs=_leading_refs(source.text);indexes=sequence.theAnimDescIndexes or []
        if source_options.get('loop') and not sequence.flags&1:raise ValueError('编译循环标志与 QC 不符: '+source.name)
        expected_events=_event_values(source.text);events=sequence.theEvents or []
        if len(expected_events)!=len(events):raise ValueError('编译事件数量与 QC 不符: '+source.name)
        first_frames=animations[indexes[0]].frameCount if indexes else 1
        for expected_event,event in zip(expected_events,events):
            kind,frame,*event_options=expected_event
            option=event_options[0] if event_options else ''
            cycle=float(frame)/max(first_frames-1,1)
            if event.theName!=kind or event.options.rstrip('\x00')!=option or not math.isclose(event.cycle,cycle,rel_tol=1e-5,abs_tol=1e-5):
                raise ValueError('编译事件/周期与 QC 不符: '+source.name)
    names={t.thePathFileName.replace('\\','/').lower() for t in data.theTextures or []}
    for binding in bindings:
        if binding['qc']!=str(qc):continue
        if binding['target_name'].replace('\\','/').lower() not in names:raise ValueError('编译模型缺少材质映射')
        if binding.get('binding_strategy')=='directories':
            directories=[d.replace('\\','/').strip('/').lower() for d in data.theTexturePaths or []] or ['']
            expected=[d.replace('\\','/').strip('/').lower() for d in binding['search_directories']]
            if directories!=expected:raise ValueError('编译模型材质查找目录与计划不符')
            candidates={resource_key((d+'/' if d else '')+binding['target_name'],'.vmt') for d in directories}
            if binding['archive_path'].lower() not in candidates:raise ValueError('编译模型材质目录无法定位目标文件')
    geometry=any(m.vertexCount for b in data.theBodyParts or [] for m in b.theModels or [])
    vvd=model.with_suffix('.vvd');vtx=model.with_suffix('.dx90.vtx')
    if geometry and (not vvd.is_file() or not vtx.is_file()):raise ValueError('编译模型缺少 VVD/DX90 VTX')
    if vvd.is_file():
        payload=vvd.read_bytes();info=SourceVvdFileData04();SourceVvdFile04(BinaryReader(payload),info).read_source_vvd_header()
        if info.id!='IDSV' or info.version!=4 or info.checksum!=data.checksum or not 1<=info.lodCount<=8:raise ValueError('编译 VVD 标头/checksum 无效')
        count=info.lodVertexCount[0]
        if count<0 or (count and (info.vertexDataOffset<64 or info.vertexDataOffset+48*count>len(payload)
                                  or info.tangentDataOffset+16*count>len(payload))):raise ValueError('编译 VVD 数据越界')
    for file in model.parent.glob(model.stem+'*.vtx'):
        payload=file.read_bytes();info=SourceVtxFileData07();parser=SourceVtxFile07(BinaryReader(payload),info);parser.read_source_vtx_header()
        if info.version not in (6,7) or info.checksum!=data.checksum or info.bodyPartCount!=data.bodyPartCount:raise ValueError('编译 VTX 标头/checksum 无效')
        _validate_vtx_tables(payload,info,data)
        parser.read_source_vtx_material_replacement_lists()
        expected_names={b['target_name'].replace('\\','/').lower() for b in bindings if b['qc']==str(qc)}
        source_names={b['source_name'].replace('\\','/').lower() for b in bindings if b['qc']==str(qc)}
        for group in info.theVtxMaterialReplacementLists or []:
            for replacement in group.theVtxMaterialReplacements or []:
                name=replacement.theName.replace('\\','/').lower()
                if expected_names and name not in expected_names and name in source_names:
                    raise ValueError('编译 LOD 残留旧材质引用')
    phy=model.with_suffix('.phy');needs_phy=any(c.kind in ('collisionmodel','collisionjoints') for c in doc.commands)
    if needs_phy and not phy.is_file():raise ValueError('编译模型缺少 PHY')
    if phy.is_file():
        payload=phy.read_bytes()
        if len(payload)<16 or struct.unpack_from('<i',payload,12)[0]!=data.checksum:raise ValueError('编译 PHY checksum 无效')
    return data,{'sequences':len(actual),'animation_references':reference_count,'checksum':data.checksum,'materials':sorted(names),
                 'animation_sections':section_verification}


def compile_and_fill(pack_dir,qc_paths,compiler_path,*,game_path='',retired_models=(),archive_names=None,
                     keep_ads=True,bindings=(),cancelled=None,on_log=None,semantic_origins=None):
    pack=Path(pack_dir).resolve();compiler=Path(compiler_path).resolve()
    if pack.name!='pack' or not pack.is_dir():raise ValueError('编译回填只能使用独立 pack')
    if not compiler.is_file():raise ValueError('未找到配置的模型编译器')
    qcs=[Path(p).resolve() for p in qc_paths]
    if not qcs:raise ValueError('没有可编译的转换 QC')
    for qc in qcs:
        if not qc.is_relative_to(pack.parent) or any(qc.is_relative_to(pack.parent/n) for n in ('pack','unpack','source')):raise ValueError('编译输入必须是独立转换 QC')
    targets=[_model_name(qc) for qc in qcs]
    if len(set(targets))!=len(targets):raise ValueError('多个 QC 输出同一模型路径')
    retired={safe_archive_path(str(p).replace('\\','/')).lower() for p in retired_models}
    if any(not p.startswith('models/') or not p.endswith('.mdl') for p in retired):raise ValueError('清理来源必须是已识别主模型')
    def check():
        if cancelled and cancelled():raise InterruptedError('编译回填已取消')
    check();inventory={};names={}
    for path in pack.rglob('*'):
        if not path.is_file():continue
        key=safe_archive_path(path.relative_to(pack).as_posix()).lower()
        if not path.resolve().is_relative_to(pack) or key in inventory:raise ValueError('pack 路径越界/冲突')
        inventory[key]=path;names[key]=(archive_names or {}).get(key,path.relative_to(pack).as_posix())
    removed={key for model in retired for suffix in SUFFIXES for key in inventory if key==model[:-4]+suffix}
    stage=Path(tempfile.mkdtemp(prefix='.loka_compile_models_',dir=pack.parent));generated={};models=[]
    for i,(qc,target) in enumerate(zip(qcs,targets)):
        check();job=stage/str(i);job.mkdir()
        game=Path(prepare_compile_game(str(job),game_path,str(pack),extra_dependency_paths=[qc.parent]))
        code,output=run_compiler([str(compiler),'-nop4','-game',str(game),'-i',str(qc)],qc.parent,job/'compiler.log',cancelled=cancelled,on_log=on_log)
        if code!=0 or re.search(r'(?mi)^\s*(?:ERROR(?:\s|:)|FATAL\b|EXCEPTION_ACCESS_VIOLATION)',output):raise ValueError('模型编译失败: '+str(qc)+'\n'+output[-4000:])
        model=game/target
        if not model.is_file():raise ValueError('编译器未生成预期 MDL: '+target)
        from .world_physics import preserve_compiled_world_physics
        preserve_compiled_world_physics(model,qc,staging_root=stage)
        from .core.animation_sections import repair_compiled_animation_sections
        section_repairs=repair_compiled_animation_sections(model,staging_root=stage)
        if section_repairs and on_log:on_log('已修复编译模型的动作分段偏移: '+str(len(section_repairs))+' 项\n')
        origins=None if semantic_origins is None else semantic_origins[str(qc)]
        from .core.ik_curve_preservation import preserve_compiled_touch_curves
        restoration=preserve_compiled_touch_curves(model,origins,staging_root=stage,qc=qc)
        data,verification=validate_compiled_model(model,qc,keep_ads=keep_ads,bindings=bindings,semantic_origins=origins)
        from .world_physics import validate_compiled_world_physics
        world_physics=validate_compiled_world_physics(model,qc)
        if world_physics:verification['world_physics']=world_physics
        if restoration:verification['touch_curve_restoration']=restoration
        if section_repairs:verification['animation_section_repairs']=section_repairs
        for file in model.parent.iterdir():
            if file.is_file() and file.name.lower() in {model.stem.lower()+s for s in SUFFIXES}:
                key=file.relative_to(game).as_posix().lower()
                if key in generated:raise ValueError('编译输出文件重复')
                generated[key]=file
        # Included world-model binaries are retained/copied, never silently removed.
        queue_models=list(data.theModelGroups or []);seen=set()
        while queue_models:
            ref=queue_models.pop(0).theFileName.replace('\\','/');ref=ref if ref.lower().startswith('models/') else 'models/'+ref
            ref=safe_archive_path(ref).lower()
            if ref in seen:continue
            seen.add(ref)
            if ref in removed:raise ValueError('目标仍引用即将移除的来源主模型: '+ref)
            library=generated.get(ref,inventory.get(ref))
            if library is None:
                library=qc.parent/ref
                if not library.resolve().is_relative_to(qc.parent) or not library.is_file():raise ValueError('编译 include 依赖缺失: '+ref)
                for suffix in SUFFIXES:
                    candidate=library.with_name(library.stem+suffix)
                    if candidate.is_file():generated[ref[:-4]+suffix]=candidate
            queue_models.extend(read_reference_data(library.read_bytes()).theModelGroups or [])
        models.append(dict(verification,qc=str(qc),model=target,compiler_log=str(job/'compiler.log')))
    check()
    for key in generated:
        if key in inventory and key not in removed:raise ValueError('编译目标覆盖未识别原资源: '+key)
        if not (pack/key).resolve().is_relative_to(pack):raise ValueError('编译目标路径越界')
    affected=removed|set(generated);backup=stage/'before';backup.mkdir();snapshots={}
    for key in affected:
        if key in inventory:
            original=inventory[key];stored=backup/key;stored.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,stored);snapshots[key]=stored
    generated_hashes={key:_fingerprint(path) for key,path in generated.items()};written=[];deleted=[]
    try:
        for key in sorted(generated):
            check();source=generated[key];destination=inventory.get(key,pack/key);destination.parent.mkdir(parents=True,exist_ok=True)
            written.append((key,destination));shutil.copy2(source,destination)
            if _fingerprint(destination)!=generated_hashes[key]:raise ValueError('回填模型复制校验失败')
        for key in removed-set(generated):
            check();inventory[key].unlink();deleted.append(key)
        check()
    except BaseException as failure:
        errors=[]
        for key in deleted:
            try:shutil.copy2(snapshots[key],inventory[key])
            except BaseException as error:errors.append(str(error)+'；原工作文件备份: '+str(snapshots[key]))
        for key,destination in reversed(written):
            try:
                if key in snapshots:shutil.copy2(snapshots[key],destination)
                elif destination.exists():destination.unlink()
            except BaseException as error:errors.append(str(error)+'；备份目录: '+str(backup))
        if errors:failure.add_note('回填回滚未完全成功: '+'; '.join(errors))
        raise
    for key in removed:names.pop(key,None)
    names.update({key:key for key in generated})
    return CompileResult(models,sorted(generated),sorted(removed),names,str(stage))
