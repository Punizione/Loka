"""Stream and verify single-file VPK v1 archives, publishing without overwrite."""
from dataclasses import dataclass
from pathlib import Path,PurePosixPath
import hashlib,os,secrets,stat,struct,tempfile,zlib
from .vpk import VpkArchive,safe_archive_path

CHUNK=1024*1024


@dataclass(frozen=True)
class VpkWriteResult:
    path: str
    entry_count: int
    size: int
    sha256: str


def hash_file(path,*,cancelled=None):
    checksum=0;sha=hashlib.sha256();size=0
    with path.open('rb') as handle:
        while data:=handle.read(CHUNK):
            if cancelled and cancelled():raise InterruptedError('VPK 封包已取消')
            checksum=zlib.crc32(data,checksum);sha.update(data);size+=len(data)
    return checksum,sha.hexdigest(),size


def write_vpk(pack_dir,output_path,*,archive_names=None,forbidden_paths=(),cancelled=None,on_log=None):
    pack=Path(pack_dir).resolve();output=Path(output_path).resolve()
    if pack.name!='pack' or not pack.is_dir():raise ValueError('封包只能读取独立 pack 目录')
    if output.is_relative_to(pack.parent) or output.suffix.lower()!='.vpk':raise ValueError('最终 VPK 必须位于工作区外')
    if output in {Path(p).resolve() for p in forbidden_paths}:raise ValueError('最终 VPK 不能覆盖输入文件')
    if output.exists():raise FileExistsError('结果 VPK 已存在: '+str(output))
    if not output.parent.is_dir():raise ValueError('结果目录不存在')
    def check():
        if cancelled and cancelled():raise InterruptedError('VPK 封包已取消')
    check();files={}
    for path in pack.rglob('*'):
        if not path.is_file():continue
        if not path.resolve().is_relative_to(pack):raise ValueError('pack 资源路径越界')
        key=safe_archive_path(path.relative_to(pack).as_posix()).lower()
        if key in files:raise ValueError('pack 大小写路径冲突')
        files[key]=path
    if archive_names is not None and set(archive_names)!=set(files):raise ValueError('归档路径清单与 pack 文件不一致')
    infos=[];names=set()
    for key,path in files.items():
        name=safe_archive_path((archive_names or {}).get(key,path.relative_to(pack).as_posix()))
        if name.lower()!=key or name.lower() in names:raise ValueError('归档路径映射不正确/冲突')
        names.add(name.lower());checksum,sha,size=hash_file(path,cancelled=cancelled)
        rel=PurePosixPath(name);extension=rel.suffix[1:] if rel.suffix else ' '
        directory=rel.parent.as_posix() if rel.parent.as_posix()!='.' else ' '
        stem=rel.name[:-len(rel.suffix)] if rel.suffix else rel.name
        infos.append(dict(key=key,path=path,name=name,extension=extension,directory=directory,stem=stem,crc=checksum,sha=sha,size=size))
    infos.sort(key=lambda item:(item['extension'],item['directory'],item['stem']))
    if any(item['size']>0xffffffff for item in infos) or sum(item['size'] for item in infos)>0xffffffff:
        raise ValueError('单文件 VPK 超过32位大小限制')
    tree=bytearray();offset=0;last_extension=None;last_directory=None
    for item in infos:
        if item['extension']!=last_extension:
            if last_extension is not None:tree.extend(b'\0\0')
            tree.extend(item['extension'].encode('utf-8')+b'\0');last_extension=item['extension'];last_directory=None
        elif item['directory']!=last_directory:tree.extend(b'\0')
        if item['directory']!=last_directory:
            tree.extend(item['directory'].encode('utf-8')+b'\0');last_directory=item['directory']
        item['offset']=offset;tree.extend(item['stem'].encode('utf-8')+b'\0')
        tree.extend(struct.pack('<IHHIIH',item['crc'],0,0x7fff,offset,item['size'],0xffff));offset+=item['size']
    tree.extend(b'\0\0\0' if infos else b'\0')
    if 12+len(tree)+offset>0xffffffff:raise ValueError('单文件 VPK 总长度超过32位限制')
    header=struct.pack('<III',0x55aa1234,1,len(tree));temporary=None
    try:
        check();fd,name=tempfile.mkstemp(prefix=output.name+'.',suffix='.loka_vpk.tmp',dir=output.parent);temporary=Path(name)
        with os.fdopen(fd,'wb') as handle:
            handle.write(header);handle.write(tree)
            for item in infos:
                check();sha=hashlib.sha256();size=0
                with item['path'].open('rb') as source:
                    while data:=source.read(CHUNK):check();handle.write(data);sha.update(data);size+=len(data)
                if size!=item['size'] or sha.hexdigest()!=item['sha']:raise ValueError('封包过程中资源已修改: '+item['name'])
            handle.flush();os.fsync(handle.fileno())
        archive=VpkArchive(temporary,include_all=True)
        if set(archive.entries)!=set(files) or archive.data_offset!=12+len(tree) or archive.data_size!=offset:raise ValueError('VPK 目录/长度验证失败')
        overall=hashlib.sha256()
        with temporary.open('rb') as handle:
            overall.update(handle.read(archive.data_offset))
            for item in infos:
                check();entry=archive.entries[item['key']]
                if (entry.crc,entry.archive,entry.offset,entry.length,entry.preload)!=(item['crc'],0x7fff,item['offset'],item['size'],b''):
                    raise ValueError('VPK 条目验证失败')
                if archive.original_names[item['key']]!=item['name']:raise ValueError('VPK 路径拼写验证失败')
                remaining=entry.length;sha=hashlib.sha256();checksum=0
                while remaining:
                    check();data=handle.read(min(CHUNK,remaining))
                    if not data:raise ValueError('VPK 数据不完整')
                    remaining-=len(data);sha.update(data);overall.update(data);checksum=zlib.crc32(data,checksum)
                if sha.hexdigest()!=item['sha'] or checksum!=item['crc']:raise ValueError('VPK 内容校验失败')
        check()
        if on_log:on_log('VPK 已验证，准备发布: '+str(output))
        check()
        if os.name=='nt':os.rename(temporary,output)
        else:os.link(temporary,output);temporary.unlink()
        temporary=None
        return VpkWriteResult(str(output),len(infos),12+len(tree)+offset,overall.hexdigest())
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()


def write_vpk_set(packages,*,forbidden_paths=(),cancelled=None,on_log=None)->list[VpkWriteResult]:
    """Verify every archive first, then publish in order with owned rollback.

    Multiple no-overwrite publications are not one atomic filesystem transaction.
    A replaced/modified artifact is retained and identified in failure notes.
    """
    def check():
        if cancelled and cancelled():raise InterruptedError('VPK 集合封包已取消')
    def real(path):
        for component in (path,*path.parents):
            if component.is_symlink() or (hasattr(component,'is_junction') and component.is_junction()):
                raise ValueError('VPK 集合不支持别名路径: '+str(component))
    def walk_error(error):
        raise OSError(error.errno,'VPK 集合无法读取资源目录: '+str(error),error.filename) from error
    forbidden={Path(os.path.abspath(p)) for p in forbidden_paths}
    prepared=[];outputs=set()
    for pack_dir,output_path,names in packages:
        check();pack=Path(os.path.abspath(pack_dir));output=Path(os.path.abspath(output_path))
        real(pack);real(output)
        if pack.name!='pack' or not pack.is_dir():raise ValueError('封包只能读取独立 pack 目录')
        if output.suffix.lower()!='.vpk' or not output.parent.is_dir():raise ValueError('结果必须为已有目录中的 VPK')
        safe_archive_path(output.name)
        if output in outputs or output in forbidden:raise ValueError('VPK 集合结果重复或覆盖输入')
        if output.exists():raise FileExistsError('结果 VPK 已存在: '+str(output))
        inventory={}
        for directory,directories,filenames in os.walk(pack,followlinks=False,onerror=walk_error):
            check()
            for name in directories+filenames:real(Path(directory)/name)
            for name in filenames:
                path=Path(directory)/name
                if not path.is_file():raise ValueError('VPK 集合资源不是普通文件: '+str(path))
                relative=safe_archive_path(path.relative_to(pack).as_posix());key=relative.lower()
                if key in inventory:raise ValueError('pack 大小写路径冲突')
                inventory[key]=relative
        if names is None:names=inventory
        elif set(names)!=set(inventory):raise ValueError('归档路径清单与 pack 文件不一致')
        outputs.add(output);prepared.append((pack,output,names))
    if any(output.is_relative_to(pack.parent) for _,output,_ in prepared for pack,_,_ in prepared):
        raise ValueError('最终 VPK 必须位于全部工作区外')
    stages=[];published=[]

    def snapshot(path,result,index):
        before=path.lstat()
        if not stat.S_ISREG(before.st_mode):raise ValueError('VPK stage 不是普通文件: '+str(path))
        # Capture the first available identity before hash/read/re-stat can fail.
        stages[index]=(*stages[index][:3],(before.st_dev,before.st_ino,result.size,result.sha256))
        _,digest,length=hash_file(path)
        after=path.lstat()
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or (
                digest,length)!=(result.sha256,result.size):
            raise ValueError('VPK stage 校验后已修改: '+str(path))
        return (before.st_dev,before.st_ino,result.size,result.sha256)

    def owned(path,identity):
        real(path)
        before=path.lstat()
        if not stat.S_ISREG(before.st_mode) or (before.st_dev,before.st_ino,before.st_size)!=identity[:3]:return False
        _,digest,length=hash_file(path)
        after=path.lstat()
        return digest==identity[3] and length==identity[2] and (
            before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(
            after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)

    try:
        for pack,output,names in prepared:
            check()
            for _ in range(100):
                stage=output.parent/('.'+output.name+'.'+secrets.token_hex(16)+'.loka_set.vpk')
                if not stage.exists():break
            else:raise FileExistsError('无法分配唯一 VPK stage')
            result=write_vpk(pack,stage,archive_names=names,forbidden_paths=forbidden,
                             cancelled=cancelled,on_log=on_log)
            # The archive exists now. Even an initial identity-read failure must
            # leave a tracked candidate with a precise retained-path diagnostic.
            stages.append((stage,output,result,None))
            snapshot(stage,result,len(stages)-1)
        for stage,_,_,identity in stages:
            check()
            if not owned(stage,identity):raise ValueError('VPK stage 所有权或内容改变: '+str(stage))
        pending={s for s,_,_,_ in stages}
        for stage,output,result,identity in stages:
            check()
            if on_log:on_log('VPK 集合已验证，准备发布: '+str(output))
            check();real(output)
            # Recheck all unpublished stages after callback boundaries.
            for remaining,_,_,remaining_identity in stages:
                if remaining in pending and not owned(remaining,remaining_identity):
                    raise ValueError('VPK stage 所有权或内容改变: '+str(remaining))
            if not owned(stage,identity):raise ValueError('VPK stage 所有权或内容改变: '+str(stage))
            if os.name=='nt':os.rename(stage,output)
            else:os.link(stage,output)
            published.append((output,identity))
            if os.name!='nt':stage.unlink()
            pending.remove(stage)
            if not owned(output,identity):raise ValueError('发布 VPK 被替换或修改: '+str(output))
        for output,identity in published:
            if not owned(output,identity):raise ValueError('发布 VPK 被替换或修改: '+str(output))
        return [VpkWriteResult(str(output),result.entry_count,result.size,result.sha256)
                for _,output,result,_ in stages]
    except BaseException as failure:
        notes=[]
        for path,identity in [*reversed(published),*[(s,i) for s,_,_,i in reversed(stages)]]:
            try:
                if not path.exists() and not path.is_symlink():continue
                if identity is None:
                    notes.append('无法确认初始所有权，保留 stage: '+str(path));continue
                if not owned(path,identity):
                    notes.append('所有权/内容已改变，保留其他操作的文件: '+str(path));continue
                path.unlink()
            except BaseException as error:notes.append('无法验证或清理，保留文件 '+str(path)+': '+str(error))
        if notes:failure.add_note('VPK 集合回滚未完全成功: '+'; '.join(notes))
        raise
