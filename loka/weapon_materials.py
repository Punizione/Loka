"""Owned pack material isolation, HUD mapping and independent QC bindings."""
from dataclasses import dataclass,field
from pathlib import Path,PurePosixPath
import hashlib
import secrets
import re
import shutil
import os

from .weapon_catalog import get_weapon
from .vpk import safe_archive_path
from .weapon_qc import _tokens,parse_qc,_patch,_resource_tokens
from .material_text import patch_material,resource_key,material_pairs,TEXTURES,MATERIALS
from .pcf_materials import patch_pcf_materials,pcf_attributes
from .vmt_normalize import normalize_vmt as normalize_vmt_bytes,normalized_material_pairs,patch_normalized_material


def is_hud_material(key):
    return str(key).replace('\\','/').lower().startswith(('materials/vgui/hud/','materials/ping_system/'))


def digest(path):
    with path.open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


@dataclass(frozen=True)
class MaterialChange:
    source: Path
    destination: Path
    source_archive: str
    archive_path: str
    source_digest: str
    before: bytes | None
    after: bytes | None
    retain_source: bool=False


@dataclass(frozen=True)
class MaterialQcChange:
    path: Path
    before: bytes
    after: bytes


@dataclass
class MaterialPlan:
    root: Path
    salt: str
    files: list[MaterialChange]=field(default_factory=list)
    qc_edits: list[MaterialQcChange]=field(default_factory=list)
    archive_names: dict[str,str]=field(default_factory=dict)
    decisions: list[dict]=field(default_factory=list)
    bindings: list[dict]=field(default_factory=list)
    resource_checks: dict[Path,str]=field(default_factory=dict)
    warnings: list[str]=field(default_factory=list)
    normalization_repairs: list[dict]=field(default_factory=list)


@dataclass
class MaterialResult:
    salt: str
    file_manifest: list[dict]
    qc_bindings: list[dict]
    reference_edits: list[dict]
    archive_names: dict[str,str]
    warnings: list[str]
    normalization_repairs: list[dict]=field(default_factory=list)


def mesh_materials(path,checks):
    materials=[];checksum=hashlib.sha256()
    with path.open('rb') as handle:
        def lines():
            for raw in handle:
                checksum.update(raw)
                yield raw.decode('utf-8-sig').strip()
        iterator=iter(lines())
        for line in iterator:
            if line!='triangles':continue
            for name in iterator:
                if name=='end':break
                materials.append(name.strip('"'))
                for _ in range(3):
                    vertex=next(iterator,None)
                    if vertex is None or vertex=='end':raise ValueError('SMD triangles 不完整: '+str(path))
            break
        for _ in iterator:pass
    checks[path]=checksum.hexdigest()
    return materials


def _material_name(value):
    value=value.replace('\\','/')
    return value[:-4] if value.lower().endswith('.vmt') else value


def _material_quote(value):
    if any(c in value for c in '"\r\n\x00`'):raise ValueError('QC 材质名称不安全')
    return '"'+value+'"'


def _resolve_material(name,directories,inventory):
    for directory in directories:
        key=resource_key((directory+'/' if directory else '')+name,'.vmt')
        if key in inventory:return key,directory
    return None,None


def _mesh_reference_edit(path,replacements,checks):
    """Change triangle material lines only; preserve skeleton/vertices bytewise."""
    before=path.read_bytes()
    if hashlib.sha256(before).hexdigest()!=checks[path]:raise ValueError('规划时 mesh 材质来源已修改: '+str(path))
    lines=before.splitlines(keepends=True);offset=0;remaining=0;active=False;changes=[]
    for raw in lines:
        value=raw.decode('utf-8-sig').strip()
        if not active:
            if value=='triangles':active=True
        elif remaining:remaining-=1
        elif value=='end':break
        else:
            old=value.strip('"');new=replacements.get(old.lower(),old)
            if new!=old:
                start=len(raw)-len(raw.lstrip(b' \t'));end=len(raw.rstrip(b' \t\r\n'))
                quoted=raw[start:end].startswith(b'"')
                content=(_material_quote(new) if quoted else new).encode('utf-8')
                changes.append((offset+start,offset+end,content))
            remaining=3
        offset+=len(raw)
    if not changes:return None
    pieces=[];end=0
    for start,stop,new in changes:pieces.extend((before[end:start],new));end=stop
    pieces.append(before[end:])
    return MaterialQcChange(path,before,b''.join(pieces))


def qc_material_bindings(qc,inventory,mapping,checks,*,retained=(),mesh_intents=None):
    before=qc.read_bytes();text=before.decode('utf-8-sig');commands=parse_qc(text)
    directory_commands=[c for c in commands if c.kind=='cdmaterials']
    directories=[c.name.replace('\\','/').strip('/') for c in directory_commands] or ['']
    names=[];meshes={}
    for command in commands:
        if command.kind=='renamematerial':raise ValueError('转换 QC 已存在 renamematerial，需先核对映射')
        if command.kind in ('body','bodygroup','model','lod','shadowlod'):
            for token in _resource_tokens(command.text):
                if not token.value.lower().endswith('.smd'):continue
                path=(qc.parent/token.value.replace('\\','/')).resolve()
                if not path.is_relative_to(qc.parent) or not path.is_file():raise ValueError('QC mesh 资源越界或缺失')
                if path not in meshes:meshes[path]=mesh_materials(path,checks)
                names.extend(meshes[path])
        tokens=_tokens(command.text)
        if command.kind=='texturegroup':names.extend(t.value for t in tokens[2:] if t.quoted or t.value not in ('{','}'))
        for index,token in enumerate(tokens):
            if not token.quoted and token.value.lower()=='replacematerial':
                if index+2>=len(tokens):raise ValueError('QC replacematerial 缺少参数')
                names.extend((tokens[index+1].value,tokens[index+2].value))
    entries={};unknown=False
    for name in names:
        if name.lower() in entries:continue
        found,directory=_resolve_material(name,directories,inventory)
        if found is None:unknown=True;continue
        entries[name.lower()]=dict(name=name,source=found,directory=directory,target=mapping.get(found,found))
    affected=any(e['source'] in mapping for e in entries.values())
    replacements={};edits=[];bindings=[]
    if affected:
        final_inventory=(set(inventory)-set(mapping))|set(mapping.values())|set(retained)
        proposed=[];fallback=[];possible=True
        for directory in directories:
            group=[e for e in entries.values() if e['directory']==directory]
            moved=[e for e in group if e['source'] in mapping]
            for entry in moved:
                target=entry['target'].removeprefix('materials/').removesuffix('.vmt')
                name=_material_name(entry['name'])
                if target.lower()==name.lower():prefix=''
                elif target.lower().endswith('/'+name.lower()):prefix=target[:-len(name)-1]
                else:possible=False;break
                if prefix not in proposed:proposed.append(prefix)
            if unknown or (not moved and directory) or any(e['source'] not in mapping for e in group):fallback.append(directory)
        new_directories=list(dict.fromkeys(proposed+fallback))
        # An implicit root must stay implicit; never introduce $cdmaterials "".
        if '' in new_directories and new_directories!=directories:possible=False
        if '' in new_directories and not any(c.name=='' for c in directory_commands):
            possible=possible and new_directories==[''] and not directory_commands
        if possible:
            possible=all(_resolve_material(e['name'],new_directories,final_inventory)[0]==e['target'] for e in entries.values())
            possible=possible and all(_resolve_material(n,new_directories,final_inventory)[0] is None
                                     for n in names if n.lower() not in entries)
        if not possible:
            # Real filename/nonuniform-relative-path changes are expressed in
            # conversion copies, retaining the original search-directory order.
            new_directories=directories
            for key,entry in entries.items():
                if entry['source'] not in mapping:continue
                target=entry['target'].removeprefix('materials/').removesuffix('.vmt')
                prefix=(entry['directory']+'/') if entry['directory'] else ''
                if not target.lower().startswith(prefix.lower()):raise ValueError('材质目标无法保留原查找目录: '+entry['name'])
                new=target[len(prefix):]
                if _material_name(entry['name']).lower()!=new.lower():replacements[key]=new
            for key,entry in entries.items():
                if _resolve_material(replacements.get(key,entry['name']),new_directories,final_inventory)[0]!=entry['target']:
                    raise ValueError('转换材质引用存在目录冲突: '+entry['name'])
            if any(_resolve_material(n,new_directories,final_inventory)[0] is not None for n in names if n.lower() not in entries):
                raise ValueError('转换材质目录覆盖未知外部引用')
        changes=[];offset=0;directory_index=0
        for command in commands:
            start=text.find(command.text,offset);offset=start+len(command.text);tokens=_tokens(command.text)
            if command.kind=='cdmaterials':
                value=(_material_quote(new_directories[directory_index]+'/') if new_directories[directory_index] else '""') if directory_index<len(new_directories) else None
                if value is None:changes.append((start+tokens[0].start,start+tokens[1].end,''))
                else:changes.append((start+tokens[1].start,start+tokens[1].end,value))
                directory_index+=1
            if command.kind=='texturegroup':
                for token in tokens[2:]:
                    if token.value.lower() in replacements:changes.append((start+token.start,start+token.end,_material_quote(replacements[token.value.lower()])))
            for index,token in enumerate(tokens):
                if not token.quoted and token.value.lower()=='replacematerial':
                    for ref in tokens[index+1:index+3]:
                        if ref.value.lower() in replacements:changes.append((start+ref.start,start+ref.end,_material_quote(replacements[ref.value.lower()])))
        after=(b'\xef\xbb\xbf' if before.startswith(b'\xef\xbb\xbf') else b'')+_patch(text,changes).encode('utf-8')
        extra=new_directories[directory_index:]
        if not directory_commands and new_directories==['']:extra=[]
        if extra:after+=b'\n'+('\n'.join('$cdmaterials '+_material_quote(d+'/') for d in extra)+'\n').encode('utf-8')
        if after!=before:edits.append(MaterialQcChange(qc,before,after))
        for key,entry in entries.items():
            bindings.append(dict(qc=str(qc),source_name=entry['name'],source_archive=entry['source'],
                target_name=_material_name(replacements.get(key,entry['name'])),archive_path=entry['target'],
                search_directories=new_directories,binding_strategy='directories'))
    for path,materials in meshes.items():
        intent={name.lower():_material_name(replacements.get(name.lower(),name)).lower() for name in materials}
        if mesh_intents is not None:
            if path in mesh_intents and mesh_intents[path]!=intent:raise ValueError('共享转换 mesh 需要不同材质引用: '+str(path))
            mesh_intents[path]=intent
        edit=_mesh_reference_edit(path,replacements,checks) if replacements else None
        if edit:edits.append(edit)
    return edits,bindings


def plan_material_conversion(pack_dir,qc_paths,source_id,target_id,*,isolate=False,salt=None,original_names=None,normalize_vmt=False):
    get_weapon(source_id);get_weapon(target_id)
    root=Path(pack_dir).resolve()
    if not root.is_dir() or root.name!='pack':raise ValueError('材质转换只能使用独立 pack 目录')
    qc_paths=[Path(p).resolve() for p in qc_paths]
    for qc in qc_paths:
        if (not qc.is_relative_to(root.parent) or any(qc.is_relative_to(root.parent/name) for name in ('pack','unpack','source'))):
            raise ValueError('材质 QC 必须是工作区独立转换源')
    salt=(salt or secrets.token_hex(2)) if isolate else ''
    if isolate and not re.fullmatch('[a-f0-9]{4}',salt):raise ValueError('材质目录必须为四位小写十六进制')
    files={};archive_names={}
    def strict_paths():
        def failed(error):raise OSError(error.errno,'VMT 规范化无法读取目录: '+str(error),error.filename) from error
        for directory,directories,filenames in os.walk(root,followlinks=False,onerror=failed):
            for name in directories+filenames:
                path=Path(directory)/name
                if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):raise ValueError('VMT 规范化资源不允许别名路径: '+str(path))
            for name in filenames:yield Path(directory)/name
    for path in strict_paths() if normalize_vmt else root.rglob('*'):
        if not path.is_file():continue
        if not path.resolve().is_relative_to(root):raise ValueError('pack 路径越界')
        key=safe_archive_path(path.relative_to(root).as_posix()).lower()
        if key in files:raise ValueError('pack 路径大小写冲突')
        files[key]=path;archive_names[key]=(original_names or {}).get(key,path.relative_to(root).as_posix())
    mapping={}
    for key in files:
        if not key.startswith('materials/') or PurePosixPath(key).suffix not in ('.vmt','.vtf'):continue
        rel=PurePosixPath(key)
        if is_hud_material(key):
            if rel.stem=='icon_'+source_id:
                mapping[key]=(rel.parent/('icon_'+target_id+rel.suffix)).as_posix()
        elif isolate:mapping[key]=(rel.parent/salt/rel.name).as_posix()
    mapping={s:d for s,d in mapping.items() if s!=d}
    destinations=set()
    for source,destination in mapping.items():
        target=root/safe_archive_path(destination)
        if destination in files or destination in destinations:raise ValueError('目标材质/HUD 路径已存在: '+destination)
        if not target.resolve().is_relative_to(root):raise ValueError('材质目标路径越界')
        for parent in target.parents:
            if parent==root:break
            if parent.is_file():raise ValueError('材质目标目录是文件')
        destinations.add(destination)
    plan=MaterialPlan(root,salt,archive_names=dict(archive_names))
    payloads={};original_payloads={};repair_records={}
    def payload(path):
        if path not in payloads:
            raw=path.read_bytes();original_payloads[path]=raw
            after=raw
            if normalize_vmt and path.suffix.lower()=='.vmt':
                try:after,repairs=normalize_vmt_bytes(raw,available_resources=files)
                except ValueError as error:raise ValueError('VMT 规范化失败 '+str(path)+': '+str(error)) from error
                plan.resource_checks[path]=hashlib.sha256(raw).hexdigest()
                for repair in repairs:
                    witness=repair.get('witness_resource')
                    if witness is not None:plan.resource_checks[files[witness]]=digest(files[witness])
                if repairs:
                    record={'source_archive':path.relative_to(root).as_posix().lower(),
                        'source_sha256':hashlib.sha256(raw).hexdigest(),'normalized_sha256':hashlib.sha256(after).hexdigest(),
                        'repairs':repairs,'outputs':[]}
                    repair_records[path]=record;plan.normalization_repairs.append(record)
            payloads[path]=after
        return payloads[path]
    def original_digest(path):
        return hashlib.sha256(original_payloads[path]).hexdigest() if path in original_payloads else digest(path)
    if normalize_vmt:
        # Validate all VMTs up front, outside dependency fallback handling, so
        # ambiguous input cannot be hidden by the old retain-HUD warning path.
        for key,path in files.items():
            if path.suffix.lower()=='.vmt':payload(path)
    retained=set()
    if not isolate and mapping:
        def dependencies(data):
            pairs,_=(normalized_material_pairs(data) if normalize_vmt else material_pairs(data));found=set()
            for key,value,_ in pairs:
                kind='.vtf' if key.value.lower() in TEXTURES else '.vmt' if key.value.lower() in MATERIALS else None
                if kind:
                    resource=resource_key(value.value,kind)
                    if resource in mapping:found.add(resource)
            return found
        for key,path in files.items():
            try:
                if key.endswith('.vmt') and not is_hud_material(key):
                    raw=payload(path);plan.resource_checks[path]=original_digest(path)
                    retained.update(dependencies(raw))
                elif key.endswith('.pcf'):
                    raw=payload(path)
                    plan.resource_checks[path]=original_digest(path)
                    if any(PurePosixPath(source).stem.encode() in raw.lower() for source in mapping):
                        for attribute in pcf_attributes(raw):
                            if attribute['key'].lower()=='material':
                                for value in attribute['values']:
                                    reference=resource_key(value,'.vmt')
                                    if reference in mapping:retained.add(reference)
            except ValueError:
                retained.update(mapping)
                plan.warnings.append('无法完整核对未修改资源的 HUD 依赖，保留旧 HUD 副本: '+key)
        todo=list(retained);seen=set()
        while todo:
            source=todo.pop()
            if source in seen:continue
            seen.add(source)
            if source.endswith('.vmt'):
                for dependency in dependencies(payload(files[source])):
                    if dependency not in retained:retained.add(dependency);todo.append(dependency)
    for key,path in files.items():
        destination=mapping.get(key,key);before=None;after=None;decisions=[]
        if key.endswith('.vmt') and normalize_vmt:
            normalized=payload(path);before=original_payloads[path];after=normalized
            if key.startswith('materials/') and (isolate or is_hud_material(key)):
                after,decisions=patch_normalized_material(normalized,mapping)
        elif key.endswith('.vmt') and key.startswith('materials/') and (isolate or is_hud_material(key)):
            before=payload(path);after,decisions=patch_material(before,mapping)
        elif isolate and key.endswith('.pcf'):
            before=payload(path);after,decisions=patch_pcf_materials(before,mapping)
        if destination==key and (after is None or after==before):continue
        plan.files.append(MaterialChange(path,files.get(destination,root/destination),key,destination,
            original_digest(path),before,after,key in retained))
        if path in repair_records:
            repair_records[path]['outputs'].append({'archive_path':destination,'sha256':hashlib.sha256(after).hexdigest()})
        if normalize_vmt and key in retained and key.endswith('.vmt') and payload(path)!=original_payloads[path] and destination!=key:
            # An unchanged ordinary material may still need the old HUD name.
            # Its retained VMT must also receive normalization, without remapping
            # its old dependency path or losing the original rollback snapshot.
            plan.files.append(MaterialChange(path,path,key,key,original_digest(path),original_payloads[path],payload(path),True))
            repair_records[path]['outputs'].append({'archive_path':key,'sha256':hashlib.sha256(payload(path)).hexdigest()})
        if key not in retained:plan.archive_names.pop(key,None)
        plan.archive_names[destination]=destination if destination!=key else archive_names[key]
        plan.decisions.extend(dict(item,material=key) for item in decisions)
    if any(source.endswith('.vmt') for source in mapping):
        mesh_intents={};edits_by_path={}
        for qc in qc_paths:
            edits,bindings=qc_material_bindings(qc,files,mapping,plan.resource_checks,retained=retained,mesh_intents=mesh_intents)
            for edit in edits:
                if edit.path in edits_by_path and edits_by_path[edit.path].after!=edit.after:raise ValueError('共享转换源材质编辑冲突')
                edits_by_path[edit.path]=edit
            plan.bindings.extend(bindings)
        plan.qc_edits.extend(edits_by_path.values())
    return plan


def apply_material_conversion(plan,*,cancelled=None):
    def check():
        if cancelled and cancelled():raise InterruptedError('材质转换已取消')
    check()
    for change in plan.files:
        for path in (change.source,change.destination):
            if not path.resolve().is_relative_to(plan.root):raise ValueError('材质修改路径越界')
        if digest(change.source)!=change.source_digest:raise ValueError('规划后源材质已修改: '+str(change.source))
        if change.source!=change.destination and change.destination.exists():raise ValueError('规划后目标材质路径已存在')
    for edit in plan.qc_edits:
        if edit.path.read_bytes()!=edit.before:raise ValueError('规划后材质 QC 已修改')
    for resource,checksum in plan.resource_checks.items():
        if digest(resource)!=checksum:raise ValueError('规划后 mesh 材质来源已修改: '+str(resource))
    written=[];edited=[];deleted=[]
    try:
        for change in plan.files:
            check();change.destination.parent.mkdir(parents=True,exist_ok=True)
            if change.source==change.destination:
                written.append(change);change.destination.write_bytes(change.after)
            else:
                with change.destination.open('xb') as target:
                    written.append(change)
                    if change.after is not None:target.write(change.after)
                    else:
                        with change.source.open('rb') as source:
                            while data:=source.read(1024*1024):check();target.write(data)
                if change.after is None and digest(change.destination)!=change.source_digest:raise ValueError('复制时源纹理发生变化')
        for edit in plan.qc_edits:
            check();edited.append(edit);edit.path.write_bytes(edit.after)
        for change in plan.files:
            if change.source!=change.destination and not change.retain_source:check();change.source.unlink();deleted.append(change)
    except BaseException as failure:
        errors=[];restore_failed=set()
        for edit in reversed(edited):
            try:
                if edit.path.read_bytes()!=edit.before:edit.path.write_bytes(edit.before)
            except BaseException as error:errors.append(str(error))
        for change in deleted:
            try:
                if change.before is not None:change.source.write_bytes(change.before)
                else:shutil.copy2(change.destination,change.source)
            except BaseException as error:
                restore_failed.add(change.source)
                errors.append(str(error)+'；保留工作副本: '+str(change.destination))
        for change in reversed(written):
            try:
                if change.source==change.destination:change.source.write_bytes(change.before)
                elif change.source not in restore_failed and change.destination.exists():change.destination.unlink()
            except BaseException as error:errors.append(str(error))
        if errors:failure.add_note('材质回滚未完全成功: '+'; '.join(errors))
        raise
    manifest=[{'source_file':str(c.source),'file':str(c.destination),'source_archive':c.source_archive,'archive_path':c.archive_path,
               'retained_source':c.retain_source} for c in plan.files]
    return MaterialResult(plan.salt,manifest,plan.bindings,plan.decisions,plan.archive_names,plan.warnings,plan.normalization_repairs)
