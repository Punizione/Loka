"""Weapon sound-event and owned WAV conversion using bundled profiles."""
from dataclasses import dataclass,field
from functools import lru_cache
from pathlib import Path
import json
import hashlib

from .vpk import safe_archive_path
from .weapon_qc import _tokens,_patch

SOUND_EVENTS=frozenset(('5004','ae_cl_playsound','ae_sv_playsound'))


@dataclass(frozen=True)
class SoundRecord:
    name: str
    owner: str | None
    action: str
    paths: tuple[str,...]


@dataclass(frozen=True)
class SoundReference:
    profiles: dict
    events: dict[str,SoundRecord]
    shared_paths: frozenset[str]


@lru_cache(maxsize=1)
def sound_reference():
    data=json.loads((Path(__file__).parent/'data/weapon_sounds.json').read_text(encoding='utf-8'))
    records={key:SoundRecord(v['name'],v['owner'],v['action'],tuple(v['paths'])) for key,v in data['events'].items()}
    for record in records.values():
        for path in record.paths:safe_archive_path(path)
    return SoundReference(data['profiles'],records,frozenset(data['shared_paths']))


@dataclass(frozen=True)
class QcAudioEdit:
    path: Path
    before: bytes
    after: bytes


@dataclass(frozen=True)
class WaveCopy:
    source: Path
    destination: Path
    archive_path: str
    payload: bytes | None = None
    normalization: dict | None = None
    destination_before: bytes | None = None
    destination_identity: tuple | None = None


@dataclass
class AudioPlan:
    root: Path
    qc_edits: list[QcAudioEdit]=field(default_factory=list)
    wave_copies: list[WaveCopy]=field(default_factory=list)
    remove_sources: list[Path]=field(default_factory=list)
    decisions: list[dict]=field(default_factory=list)
    warnings: list[str]=field(default_factory=list)
    archive_names: dict[str,str]=field(default_factory=dict)
    changed_events: int=0
    source_hashes: dict[Path,str]=field(default_factory=dict)


@dataclass
class AudioResult:
    changed_events: int
    converted_files: list[str]
    decisions: list[dict]
    warnings: list[str]
    archive_names: dict[str,str]
    file_manifest: list[dict]


def _family(profile):return 'pistol' if profile in ('pistol','dual_pistol') else profile


# Verified alternate event namespace. Cross-profile pistol fire is never an alias.
_NATIVE_ALIASES={'pistol':{'fire':('pistol_silver.fire',),'deploy':('pistol_silver.deploy',)}}


def _native_records(reference, source_profile, files):
    """Choose one supplied native event per semantic action from this profile."""
    if source_profile not in ('pistol','dual_pistol'):
        yield from (r for r in reference.events.values() if r.owner==source_profile
                    and any(p.lower() in files for p in r.paths))
        return
    for action,name in reference.profiles[source_profile].items():
        candidates=(name,*_NATIVE_ALIASES.get(source_profile,{}).get(action,()))
        for candidate in candidates:
            record=reference.events.get(candidate.lower())
            if record and any(p.lower() in files for p in record.paths):
                yield record
                break


def _sounds(text):
    tokens=_tokens(text)
    for i,token in enumerate(tokens):
        if token.quoted or token.value.lower()!='event' or i+3>=len(tokens):continue
        if tokens[i+1].value.lower() in SOUND_EVENTS and tokens[i+3].quoted:
            yield tokens[i+3]


def _sound_calls(text):
    """Yield the payload and complete event span, including its optional braces."""
    tokens=_tokens(text)
    for i,token in enumerate(tokens):
        if token.quoted or token.value.lower()!='event' or i+3>=len(tokens):continue
        if tokens[i+1].value.lower() not in SOUND_EVENTS or not tokens[i+3].quoted:continue
        start,end=token.start,tokens[i+3].end
        if i and tokens[i-1].value=='{' and i+4<len(tokens) and tokens[i+4].value=='}':
            start,end=tokens[i-1].start,tokens[i+4].end
        yield tokens[i+3],start,end


def _melee_private_wave(reference,source_profile,targets):
    """Classify verified source-only WAV paths, including author backup subfolders.

    Unknown files are only owned inside a weapon directory whose complete known
    sound inventory belongs to the source. Mixed and shared roots stay protected.
    """
    from .weapon_catalog import MELEE_IDS
    if source_profile not in MELEE_IDS or not targets or source_profile in targets or not set(targets)<=MELEE_IDS:return lambda path:False
    owners={}
    for record in reference.events.values():
        for path in record.paths:owners.setdefault(path.lower(),set()).add(record.owner)
    shared={path.lower() for path in reference.shared_paths}
    target_paths={path.lower() for record in reference.events.values() if record.owner in targets for path in record.paths}
    exclusive={path for path,values in owners.items() if values=={source_profile} and path not in shared}
    candidates={'/'.join(path.split('/')[:3])+'/' for path in exclusive if path.startswith('sound/weapons/') and len(path.split('/'))>3}
    roots={prefix for prefix in candidates if all(values=={source_profile} and path not in shared
        for path,values in owners.items() if path.startswith(prefix)) and not any(path.startswith(prefix) for path in shared)}
    def private(path):
        path=path.lower()
        return (path.startswith('sound/') and path.endswith('.wav') and path not in shared and path not in target_paths
            and (path in exclusive or any(path.startswith(prefix) for prefix in roots)))
    return private


def _counterpart(record,target,reference):
    key=reference.profiles[target].get(record.action)
    return reference.events[key] if key else None


def _engine_trigger(record):
    """Return the verified non-animation trigger owning this sound's WAVs.

    Installed Valve update/pak01 scripts/melee/*.txt SoundData binds all melee
    profiles to melee_miss/melee_hit/melee_hit_world. scripts/weapon_*.txt binds
    firearm fire/fireincendiary to single_shot/double_shot/shoot_incendiary;
    hunting_rifle and sniper_military also bind zoom to special3. The SDK's
    scripting/scripts/vscripts/entitygroups/*_group.nut uses the grenade
    explosion through ambient_generic independently of the weapon model.

    Scope by owner as well as action: names alone do not establish a trigger.
    Deploy, reload and foley stay in the animation allocation domain.
    """
    if record is None:return ''
    from .weapon_catalog import MELEE_IDS,WEAPONS_BY_ID
    if record.owner in MELEE_IDS:
        return {'miss':'melee_miss','impactflesh':'melee_hit','impactworld':'melee_hit_world'}.get(record.action,'')
    if record.owner=='dual_pistol' or record.owner in WEAPONS_BY_ID:
        if record.action in ('fire','fireincendiary'):return 'weapon_'+record.action
        if record.owner in ('hunting_rifle','sniper_military') and record.action=='zoom':return 'weapon_zoom'
        if record.owner=='grenade_launcher' and record.action=='explode':return 'projectile_explosion'
    return ''


def _reason(record,target,reference):
    if record is None:return 'unknown'
    if record.owner is None:return 'outside_firearms'
    if any(p in reference.shared_paths for p in record.paths):return 'shared_source'
    if target is None:return 'missing_target_action'
    if len(record.paths)!=len(target.paths):return 'variant_count_mismatch'
    return ''


def _conversion_reason(record,target,reference,private_wave):
    reason=_reason(record,target,reference)
    # Melee random-variant counts differ between weapons. Both allocation and
    # conversion copy the available prefix and discard excess private variants.
    if reason=='variant_count_mismatch' and record.paths and all(private_wave(p) for p in record.paths):
        return ''
    return reason


def plan_audio_conversion(pack_dir,qc_targets,source_profile,*,original_names=None,
                          event_overrides=None,preserve_events=None,extra_wave_requests=(),
                          remove_wave_sources=(),retain_wave_paths=(),target_metadata=None,
                          ffmpeg_path='',cancelled=None,expected_source_hashes=None):
    root=Path(pack_dir).resolve();reference=sound_reference()
    if not root.is_dir() or root.name!='pack':raise ValueError('音频转换只能处理独立的 pack 待打包目录')
    if source_profile not in reference.profiles:raise ValueError('没有来源声音配置: '+source_profile)
    qc_targets=[(Path(p).resolve(),target) for p,target in qc_targets]
    from .weapon_catalog import MELEE_IDS
    melee_conversion=source_profile in MELEE_IDS and bool(qc_targets) and all(target in MELEE_IDS for _,target in qc_targets)
    private_wave=_melee_private_wave(reference,source_profile,{target for _,target in qc_targets})
    protected=(root,root.parent/'unpack',root.parent/'source')
    for path,target in qc_targets:
        if target not in reference.profiles:raise ValueError('没有目标声音配置: '+target)
        if not path.is_relative_to(root.parent) or any(path.is_relative_to(directory) for directory in protected):
            raise ValueError('QC 必须在当前工作区的独立转换源中')
    files={};names={}
    if target_metadata is not None:
        from .weapon_l4n import _inventory
        inventory=_inventory(Path(pack_dir).absolute()).values()
    else:inventory=root.rglob('*')
    for path in inventory:
        if not path.is_file():continue
        if not path.resolve().is_relative_to(root):raise ValueError('解包资源路径越界')
        rel=safe_archive_path(path.relative_to(root).as_posix());key=rel.lower()
        if key in files:raise ValueError('解包路径大小写冲突: '+rel)
        files[key]=path;names[key]=(original_names or {}).get(key,rel)
    overrides={profile:{name.lower():value for name,value in values.items()} for profile,values in (event_overrides or {}).items()}
    preserved={profile:{name.lower() for name in values} for profile,values in (preserve_events or {}).items()}
    requests=[(safe_archive_path(str(source).replace('\\','/')).lower(),
               safe_archive_path(str(destination).replace('\\','/')).lower()) for source,destination in extra_wave_requests]
    explicit_removals={safe_archive_path(str(p).replace('\\','/')).lower() for p in remove_wave_sources}
    retained={safe_archive_path(str(p).replace('\\','/')).lower() for p in retain_wave_paths}
    composed=event_overrides is not None or preserve_events is not None or bool(requests or explicit_removals or retained)
    allocated={source for source,_ in requests}|explicit_removals
    for profile,values in overrides.items():
        for name,value in values.items():
            if not isinstance(value,tuple) or len(value)!=2 or value[1] not in ('normal_slot','l4n'):
                raise ValueError('音频事件覆盖必须为 (名称, normal_slot/l4n)')
            if not isinstance(value[0],str) or not value[0] or any(c in value[0] for c in '\r\n\0"'):
                raise ValueError('音频覆盖名称无效')
            destination_record=reference.events.get(value[0].lower())
            destination_wave=value[0].replace('\\','/').lower().lstrip(')*#@^><!?')
            if not destination_wave.startswith('sound/'):destination_wave='sound/'+destination_wave
            if ((destination_record and any(private_wave(p) for p in destination_record.paths)) or private_wave(destination_wave)):
                raise ValueError('近战声音覆盖不能引用待移除的来源专属音频: '+value[0])
            record=reference.events.get(name)
            if record:allocated.update(p.lower() for p in record.paths)
    for values in preserved.values():
        for name in values:
            record=reference.events.get(name)
            if record:allocated.update(p.lower() for p in record.paths)
    plan=AudioPlan(root,archive_names=dict(names))
    private_sources={path for path in files if private_wave(path)}
    explicit_removals.update(private_sources)
    if private_sources:
        from .material_text import material_tokens
        for key,path in files.items():
            if not key.startswith(('scripts/','l4n/scripts/')) or not key.endswith(('.txt','.vdf')):continue
            raw=path.read_bytes()
            try:tokens,_=material_tokens(raw)
            except ValueError as error:raise ValueError('无法核对近战专属音频的本地声音脚本: '+str(path)) from error
            plan.source_hashes[path]=hashlib.sha256(raw).hexdigest()
            for index,token in enumerate(tokens):
                if not index or tokens[index-1].value.lower()!='wave' or not token.value.lower().endswith('.wav'):continue
                dependency=token.value.replace('\\','/').lower().lstrip(')*#@^><!?')
                if not dependency.startswith('sound/'):dependency='sound/'+dependency
                if dependency in private_sources:
                    raise ValueError(f'本地声音脚本引用待清理的来源专属音频，无法安全改写别名: {path}: {dependency}')

    def private_record(record):
        return record is not None and record.owner==source_profile and bool(record.paths) and all(private_wave(p) for p in record.paths)

    def conversion_reason(record,target):
        return _conversion_reason(record,target,reference,private_wave)

    def preserve(record,reason,source='',target=''):
        if record and not private_record(record):retained.update(record.paths)
        if reason in ('missing_target_action','variant_count_mismatch') and not private_record(record):
            message=f'{source or record.name} 无可确定的目标音频对应，保留原音频（{reason}）。'
            if message not in plan.warnings:plan.warnings.append(message)

    # Native weapon sounds (including engine-fired sounds outside QC) must follow the target.
    for record in _native_records(reference,source_profile,files):
        if any(p.lower() in allocated for p in record.paths):continue
        for target_id in dict.fromkeys(target for _,target in qc_targets):
            target=_counterpart(record,target_id,reference)
            reason=conversion_reason(record,target)
            if reason:preserve(record,reason);continue
            requests.extend((source,destination) for source,destination in zip(record.paths,target.paths) if source in files)

    for qc,target_id in qc_targets:
        before=qc.read_bytes();text=before.decode('utf-8');changes=[]
        for token,call_start,call_end in _sound_calls(text):
            record=reference.events.get(token.value.lower())
            override=overrides.get(target_id,{}).get(token.value.lower())
            if override is not None:
                name,reason=override
                if token.value!=name:
                    changes.append((token.start,token.end,'"'+name+'"'));plan.changed_events+=1
                plan.decisions.append({'qc':str(qc),'source':token.value,'target':name,'reason':reason})
                continue
            direct=None
            if record is None and token.value.lower().endswith('.wav'):
                direct=token.value.replace('\\','/').lower().lstrip(')*#@^><!?')
                if not direct.startswith('sound/'):direct='sound/'+direct
            if direct and private_wave(direct):
                destinations=[destination for source,destination in requests if source==direct and not private_wave(destination)]
                if destinations:
                    prefix=token.value[:len(token.value)-len(token.value.lstrip(')*#@^><!?'))]
                    original=token.value[len(prefix):].replace('\\','/')
                    name=prefix+(destinations[0] if original.lower().startswith('sound/') else destinations[0].removeprefix('sound/'))
                    changes.append((token.start,token.end,'"'+name+'"'));plan.changed_events+=1
                    plan.decisions.append({'qc':str(qc),'source':token.value,'target':name,'reason':'converted'})
                else:
                    changes.append((call_start,call_end,''));plan.changed_events+=1
                    plan.decisions.append({'qc':str(qc),'source':token.value,'target':'','reason':'dropped_source_audio'})
                continue
            if token.value.lower() in preserved.get(target_id,set()) and private_record(record):
                changes.append((call_start,call_end,''));plan.changed_events+=1
                plan.decisions.append({'qc':str(qc),'source':token.value,'target':'','reason':'dropped_source_audio'})
                continue
            if token.value.lower() in preserved.get(target_id,set()):
                preserve(record,'preserved',token.value)
                plan.decisions.append({'qc':str(qc),'source':token.value,'target':token.value,'reason':'preserved'})
                continue
            target=_counterpart(record,target_id,reference) if record and record.owner else None
            reason=conversion_reason(record,target)
            if private_record(record) and target is None:
                changes.append((call_start,call_end,''));plan.changed_events+=1
                plan.decisions.append({'qc':str(qc),'source':token.value,'target':'','reason':'dropped_source_audio'})
                continue
            if not reason and _family(record.owner)!=_family(source_profile) and (melee_conversion or not any(p in files for p in record.paths)):
                reason='unowned_foreign_sound'
            if not reason and token.value.lower()==target.name.lower():reason='already_target'
            if reason:
                preserve(record,reason,token.value)
                if record is None and token.value.lower().endswith('.wav'):
                    direct=token.value.replace('\\','/').lower().lstrip(')*#@^><!?' if composed else '*#@^')
                    if not direct.startswith('sound/'):direct='sound/'+direct
                    if direct in files:retained.add(direct)
                plan.decisions.append({'qc':str(qc),'source':token.value,'target':token.value,'reason':reason})
                continue
            changes.append((token.start,token.end,'"'+target.name+'"'))
            plan.changed_events+=1
            plan.decisions.append({'qc':str(qc),'source':token.value,'target':target.name,'reason':'converted'})
            requests.extend((source,destination) for source,destination in zip(record.paths,target.paths) if source in files)
        after=_patch(text,changes).encode('utf-8') if changes else before
        if after!=before:plan.qc_edits.append(QcAudioEdit(qc,before,after))

    chosen={};source_targets={};payloads={}
    def payload(path):
        if path not in payloads:payloads[path]=path.read_bytes()
        return payloads[path]
    for path,digest in (expected_source_hashes or {}).items():
        path=Path(path)
        if not path.resolve().is_relative_to(root) or hashlib.sha256(payload(path)).hexdigest()!=digest:
            raise ValueError('Allocator source WAV hash changed before normalization')
        plan.source_hashes[path]=digest
    for source,destination in dict.fromkeys(requests):
        destination=safe_archive_path(destination).lower()
        source=safe_archive_path(source).lower()
        if source not in files or not source.startswith('sound/') or not source.endswith('.wav') or not destination.startswith('sound/') or not destination.endswith('.wav'):
            raise ValueError('音频请求缺少实际 WAV 或路径无效: '+source+' -> '+destination)
        path=files[source];target=(root/destination)
        data=payload(path)
        if not target.resolve().is_relative_to(root):raise ValueError('音频目标路径越界')
        for parent in target.parents:
            if parent==root:break
            if parent.is_file():raise ValueError('音频目标目录是文件: '+str(parent))
        if destination in files and payload(files[destination])!=data:
            raise ValueError('目标音频已存在且内容不同: '+destination)
        if destination in chosen and payload(chosen[destination])!=data:
            raise ValueError('多个源音频汇入同一目标且内容不同: '+destination)
        if target_metadata is not None and destination in chosen and chosen[destination]!=path:
            raise ValueError('不同物理来源不能占用同一目标 WAV: '+destination)
        chosen.setdefault(destination,path);source_targets.setdefault(source,set()).add(destination)
        plan.source_hashes.setdefault(path,hashlib.sha256(data).hexdigest())
        plan.archive_names[destination]=destination
    for destination,path in chosen.items():
        physical=files.get(destination,root/destination)
        if target_metadata is None:
            plan.wave_copies.append(WaveCopy(path,physical,destination))
        else:
            from .wav_normalize import normalize_wave
            from .weapon_audio_metadata import target_wave_metadata
            from dataclasses import asdict
            target=target_wave_metadata(destination,records=target_metadata)
            try:result=normalize_wave(path,target,ffmpeg_path=ffmpeg_path,expected_source_sha256=plan.source_hashes[path],cancelled=cancelled)
            except ValueError as error:raise ValueError(f'{path}: {error}') from error
            details={k:v for k,v in asdict(result).items() if k!='payload'}
            before=payload(files[destination]) if destination in files else None
            stat=physical.stat() if physical.exists() else None
            plan.wave_copies.append(WaveCopy(path,physical,destination,result.payload,details,before,(stat.st_dev,stat.st_ino) if stat else None))
    retained.difference_update(private_sources)
    for source in set(source_targets)|explicit_removals:
        if source not in files or not source.startswith('sound/') or not source.endswith('.wav'):
            raise ValueError('待删除音频不是实际 WAV: '+source)
        if source not in retained and source not in chosen:
            plan.source_hashes.setdefault(files[source],hashlib.sha256(payload(files[source])).hexdigest())
            plan.remove_sources.append(files[source]);plan.archive_names.pop(source,None)
    for source in sorted(private_sources):
        if files[source] in plan.remove_sources:
            plan.decisions.append({'source':source,'target':'','reason':'dropped_source_audio'})
    return plan


def apply_audio_conversion(plan,*,cancelled=None):
    def check():
        if cancelled and cancelled():raise InterruptedError('音频转换已取消')
    check()
    affected=set(plan.remove_sources)
    affected.update(plan.source_hashes)
    affected.update(c.source for c in plan.wave_copies)
    affected.update(c.destination for c in plan.wave_copies)
    for path in affected:
        if not path.resolve().is_relative_to(plan.root):raise ValueError('音频修改路径越界')
    snapshot={path:path.read_bytes() if path.is_file() else None for path in affected}
    for path,digest in plan.source_hashes.items():
        data=snapshot.get(path)
        if data is None or hashlib.sha256(data).hexdigest()!=digest:
            raise ValueError('规划后源音频已修改: '+str(path))
    for edit in plan.qc_edits:
        if edit.path.read_bytes()!=edit.before:raise ValueError('规划后 QC 已修改: '+str(edit.path))
    for copy in plan.wave_copies:
        if copy.payload is None:continue
        if snapshot[copy.destination]!=copy.destination_before:raise ValueError('规划后目标音频发生冲突')
        if copy.destination_identity is not None:
            stat=copy.destination.stat()
            if (stat.st_dev,stat.st_ino)!=copy.destination_identity:raise ValueError('规划后目标音频被替换')
    written=[];edited=[];deleted=[];owned_files={}
    try:
        for copy in plan.wave_copies:
            check();data=snapshot[copy.source] if copy.payload is None else copy.payload
            if copy.destination.is_file():
                if copy.payload is not None:
                    stat=copy.destination.stat()
                    if (stat.st_dev,stat.st_ino)!=copy.destination_identity:
                        raise ValueError('规划后目标音频被替换，保留其他操作文件')
                    if copy.destination.read_bytes()!=copy.destination_before:raise ValueError('规划后目标音频发生冲突')
                    if copy.destination.read_bytes()!=data:
                        owned_files[copy.destination]=(stat.st_dev,stat.st_ino)
                        written.append(copy.destination);copy.destination.write_bytes(data)
                    continue
                if copy.destination.read_bytes()!=data:raise ValueError('规划后目标音频发生冲突')
                continue
            copy.destination.parent.mkdir(parents=True,exist_ok=True)
            with copy.destination.open('xb') as handle:
                import os
                stat=os.fstat(handle.fileno());owned_files[copy.destination]=(stat.st_dev,stat.st_ino)
                written.append(copy.destination);handle.write(data)
        for edit in plan.qc_edits:
            check();edited.append(edit);edit.path.write_bytes(edit.after)
        for copy in plan.wave_copies:
            if copy.payload is not None and hashlib.sha256(copy.destination.read_bytes()).hexdigest()!=copy.normalization['output_sha256']:
                raise ValueError('规范化 WAV 输出 hash 校验失败')
        for source in plan.remove_sources:
            check();source.unlink();deleted.append(source)
        if any(d['reason']=='dropped_source_audio' for d in plan.decisions):
            import errno
            sound_root=plan.root/'sound'
            parents={parent for source in deleted for parent in source.parents
                     if parent!=sound_root and parent.is_relative_to(sound_root)}
            for directory in sorted(parents,key=lambda p:len(p.parts),reverse=True):
                check()
                try:directory.rmdir()
                except FileNotFoundError:pass
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY,errno.EEXIST):raise
    except BaseException as failure:
        rollback_errors=[]
        for edit in reversed(edited):
            try:
                if edit.path.read_bytes()!=edit.before:edit.path.write_bytes(edit.before)
            except BaseException as error:rollback_errors.append(str(error))
        for path in set(written+deleted):
            data=snapshot[path]
            try:
                if path in owned_files and path.exists():
                    stat=path.stat()
                    if (stat.st_dev,stat.st_ino)!=owned_files[path]:raise ValueError('音频目标被其他操作替换，保留: '+str(path))
                if path in deleted and path.exists():raise ValueError('删除后的音频路径被其他操作占用，保留: '+str(path))
                if data is not None:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    if not path.is_file() or path.read_bytes()!=data:path.write_bytes(data)
                elif path.exists():path.unlink()
            except BaseException as error:rollback_errors.append(str(error))
        if rollback_errors:failure.add_note('音频回滚未完全成功: '+'; '.join(rollback_errors))
        raise
    manifest=[dict({'source_file':str(c.source),'file':str(c.destination),'archive_path':c.archive_path},**(c.normalization or {})) for c in plan.wave_copies]
    return AudioResult(plan.changed_events,[c.archive_path for c in plan.wave_copies],plan.decisions,
                       plan.warnings,plan.archive_names,manifest)
