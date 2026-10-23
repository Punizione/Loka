"""Stage, validate and journal replacement of a bound correction result."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile

from .action_session import fingerprint, owned_path,with_timing_reference
from .weapon_compile import compile_and_fill
from .vpk_writer import write_vpk
from .vpk import VpkArchive


@contextmanager
def save_lock(work):
    """OS locks release on process death; stale marker files never block recovery."""
    path = owned_path(Path(work) / '.loka_action_save.lock', work)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:raise ValueError('此转模结果正在保存或恢复，请稍后重试。') from error
        try:yield
        finally:
            handle.seek(0)
            if os.name == 'nt':msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_copy(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.loka_action_', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output, Path(source).open('rb') as original:
            shutil.copyfileobj(original, output, 1024 * 1024)
            output.flush();os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.is_file():temporary.unlink()


def _write_json(path, data):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix='.journal_', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n');handle.flush();os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.is_file():temporary.unlink()


def _destination(path, work, output, source):
    path = Path(path).absolute()
    if path == source:raise ValueError('动作矫正不能覆盖原始来源。')
    if path == output:return owned_path(path, output.parent)
    owned_path(path, work)
    allowed = (path.is_relative_to(work/'pack') or path.is_relative_to(work/'v_dec/_converted')
               or path in {work/'compile_result.json', work/'action_correction_state.json',
                           work/'v_dec/sequence_plan.json',work/'v_dec/animation_semantics.json'})
    if not allowed:raise ValueError('保存目标不是成品工作资源: ' + str(path))
    return path


def _rollback(journal_path, journal, work, output, source):
    errors = []
    for item in reversed(journal['operations']):
        try:
            destination = _destination(item['destination'], work, output, source)
            current = fingerprint(destination) if destination.is_file() else None
            if current == item['old_hash']:continue
            if current != item['new_hash']:
                raise ValueError('提交后文件被外部修改，保留现场: ' + str(destination))
            if item['old_hash'] is None:
                destination.unlink()
            else:
                backup = owned_path(item['backup'], journal_path.parent/'backup')
                if not backup.is_file() or fingerprint(backup) != item['old_hash']:
                    raise ValueError('恢复备份校验失败: ' + str(backup))
                _atomic_copy(backup, destination)
        except Exception as error:errors.append(str(error))
    journal['status'] = 'recovery_required' if errors else 'rolled_back'
    journal['recovery_errors'] = errors
    _write_json(journal_path, journal)
    if errors:raise ValueError('保存未完全恢复；保留事务备份 ' + str(journal_path.parent) + '\n' + '\n'.join(errors))


def recover_pending(work_dir, output_path, input_path, *, locked=False):
    work, output, source = Path(work_dir).absolute(), Path(output_path).absolute(), Path(input_path).absolute()
    if not locked:
        with save_lock(work):return recover_pending(work, output, source, locked=True)
    parent = owned_path(work/'.loka_action_transactions', work)
    if not parent.exists():return
    for path in sorted(parent.glob('*/journal.json')):
        owned_path(path, parent)
        journal = json.loads(path.read_text(encoding='utf-8'))
        if journal.get('version') != 1 or (journal.get('work_dir'),journal.get('output_path'),journal.get('input_path')) != (str(work),str(output),str(source)):
            raise ValueError('恢复记录身份无效: ' + str(path))
        if journal['status'] in ('prepared','committing','recovery_required'):
            _rollback(path, journal, work, output, source)


def _commit(work, output, source, replacements, expected, check):
    parent = owned_path(work/'.loka_action_transactions', work)
    parent.mkdir(exist_ok=True)
    transaction = parent/secrets.token_hex(8)
    (transaction/'backup').mkdir(parents=True)
    operations = []
    for index,(destination,staged) in enumerate(replacements.items()):
        destination = _destination(destination, work, output, source)
        current = fingerprint(destination) if destination.is_file() else None
        if current != expected.get(destination):raise ValueError('提交前文件已被修改: ' + str(destination))
        backup = transaction/'backup'/str(index)
        if current is not None:shutil.copy2(destination, backup)
        operations.append({'destination':str(destination),'backup':str(backup),'old_hash':current,
                           'new_hash':fingerprint(staged) if staged is not None else None})
    check()
    journal = dict(version=1,status='prepared',work_dir=str(work),output_path=str(output),
                   input_path=str(source),operations=operations)
    path = transaction/'journal.json'
    _write_json(path, journal)
    try:
        journal['status'] = 'committing';_write_json(path,journal)
        for item,(destination,staged) in zip(operations,replacements.items()):
            check()
            destination = _destination(destination,work,output,source)
            current = fingerprint(destination) if destination.is_file() else None
            if current != item['old_hash']:raise ValueError('提交期间文件已被修改: ' + str(destination))
            if staged is None:destination.unlink()
            else:_atomic_copy(staged,destination)
            actual = fingerprint(destination) if destination.is_file() else None
            if actual != item['new_hash']:raise ValueError('保存写入校验失败: ' + str(destination))
        journal['status'] = 'committed';_write_json(path,journal)
    except BaseException as failure:
        try:_rollback(path,journal,work,output,source)
        except Exception as restore_error:failure.add_note(str(restore_error))
        raise
    return path


def _verify_unchanged_entries(original, candidate, allowed, check):
    before, after = VpkArchive(original, include_all=True), VpkArchive(candidate, include_all=True)
    if set(before.entries)-allowed != set(after.entries)-allowed:
        raise ValueError('矫正封包改变了非目标资源清单。')
    for key in sorted(set(before.entries)-allowed):
        check()
        if before.original_names[key] != after.original_names[key] or before.read(key) != after.read(key):
            raise ValueError('矫正封包改变了非目标资源: ' + key)


def save_session(session, *, cancelled=None, on_log=None):
    log = on_log or (lambda message:None)
    def check():
        if cancelled and cancelled():raise InterruptedError('动作矫正保存已取消。')
    with save_lock(session.work_dir):
        check();session.check_unchanged()
        changes = session.changes
        if not changes:return None
        from .action_rules import retime_qc,load_actions,_QcDocument
        from .action_swaps import permute_qc
        from .action_removal import prune_sequences,qc_slots
        from .fps_values import round_qc_fps
        work, output, source = session.work_dir, session.output_path, session.input_path
        stage = Path(tempfile.mkdtemp(prefix='.loka_action_stage_', dir=work))
        candidate = output.parent/(output.stem+'.loka_action_'+secrets.token_hex(8)+'.vpk')
        candidate_hash = None
        try:
            log('准备动作矫正副本')
            shutil.copytree(session.pack_dir, stage/'pack')
            by_model = {}
            for change in changes:
                updates=by_model.setdefault(change.model_index,{})
                if not change.removed:updates[change.action.index]=change.new_fps
            qc_paths, retired = [], []
            original_qcs = {}
            staged_actions = {}
            staged_timings = {}
            staged_slots = {}
            for model_index, updates in by_model.items():
                check()
                model = session.models[model_index]
                staged_qc = stage/model.qc_path.relative_to(work)
                staged_qc.parent.parent.mkdir(parents=True,exist_ok=True)
                shutil.copytree(model.qc_path.parent, staged_qc.parent)
                assignments={destination:donor for (i,destination),donor in session.assignments.items() if i==model_index}
                staged_qc.write_text(permute_qc(model.qc_path,assignments,model.profile_id),encoding='utf-8',newline='\n')
                staged_qc.write_text(round_qc_fps(retime_qc(staged_qc,updates)), encoding='utf-8',newline='\n')
                removed={index for i,index in session.removed_sequences if i==model_index}
                indexes={slot.index:slot.official_index for slot in model.sequence_slots}
                text,mapping=prune_sequences(staged_qc.read_text(encoding='utf-8'),removed,indexes)
                staged_qc.write_text(text,encoding='utf-8',newline='\n')
                indexes={mapping[i]:official for i,official in indexes.items() if i in mapping}
                staged_slots[model_index]=qc_slots(_QcDocument(staged_qc),indexes)
                timings={mapping[a.index]:session.timing_index(model_index,a.index) for a in model.swap_actions or model.actions
                         if a.index in mapping and session.timing_index(model_index,a.index) is not None}
                staged_timings[model_index]=tuple(sorted(timings.items()))
                staged_actions[model_index]=tuple(tuple(with_timing_reference(a,model.profile_id,timings[a.index])
                                                       if a.index in timings else a for a in actions)
                    for actions in (load_actions(staged_qc,model.profile_id,indexes),
                                    load_actions(staged_qc,model.profile_id,indexes,include_timers=True)))
                qc_paths.append(staged_qc);original_qcs[str(staged_qc)] = str(model.qc_path)
                retired.append(model.model_path.relative_to(session.pack_dir).as_posix())
            bindings = []
            if session.result.material_report_file:
                bindings = deepcopy(json.loads(Path(session.result.material_report_file).read_text(encoding='utf-8'))['qc_bindings'])
                reverse = {value:key for key,value in original_qcs.items()}
                for binding in bindings:
                    binding['qc'] = reverse.get(binding['qc'],binding['qc'])
            origins = None
            next_origins = None
            if session.result.animation_semantics_file:
                raw = json.loads(Path(session.result.animation_semantics_file).read_text(encoding='utf-8'))
                next_origins=deepcopy(raw)
                for model_index in by_model:
                    model=session.models[model_index]
                    original=raw[str(model.qc_path)]
                    document=_QcDocument(model.qc_path)
                    for (i,destination),donor in session.assignments.items():
                        if i==model_index:
                            next_origins[str(model.qc_path)][document.sequences[destination].name]=deepcopy(
                                original[document.sequences[donor].name])
                    for i,index in session.removed_sequences:
                        if i==model_index:next_origins[str(model.qc_path)].pop(document.sequences[index].name,None)
                origins = {staged:next_origins[original] for staged,original in original_qcs.items()}
            log('编译并验证修改后的 V 模')
            compiled = compile_and_fill(stage/'pack',qc_paths,session.options.compiler_path,
                game_path=session.options.game_path,retired_models=retired,
                archive_names=session.result.archive_names,keep_ads=session.options.keep_ads,
                bindings=bindings,semantic_origins=origins,cancelled=cancelled,on_log=log)
            check();session.check_unchanged()
            log('封包并核对未修改资源')
            package = write_vpk(stage/'pack',candidate,archive_names=compiled.archive_names,
                forbidden_paths=[source,output],cancelled=cancelled,on_log=log)
            candidate_hash = package.sha256
            allowed = set(compiled.compiled_files)|set(compiled.retired_files)
            _verify_unchanged_entries(output,candidate,allowed,check)
            check();session.check_unchanged()
            replacements = {}
            for staged,original in original_qcs.items():replacements[Path(original)] = Path(staged)
            for key in sorted(allowed):
                staged = stage/'pack'/key
                replacements[session.pack_dir/key] = staged if staged.is_file() else None
            # Preserve all other model records and stable source QC identities.
            records = deepcopy(session.result.compiled_models)
            for row in compiled.models:
                row = deepcopy(row);row['qc'] = original_qcs[row['qc']]
                records = [r for r in records if r['qc'] != row['qc']]+[row]
            report = asdict(compiled)
            merged_files=sorted(set(session.result.compiled_model_files)-set(compiled.retired_files)|set(compiled.compiled_files))
            report.update(models=records,archive_names=compiled.archive_names,compiled_files=merged_files)
            report_path = stage/'compile_result.json'
            _write_json(report_path,report)
            replacements[work/'compile_result.json'] = report_path
            if next_origins is not None:
                origin_path=owned_path(session.result.animation_semantics_file,work)
                staged_origin=stage/origin_path.relative_to(work)
                staged_origin.parent.mkdir(parents=True,exist_ok=True)
                _write_json(staged_origin,next_origins)
                replacements[origin_path]=staged_origin
            for plan_path in session.result.sequence_plan_files:
                original_plan=owned_path(plan_path,work)
                plan=json.loads(original_plan.read_text(encoding='utf-8'))
                changed=False
                for target in plan['targets']:
                    # Metadata follows the donor payload while official layout stays in place.
                    before=deepcopy(target['sequences'])
                    for (i,destination),donor in session.assignments.items():
                        if target['target_profile']!=session.models[i].profile_id:continue
                        identity={key:before[destination].get(key) for key in ('name','activity','official_index','action_correction_timing_index')}
                        target['sequences'][destination]=deepcopy(before[donor])
                        target['sequences'][destination].update(identity)
                        target['sequences'][destination]['action_correction_source_index']=donor
                        changed=True
                    for change in changes:
                        if change.removed:continue
                        if target['target_profile']!=change.model.profile_id:continue
                        row=target['sequences'][change.action.index]
                        row['fps']=change.new_fps
                        donor=session.source_index(change.model_index,change.action.index)
                        donor_fps=session.baseline_action(change.model_index,donor,all_slots=True).fps
                        if row.get('fps_scale') is not None:
                            row['fps_scale']*=change.new_fps/donor_fps
                        row['action_correction_fps']={'old':change.old_fps,'new':change.new_fps}
                        changed=True
                    for model_index in by_model:
                        model=session.models[model_index]
                        if target['target_profile']!=model.profile_id:continue
                        removed={index for i,index in session.removed_sequences if i==model_index}
                        target['sequences']=[row for index,row in enumerate(target['sequences']) if index not in removed]
                        document=_QcDocument(next(Path(staged) for staged,original in original_qcs.items()
                                                  if Path(original)==model.qc_path))
                        for index,row in enumerate(target['sequences']):
                            row['fps']=document.references(document.sequences[index])[0].fps
                            timing=dict(staged_timings[model_index]).get(index)
                            if timing is not None:row['action_correction_timing_index']=timing
                        changed=True
                if changed:
                    staged_plan=stage/original_plan.relative_to(work)
                    staged_plan.parent.mkdir(parents=True,exist_ok=True)
                    _write_json(staged_plan,plan)
                    replacements[original_plan]=staged_plan
            audit = [dict(model=c.model.profile_id,index=c.action.index,name=c.action.name,
                          activity=c.action.activity,old_fps=c.old_fps,new_fps=c.new_fps,
                          source_index=session.source_index(c.model_index,c.action.index),removed=c.removed) for c in changes]
            state_path = stage/'action_correction_state.json'
            previous = work/'action_correction_state.json'
            history = json.loads(previous.read_text(encoding='utf-8')).get('history',[]) if previous.is_file() else []
            _write_json(state_path,dict(output_path=str(output),package_sha256=package.sha256,
                history=history+[audit],archive_names=compiled.archive_names))
            replacements[previous] = state_path
            # The candidate VPK is committed last; all originals have recoverable backups.
            replacements[output] = candidate
            # All fallible decoding, validation and hashing happens before commit.
            # Once published, accepting the new immutable records requires no disk reads.
            next_models=tuple(replace(model,actions=staged_actions[index][0],swap_actions=staged_actions[index][1],
                                      timing_indexes=staged_timings[index],sequence_slots=staged_slots[index])
                              if index in staged_actions else model for index,model in enumerate(session.models))
            next_fingerprints=dict(session.fingerprints)
            for destination,staged in replacements.items():
                if staged is None:next_fingerprints.pop(destination,None)
                else:next_fingerprints[destination]=fingerprint(staged)
            accepted_package=replace(package,path=str(output))
            log('校验通过，替换成品 QC 和 VPK')
            journal = _commit(work,output,source,replacements,session.fingerprints,check)
            session.result.compiled_models = records
            session.result.archive_names = compiled.archive_names
            session.result.compiled_model_files = merged_files
            session.result.package_sha256 = package.sha256
            session.result.compile_report_file=str(work/'compile_result.json')
            session.models=next_models
            session.fingerprints=next_fingerprints
            session._inventory=set(next_fingerprints)
            session.updates.clear()
            session.assignments.clear()
            session.removed_sequences.clear()
            session.timing_overrides.clear()
            session._preview_models.clear()
            try:log('动作矫正已保存；恢复备份: '+str(journal.parent))
            except Exception as error:
                session.result.warnings.append('动作矫正已保存，状态日志未能刷新: '+str(error))
            return accepted_package
        finally:
            try:
                if candidate_hash and candidate.is_file() and fingerprint(candidate) == candidate_hash:
                    candidate.unlink()
            except OSError as error:
                # Cleanup cannot revoke a successful publication or obscure the
                # original staging/commit error. Keep the identifiable candidate.
                session.result.warnings.append('动作矫正候选文件未能清理，保留在 '+str(candidate)+': '+str(error))
