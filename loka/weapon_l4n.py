"""Allocate provided V-QC sounds and prepare a private, verified L4N audio pack."""
from dataclasses import dataclass,field,asdict
from collections import Counter
from fractions import Fraction
from pathlib import Path
import hashlib, os, random, re, secrets, shutil, time

from . import weapon_audio as audio
from .material_text import material_pairs
from .soundcache import build_sound_cache, read_sound_cache
from .vpk import safe_archive_path
from .wav_normalize import analyze_wave
from .weapon_audio_metadata import target_wave_metadata


@dataclass
class L4NPlan:
    root: Path
    identifier: str
    event_overrides: dict[str, dict[str, tuple[str, str]]]
    preserve_events: dict[str, set[str]]
    wave_requests: list[tuple[str, str]]
    remove_wave_sources: list[str]
    retain_wave_paths: set[str]
    extra_sounds: list[dict]
    source_hashes: dict[Path, str]
    statistics: dict
    decisions: list[dict]
    warnings: list[str]
    source_wave_analysis: dict=field(default_factory=dict)


@dataclass
class L4NResult:
    pack_dir: str | None
    identifier: str
    event_names: list[str]
    wave_files: list[str]
    cache_path: str | None
    cache_sha256: str
    statistics: dict
    decisions: list[dict]
    file_manifest: list[dict]
    warnings: list[str]


def _hash(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _real(path):
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, 'is_junction') and component.is_junction()):
            raise ValueError('L4N 不支持别名路径: ' + str(component))


def _inventory(root):
    _real(root)
    if root.name != 'pack' or not root.is_dir():
        raise ValueError('L4N 只能读取独立 pack')
    files = {}
    def traversal_error(error):
        raise OSError(error.errno, 'L4N 无法读取 pack 目录: ' + str(error), error.filename or str(root)) from error
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=traversal_error):
        for name in directories + filenames:
            _real(Path(directory)/name)
        for name in filenames:
            path = Path(directory)/name
            if not path.is_file():
                continue
            key = safe_archive_path(path.relative_to(root).as_posix()).lower()
            if key in files or not path.resolve().is_relative_to(root):
                raise ValueError('L4N pack 路径冲突或越界: ' + key)
            files[key] = path
    return files


def _wave_key(value):
    value = value.replace('\\', '/').lstrip(')*#@^><!?')
    if not value.lower().startswith('sound/'):
        value = 'sound/' + value
    return safe_archive_path(value).lower()


def _identifier(root, files, identifier):
    def collision(value):
        prefix = 'sound/' + value
        return (root / prefix).exists() or any(k == prefix or k.startswith(prefix + '/') for k in files)
    if identifier is not None:
        if not isinstance(identifier, str) or not re.fullmatch('[a-z0-9]{8}', identifier):
            raise ValueError('L4N 标识必须为 8 位小写字母数字')
        if collision(identifier):
            raise FileExistsError('L4N 标识目录已存在: ' + identifier)
        return identifier
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
    for _ in range(128):
        number = (time.time_ns() + secrets.randbits(64)) % (36 ** 8)
        chars = []
        for _ in range(8):
            number, digit = divmod(number, 36)
            chars.append(alphabet[digit])
        value = ''.join(reversed(chars))
        if not collision(value):
            return value
    raise FileExistsError('无法分配新的 L4N 标识')


def _matching(sources,slots,allowed,cost,cancelled=None):
    """Successive shortest augmenting paths: max cardinality then exact tuple cost."""
    count=len(sources)+len(slots)+2;end=count-1;graph=[[] for _ in range(count)];links=[]
    zero=(Fraction(0),)*5
    def edge(a,b,value):
        forward=[b,len(graph[b]),1,value];reverse=[a,len(graph[a]),0,tuple(-v for v in value)]
        graph[a].append(forward);graph[b].append(reverse);return forward
    for i,source in enumerate(sources):
        edge(0,1+i,zero)
        for j,slot in enumerate(slots):
            if allowed(source,slot):links.append((source,slot,edge(1+i,1+len(sources)+j,cost(source,slot))))
    for j in range(len(slots)):edge(1+len(sources)+j,end,zero)
    while True:
        if cancelled and cancelled():raise InterruptedError('Audio assignment cancelled')
        distance=[None]*count;distance[0]=zero;previous=[None]*count
        for _ in range(count-1):
            changed=False
            for node,edges in enumerate(graph):
                if distance[node] is None:continue
                for index,arc in enumerate(edges):
                    if not arc[2]:continue
                    candidate=tuple(a+b for a,b in zip(distance[node],arc[3]))
                    if distance[arc[0]] is None or candidate<distance[arc[0]]:
                        distance[arc[0]]=candidate;previous[arc[0]]=(node,index);changed=True
            if not changed:break
        if distance[end] is None:break
        node=end
        while node:
            parent,index=previous[node];arc=graph[parent][index];arc[2]-=1;graph[node][arc[1]][2]+=1;node=parent
    return {source['path']:slot for source,slot,arc in links if not arc[2]}


def _allocate_singletons(sources,slots,claimed,metadata,policy,cancelled=None):
    sources=sorted(sources,key=lambda s:s['path']);slots=sorted(slots)
    rates={p:target_wave_metadata(p,records=metadata).duration for p in slots}
    allowed=lambda source,slot:slot not in claimed or claimed[slot]==source['path']
    rank={s['path']:i for i,s in enumerate(sources)}
    priority=lambda s,p:(Fraction(0),Fraction(0),-s['uses'],Fraction(0),rank[s['path']])
    cohort_map=_matching(sources,slots,allowed,priority,cancelled);cardinality=len(cohort_map)
    cohort=[s for s in sources if s['path'] in cohort_map]
    oversubscribed=len(sources)>cardinality
    def costs(s,p):
        cut=max(Fraction(0),s['duration']-rates[p]);pad=max(Fraction(0),rates[p]-s['duration'])
        return cut,pad,-s['uses'],cut*s['uses'],rank[s['path']]
    if policy in ('balanced','complete'):
        chosen=_matching(cohort if policy=='balanced' else sources,slots,allowed,costs,cancelled)
    else:
        order=sorted(cohort,key=(lambda s:(-s['uses'],s['path'])) if oversubscribed else (lambda s:(-s['duration'],-s['uses'],s['path'])))
        chosen={};remaining=list(slots)
        for index,source in enumerate(order):
            options=[p for p in remaining if allowed(source,p)]
            options.sort(key=lambda p:(0,rates[p],p) if rates[p]>=source['duration'] else (1,-rates[p],p))
            for path in options:
                later=order[index+1:];free=[p for p in remaining if p!=path]
                if len(_matching(later,free,allowed,priority,cancelled))==len(later):
                    chosen[source['path']]=path;remaining.remove(path);break
    by_path={s['path']:s for s in sources}
    cut=sum((max(Fraction(0),by_path[s]['duration']-rates[p]) for s,p in chosen.items()),Fraction(0))
    pad=sum((max(Fraction(0),rates[p]-by_path[s]['duration']) for s,p in chosen.items()),Fraction(0))
    return chosen,{'audio_logic':policy,'optimization_domain':'residual_singletons_after_atomic_reservations','eligible_singletons':len(sources),
        'feasible_cardinality':cardinality,'oversubscribed':oversubscribed,'cohort':sorted(chosen if policy=='complete' else cohort_map),
        'truncate_seconds':{'numerator':cut.numerator,'denominator':cut.denominator},'padding_seconds':{'numerator':pad.numerator,'denominator':pad.denominator}}


def plan_l4n_conversion(pack_dir, source_qcs, target_profiles, source_profile, *, identifier=None, rng=None,
                         audio_logic='frequency',target_metadata=None,ffmpeg_path='',cancelled=None):
    if audio_logic not in ('frequency','balanced','complete'):raise ValueError('音频处理逻辑无效: '+str(audio_logic))
    if target_metadata is None and audio_logic!='frequency':raise ValueError('音频分配策略需要官方 WAV metadata')
    if cancelled and cancelled():raise InterruptedError('音频分配已取消')
    root = Path(pack_dir).absolute()
    files = _inventory(root)
    reference = audio.sound_reference()
    targets = tuple(dict.fromkeys(target_profiles))
    if source_profile not in reference.profiles or not targets or any(t not in reference.profiles for t in targets):
        raise ValueError('L4N 来源/目标声音配置无效')
    private_wave=audio._melee_private_wave(reference,source_profile,set(targets))
    rng = rng if rng is not None else random.SystemRandom()
    identifier = _identifier(root, files, identifier)
    waves = {k for k in files if k.startswith('sound/') and k.endswith('.wav')}
    shared = {p.lower() for p in reference.shared_paths}
    calls = []
    retain, warnings, decisions = set(), [], []
    for qc in source_qcs:
        path = Path(qc).absolute()
        _real(path)
        for token in audio._sounds(path.read_text(encoding='utf-8-sig')):
            calls.append(token.value.lower())
            if token.value.lower().endswith('.wav'):
                try:
                    direct = _wave_key(token.value)
                    if direct in waves:
                        retain.add(direct)
                except ValueError:
                    pass
    # Existing author script dependencies are not removed by allocation.
    for key, path in files.items():
        if not key.endswith(('.txt', '.vdf')) or not key.startswith(('scripts/', 'l4n/scripts/')):
            continue
        try:
            pairs, _ = material_pairs(path.read_bytes())
            for token, value, _ in pairs:
                if token.value.lower() == 'wave':
                    dependency = _wave_key(value.value)
                    if dependency in waves:
                        retain.add(dependency)
        except (ValueError, UnicodeError):
            retain.update(waves)
            warnings.append('无法安全读取声音脚本，保留原 WAV 依赖: ' + key)

    overrides = {t: {} for t in targets}
    preserve = {t: set() for t in targets}
    groups = {}
    original_events = list(dict.fromkeys(calls))
    call_counts=Counter(calls)
    for name in original_events:
        record = reference.events.get(name)
        provided = tuple(p.lower() for p in record.paths if p.lower() in waves) if record else ()
        trigger=audio._engine_trigger(record)
        if (trigger and audio._family(record.owner)==audio._family(source_profile)
            and not any(p.lower() in shared for p in record.paths)):
            # QC may explicitly call an engine role, but never changes its
            # meaning or moves it into the animation/L4N allocation domain.
            # A missing custom WAV still resolves to the target's stock sound.
            for t in targets:
                counterpart=audio._counterpart(record,t,reference)
                reason=audio._conversion_reason(record,counterpart,reference,private_wave)
                if reason:
                    preserve[t].add(name);retain.update(provided)
                else:
                    overrides[t][name]=(counterpart.name,'normal_slot')
                decisions.append({'target_profile':t,'source_event':name,'source_paths':list(provided),
                    'target_event':counterpart.name if counterpart and not reason else name,
                    'reason':'engine_counterpart' if not reason else reason,'trigger':trigger})
            continue
        reason = ('unknown' if record is None else
                  'foreign' if audio._family(record.owner) != audio._family(source_profile) else
                  'shared' if record.owner is None or any(p.lower() in shared for p in record.paths) else
                  'stock_only' if not provided else '')
        if reason:
            for t in targets:
                preserve[t].add(name)
            if record:
                retain.update(provided)
            decisions.append({'source_event': name, 'reason': reason, 'source_paths': list(provided)})
            continue
        paths = tuple(dict.fromkeys(provided))
        key = frozenset(paths)
        group = groups.setdefault(key, {'paths': paths, 'events': [], 'records': [], 'protected': False})
        group['events'].append(name)
        group['records'].append(record)
    group_list = list(groups.values())
    for group in group_list:group['original_use_count']=sum(call_counts[name] for name in group['events'])
    for i, group in enumerate(group_list):
        for other in group_list[i+1:]:
            if set(group['paths']) & set(other['paths']) and (len(group['paths']) > 1 or len(other['paths']) > 1):
                group['protected'] = other['protected'] = True
    custom_paths = set().union(*(set(g['paths']) for g in group_list)) if group_list else set()
    protected_paths = set()
    for group in group_list:
        if group['protected']:
            protected_paths.update(group['paths']); retain.update(group['paths'])
            for t in targets:
                preserve[t].update(group['events'])
            decisions.append({'source_events': list(group['events']), 'source_paths': list(group['paths']), 'reason': 'overlapping_variants'})

    target_groups, target_slots, engine_slots = {}, {}, {}
    for t in targets:
        records = [reference.events[name.lower()] for name in dict.fromkeys(reference.profiles[t].values())]
        target_groups[t] = [(r, tuple(dict.fromkeys(p.lower() for p in r.paths if p.lower() not in shared))) for r in records]
        slots = {}
        for record, paths in target_groups[t]:
            for path in paths:
                slots.setdefault(path, record)
        target_slots[t] = slots
        # Reserve physical WAV paths across aliases, whether the MOD supplies
        # none, some or all variants of the corresponding source role.
        engine_slots[t]={p for record,paths in target_groups[t] if audio._engine_trigger(record) for p in paths}

    # Physical destination ownership is shared across independently planned profiles.
    all_destinations = set().union(*(set(s) for s in target_slots.values()))
    existing = all_destinations & set(files)
    claimed = {p: None for p in existing}
    native_destinations = set()
    discarded = set()
    requests = []
    source_hashes = {}
    source_wave_analysis = {}
    variant_pairs = {}
    def remember(source):
        source_hashes.setdefault(files[source], _hash(files[source]))
    for record in audio._native_records(reference,source_profile,waves):
        supplied = [p.lower() for p in record.paths if p.lower() in waves]
        if not supplied or (frozenset(supplied) in groups and not audio._engine_trigger(record)):
            continue
        for t in targets:
            target = audio._counterpart(record, t, reference)
            if audio._conversion_reason(record, target, reference,private_wave):
                continue
            for source, destination in audio._variant_pairs(record,target,waves,rng=rng,cache=variant_pairs):
                source, destination = source.lower(), destination.lower()
                if source not in waves:
                    continue
                if destination in files and _hash(files[destination]) != _hash(files[source]):
                    raise ValueError('目标原生音频已存在且内容不同: ' + destination)
                owner = claimed.get(destination)
                if destination in claimed and owner not in (None, source):
                    raise ValueError('原生音频目标被不同来源占用: ' + destination)
                claimed[destination] = source
                native_destinations.add(destination)
                requests.append((source, destination)); remember(source)
                decisions.append({'target_profile': t, 'source_event': record.name, 'source_path': source,
                    'target_path': destination, 'reason': 'native',
                    'random_fill':destination in tuple(p.lower() for p in target.paths[len(record.paths):])})
            for source in record.paths[len(target.paths):]:
                source=source.lower()
                if source in waves and private_wave(source):
                    discarded.add(source);remember(source)
                    decisions.append({'target_profile':t,'source_event':record.name,'source_path':source,
                                      'reason':'dropped_source_audio'})

    extras = {}
    moved = set()
    statistics = {'original_event_calls': len(calls), 'unique_event_count': len(original_events),
                  'custom_event_count': sum(len(g['events']) for g in group_list),
                  'custom_wav_count': len(custom_paths), 'protected_wav_count': 0,
                  'movable_wav_count': 0, 'extra_wav_count': 0, 'targets': {}}
    for t in targets:
        reserved = set(target_slots[t]) & (existing | native_destinations | engine_slots[t])
        assigned = set()
        choices = {}
        def compatible(group, paths):
            free = [p for p in paths if p not in reserved and p not in assigned]
            if len(group['paths']) == 1:
                source = group['paths'][0]
                return [p for p in free if p not in claimed or claimed[p] == source]
            # Keep the group intact while skipping paths owned by a different WAV.
            # Reuse its prior physical claims before choosing unclaimed group paths.
            selected = []
            for source in group['paths']:
                matching = [p for p in free if claimed.get(p) == source]
                available = matching or [p for p in free if p not in claimed]
                if not available:
                    return []
                path = available[0]; selected.append(path); free.remove(path)
            return selected
        def assign(group, record, paths, *, source_paths=None):
            sources=group['paths'] if source_paths is None else source_paths
            chosen = paths[:len(sources)]
            for source, destination in zip(sources, chosen):
                claimed[destination] = source
                requests.append((source,destination)); remember(source); moved.add(source)
                assigned.add(destination)
            for name in group['events']:
                overrides[t][name] = (record.name, 'normal_slot')
            choices[id(group)] = record.name
            decisions.append({'target_profile': t, 'source_events': list(group['events']), 'source_paths': list(group['paths']),
                              'target_event': record.name, 'target_paths': list(chosen), 'reason': 'normal_slot',
                              'original_use_count':group['original_use_count'],'audio_logic':audio_logic})
            used=set(sources[:len(chosen)])
            dropped=tuple(source for source in group['paths'] if source not in used)
            if dropped:
                decisions[-1]['discarded_source_paths']=list(dropped)
                for source in dropped:
                    discarded.add(source);remember(source)
        # Reserve all logical counterparts before any random fallback.
        for group in group_list:
            if group['protected']:
                continue
            for record in group['records']:
                counterpart = audio._counterpart(record, t, reference)
                if counterpart is None:
                    continue
                paths = tuple(dict.fromkeys(p.lower() for p in counterpart.paths if p.lower() not in shared))
                # Only source-private melee counterparts may lose excess random
                # variants. Firearm atomic groups keep their established rules.
                if (paths and len(record.paths)>len(counterpart.paths) and all(private_wave(p) for p in group['paths'])
                    and not audio._conversion_reason(record,counterpart,reference,private_wave)):
                    # Keep original variant positions: a missing source variant
                    # leaves its matching target on the native game asset.
                    pairs=[(source.lower(),destination.lower()) for source,destination in zip(record.paths,counterpart.paths)
                        if source.lower() in group['paths'] and destination.lower() in paths]
                    if all(destination not in reserved and destination not in assigned
                           and (destination not in claimed or claimed[destination]==source) for source,destination in pairs):
                        assign(group,counterpart,[destination for _,destination in pairs],
                               source_paths=tuple(source for source,_ in pairs))
                        break
                if target_metadata is not None and len(group['paths'])==1:continue
                if len(group['paths']) > 1 and len(paths) <= 1:
                    continue
                free = compatible(group, paths)
                if len(free) >= len(group['paths']):
                    assign(group, counterpart, free)
                    break
        # Atomic groups retain their existing domain before singleton optimization.
        if target_metadata is not None:
            for group in group_list:
                if group['protected'] or id(group) in choices or len(group['paths'])==1:continue
                available=[(record,compatible(group,paths)) for record,paths in target_groups[t] if len(paths)>1]
                available=[(record,paths) for record,paths in available if len(paths)>=len(group['paths'])]
                if available:
                    record,paths=rng.choice(available);assign(group,record,paths)
                else:
                    preserve[t].update(group['events']);retain.update(group['paths']);protected_paths.update(group['paths']);choices[id(group)]='protected_variants'
                    decisions.append({'target_profile':t,'source_events':list(group['events']),'source_paths':list(group['paths']),'reason':'protected_variants'})
            candidates=[]
            for group in group_list:
                if group['protected'] or id(group) in choices or len(group['paths'])!=1:continue
                source=group['paths'][0]
                if cancelled and cancelled():raise InterruptedError('音频分配已取消')
                digest=_hash(files[source]);source_hashes.setdefault(files[source],digest)
                try:analysis=analyze_wave(files[source],ffmpeg_path=ffmpeg_path,cancelled=cancelled)
                except ValueError as error:raise ValueError(f'{files[source]}: {error}') from error
                meta=analysis.metadata
                if analysis.source_sha256!=digest:raise ValueError('Source WAV changed while reading allocation metadata: '+str(files[source]))
                source_wave_analysis[source]={'source_path':str(files[source]),'source_sha256':analysis.source_sha256,
                    'metadata':asdict(meta),'container_repair':asdict(analysis.container_repair) if analysis.container_repair else None}
                if _hash(files[source])!=digest:raise ValueError('Source WAV changed while reading allocation metadata: '+str(files[source]))
                candidates.append({'path':source,'uses':group['original_use_count'],'duration':meta.duration})
            free=[p for p in target_slots[t] if p not in reserved and p not in assigned]
            allocation,policy_statistics=_allocate_singletons(candidates,free,claimed,target_metadata,audio_logic,cancelled)
            source_facts={c['path']:c for c in candidates}
        else:allocation={};policy_statistics={}
        extra_for_target = set()
        for group in group_list:
            if group['protected'] or id(group) in choices:
                continue
            if len(group['paths']) > 1:
                available = [(record, compatible(group, paths)) for record, paths in target_groups[t] if len(paths) > 1]
                available = [(record, paths) for record, paths in available if len(paths) >= len(group['paths'])]
                if available:
                    record, paths = rng.choice(available)
                    assign(group, record, paths)
                else:
                    preserve[t].update(group['events']); retain.update(group['paths']); protected_paths.update(group['paths'])
                    decisions.append({'target_profile': t, 'source_events': list(group['events']), 'source_paths': list(group['paths']), 'reason': 'protected_variants'})
                continue
            if target_metadata is not None:
                source=group['paths'][0];path=allocation.get(source)
                available=[(path,target_slots[t][path])] if path is not None else []
            else:available = [(path, record) for path, record in target_slots[t].items() if compatible(group,(path,))]
            if available:
                path, record = rng.choice(available)
                assign(group, record, [path])
                if target_metadata is not None:
                    source=group['paths'][0];duration=source_facts[source]['duration'];target_duration=target_wave_metadata(path,records=target_metadata).duration
                    decisions[-1].update(source_duration={'numerator':duration.numerator,'denominator':duration.denominator},
                        target_duration={'numerator':target_duration.numerator,'denominator':target_duration.denominator},
                        allocation_rationale=('original_frequency_then_fit' if audio_logic=='frequency' and policy_statistics['oversubscribed'] else
                            'longest_source_then_fit' if audio_logic=='frequency' else 'global_minimum_truncation_then_padding'),
                        target_event_random_variants=len(record.paths)>1,target_event_paths=list(record.paths))
            else:
                source = group['paths'][0]
                extras.setdefault(source, {'source': files[source], 'source_path': source,
                    'archive_path': 'sound/' + identifier + '/' + source.removeprefix('sound/'),
                    'source_events': list(group['events']), 'sha256': _hash(files[source])})
                extra_for_target.add(source); moved.add(source); remember(source)
                choices[id(group)] = source
                decisions.append({'target_profile': t, 'source_events': list(group['events']), 'source_paths': [source], 'reason': 'l4n'})
        statistics['targets'][t] = {'table_slots': len(target_slots[t]), 'reserved_slots': len(reserved),
            'reserved_existing_slots': len(set(target_slots[t]) & existing),
            'reserved_native_slots': len(set(target_slots[t]) & native_destinations),
            'reserved_engine_slots': len(engine_slots[t]),
            'available_slots': len(target_slots[t]) - len(reserved), 'assigned_slots': len(assigned),
            'free_slots': len(set(target_slots[t]) - reserved - assigned), 'extra_wavs': len(extra_for_target)}
        statistics['targets'][t].update(policy_statistics)
        # Names are assigned after the per-profile allocation so shared extras are deduplicated.
        for group in group_list:
            if len(group['paths']) == 1 and group['paths'][0] in extra_for_target:
                for name in group['events']:
                    overrides[t][name] = (group['paths'][0], 'l4n')
    extra_sounds = []
    for number, (source, item) in enumerate(extras.items(), 1):
        item['event_name'] = f'Loka.{identifier}.{number:03d}'
        extra_sounds.append(item)
        for t in targets:
            for name, (new_name, reason) in overrides[t].items():
                if reason == 'l4n' and new_name == source:
                    overrides[t][name] = (item['event_name'], reason)
        for decision in decisions:
            if decision['reason'] == 'l4n' and decision['source_paths'] == [source]:
                decision['target_event'] = item['event_name']
                decision['archive_path'] = item['archive_path']
    # Native records with no V call retain their established conversion; allocation sources
    # are explicit requests/removals, never added through native aliases a second time.
    requests = list(dict.fromkeys(requests))
    statistics.update(protected_wav_count=len(protected_paths), movable_wav_count=len(custom_paths - protected_paths), extra_wav_count=len(extra_sounds))
    return L4NPlan(root, identifier, overrides, preserve, requests, sorted((moved - retain)|discarded), retain,
                   extra_sounds, source_hashes, statistics, decisions, warnings,source_wave_analysis)


def _kv(value):
    if '\0' in value:
        raise ValueError('KeyValues 字符串含 NUL')
    return '"' + value.replace('\\','\\\\').replace('"','\\"').replace('\r','\\r').replace('\n','\\n').replace('\t','\\t') + '"'


def build_l4n_pack(plan, pack_dir, *, title, cancelled=None):
    def check():
        if cancelled and cancelled():
            raise InterruptedError('L4N 音频准备已取消')
    check()
    destination = Path(pack_dir).absolute()
    _real(destination)
    expected = plan.root.parent/'l4n'/'pack'
    if destination != expected or destination.is_relative_to(plan.root):
        raise ValueError('L4N pack 必须为当前工作区独立 l4n/pack')
    for source, digest in plan.source_hashes.items():
        check(); _real(source)
        if not source.is_relative_to(plan.root) or _hash(source) != digest:
            raise ValueError('规划后 L4N 源音频已修改: ' + str(source))
    if not plan.extra_sounds:
        return L4NResult(None, plan.identifier, [], [], None, '', plan.statistics, plan.decisions, [], plan.warnings)
    if destination.exists():
        raise FileExistsError('L4N pack 已存在，不能复用: ' + str(destination))
    owned_files, owned_dirs = {}, {}
    def mkdir(path):
        missing = []
        while not path.exists():
            missing.append(path); path = path.parent
        for child in reversed(missing):
            child.mkdir()
            stat = child.stat(); owned_dirs[child] = (stat.st_dev, stat.st_ino)
    def opened(path):
        _real(path)
        mkdir(path.parent)
        handle = path.open('xb')
        stat = os.fstat(handle.fileno()); owned_files[path] = (stat.st_dev, stat.st_ino)
        return handle
    manifest = []
    try:
        mkdir(destination)
        for item in plan.extra_sounds:
            check()
            source = item['source']; target = destination/safe_archive_path(item['archive_path'])
            if not target.is_relative_to(destination) or item['sha256'] != plan.source_hashes.get(source):
                raise ValueError('L4N WAV 清单/规划 hash 无效')
            class Reader:
                def read(self, amount):
                    check(); return handle.read(amount)
            with source.open('rb') as handle, opened(target) as output:
                shutil.copyfileobj(Reader(), output, length=256*1024)
            check()
            if _hash(target) != item['sha256'] or _hash(source) != item['sha256']:
                raise ValueError('L4N WAV 复制后校验失败: ' + str(source))
            manifest.append({'source_file': str(source), 'source_archive': item['source_path'], 'file': str(target),
                             'archive_path': item['archive_path'], 'sha256': item['sha256'], 'bytes': target.stat().st_size})
        script_path = 'l4n/scripts/sound/game_sounds_' + plan.identifier + '.txt'
        blocks = []
        for item in plan.extra_sounds:
            wave = ')' + item['archive_path'].removeprefix('sound/')
            blocks.append(_kv(item['event_name']) + '\n{\n    "channel" "CHAN_WEAPON"\n    "soundlevel" "SNDLVL_NORM"\n    "volume" "1"\n    "wave" ' + _kv(wave) + '\n}\n')
        addon = '"AddonInfo"\n{\n    "addonSteamAppID" "550"\n    "addonAuthor" "Loka"\n    "addonTitle" ' + _kv(title) + '\n    "addonContent_Sound" "1"\n}\n'
        script_payload='\n'.join(blocks).encode('utf-8')
        twin_path='scripts/necola_sound_'+plan.identifier+'.txt'
        for key, payload in ((script_path, script_payload), (twin_path,script_payload), ('addoninfo.txt', addon.encode('utf-8'))):
            check(); target = destination/key
            with opened(target) as output:
                output.write(payload)
            if target.read_bytes() != payload:
                raise ValueError('L4N 脚本/元数据写入校验失败: ' + key)
            manifest.append({'file':str(target), 'archive_path':key, 'sha256':_hash(target), 'bytes':len(payload)})
        if (destination/script_path).read_bytes()!=(destination/twin_path).read_bytes():raise ValueError('L4N twin scripts differ')
        cache = build_sound_cache(destination/'sound', cancelled=cancelled)
        cache_path = destination/'sound/sound.cache'
        if cache.path != str(cache_path) or not cache_path.is_file():
            raise ValueError('L4N 必需 sound.cache 未生成')
        stat = cache_path.stat(); owned_files[cache_path] = (stat.st_dev, stat.st_ino)
        decoded = read_sound_cache(cache_path.read_bytes())
        expected_sizes = {item['archive_path']: (destination/item['archive_path']).stat().st_size for item in plan.extra_sounds}
        if tuple(decoded) != tuple(cache.entries) or {e.path:e.file_size for e in decoded} != expected_sizes or _hash(cache_path) != cache.sha256:
            raise ValueError('L4N cache WAV 路径/长度/hash 不符')
        for source, digest in plan.source_hashes.items():
            check()
            if _hash(source) != digest:
                raise ValueError('L4N 准备期间源音频已修改: ' + str(source))
        manifest.append({'file':str(cache_path), 'archive_path':'sound/sound.cache', 'sha256':cache.sha256, 'bytes':cache_path.stat().st_size})
        check()
        return L4NResult(str(destination), plan.identifier, [e['event_name'] for e in plan.extra_sounds],
                         [e['archive_path'] for e in plan.extra_sounds], str(cache_path), cache.sha256,
                         plan.statistics, plan.decisions, manifest, plan.warnings)
    except BaseException as failure:
        errors = []
        for path, identity in reversed(list(owned_files.items())):
            try:
                if path.exists():
                    stat = path.stat()
                    if (stat.st_dev, stat.st_ino) != identity:
                        raise ValueError('L4N 文件已被其他操作替换，保留: ' + str(path))
                    path.unlink()
            except BaseException as error:
                errors.append(str(error))
        for path, identity in reversed(list(owned_dirs.items())):
            try:
                if not path.exists():
                    continue
                stat = path.lstat()
                if (stat.st_dev, stat.st_ino) != identity:
                    raise ValueError('L4N 目录已被其他操作替换，保留: ' + str(path))
                path.rmdir()
            except BaseException as error:
                errors.append(str(error))
        if errors:
            failure.add_note('L4N 回滚未完全成功: ' + '; '.join(errors))
        raise
