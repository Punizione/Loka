"""User-initiated, verified Windows Essentials installation using update proxies."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import zipfile

from .update_check import _https_connection, validate_proxy, UpdateCheckError


UNAVAILABLE_MESSAGE='当前机器未找到合适的FFmpeg处理库，请在设置页安装配置'
CHECKSUM_URL='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256'
_MAX_ARCHIVE=256*1024*1024
_MAX_EXECUTABLE=512*1024*1024
_TIMEOUT=15
_runtime_ffmpeg=None


class FFmpegInstallError(ValueError):
    pass


@dataclass(frozen=True)
class FFmpegInstallResult:
    executable:Path
    version:str
    reused:bool=False


def default_install_root():
    directory=os.environ.get('LOCALAPPDATA')
    if os.name!='nt' or not directory:
        raise FFmpegInstallError('FFmpeg 自动安装需要 Windows 当前用户目录。')
    return Path(directory)/'Loka/tools/ffmpeg'


def find_ffmpeg(executable=''):
    if executable:return shutil.which(executable)
    if _runtime_ffmpeg and Path(_runtime_ffmpeg).is_file():return _runtime_ffmpeg
    try:
        managed=default_install_root()/'bin/ffmpeg.exe'
        if managed.is_file():return str(managed)
    except FFmpegInstallError:
        pass
    return shutil.which('ffmpeg')


def _check(cancelled):
    if cancelled and cancelled():raise InterruptedError('已取消 FFmpeg 安装。')


def _allowed_url(url):
    parsed=urlsplit(url)
    if (parsed.scheme!='https' or parsed.hostname not in ('www.gyan.dev','gyan.dev')
            or parsed.port not in (None,443) or parsed.username or parsed.password
            or not parsed.path.startswith('/ffmpeg/builds/') or parsed.fragment or parsed.query):
        raise FFmpegInstallError('FFmpeg 下载地址不受支持。')
    return parsed


@contextmanager
def _response(url,proxy,cancelled):
    for _ in range(6):
        _check(cancelled);parsed=_allowed_url(url)
        connection=_https_connection(proxy,_TIMEOUT,host=parsed.hostname)
        try:
            connection.request('GET',parsed.path,headers={'User-Agent':'Loka-FFmpeg-setup','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                location=response.getheader('Location')
                if not location:raise FFmpegInstallError('FFmpeg 下载服务器返回了无效跳转。')
                url=urljoin(url,location);_allowed_url(url)
                continue
            if response.status!=200:
                raise FFmpegInstallError('FFmpeg 下载服务器暂不可用，请稍后重试。')
            yield response,url
            return
        finally:connection.close()
    raise FFmpegInstallError('FFmpeg 下载服务器跳转次数过多。')


def _checksum(proxy,cancelled):
    with _response(CHECKSUM_URL,proxy,cancelled) as (response,final_url):
        raw=bytearray();deadline=time.monotonic()+30
        while len(raw)<=4096:
            _check(cancelled)
            if time.monotonic()>deadline:raise FFmpegInstallError('FFmpeg 下载超时，请检查网络或代理设置。')
            block=response.read1(4097-len(raw))
            if not block:break
            raw.extend(block)
    if len(raw)>4096:raise FFmpegInstallError('FFmpeg 校验信息无效。')
    digest=raw.decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{64}',digest):raise FFmpegInstallError('FFmpeg 校验信息无效。')
    # Pin the ZIP to the checksum redirect's concrete release. Do not race a
    # changing latest ZIP against an older release's checksum.
    if not re.fullmatch(r'/ffmpeg/builds/packages/ffmpeg-[\w.-]+-essentials_build\.zip\.sha256',urlsplit(final_url).path):
        raise FFmpegInstallError('FFmpeg 校验地址未指向明确的 Essentials 版本。')
    return digest.lower(),final_url[:-7]


def _download(url,target,proxy,cancelled,progress):
    digest=hashlib.sha256();received=0;last=0.;deadline=time.monotonic()+900
    with _response(url,proxy,cancelled) as (response,_):
        total=int(response.getheader('Content-Length') or 0)
        if total<0 or total>_MAX_ARCHIVE:raise FFmpegInstallError('FFmpeg 下载文件大小不受支持。')
        progress('download',0,total)
        with target.open('wb') as stream:
            while True:
                _check(cancelled)
                if time.monotonic()>deadline:raise FFmpegInstallError('FFmpeg 下载超时，请检查网络或代理设置。')
                # read() waits for the entire buffer even while a slow server
                # keeps resetting socket timeouts. read1() yields received data
                # so cancellation and the overall deadline remain observable.
                block=response.read1(65536)
                if not block:break
                received+=len(block)
                if received>_MAX_ARCHIVE:raise FFmpegInstallError('FFmpeg 下载文件大小不受支持。')
                stream.write(block);digest.update(block)
                now=time.monotonic()
                if now-last>=.1:progress('download',received,total);last=now
        if total and received!=total:raise FFmpegInstallError('FFmpeg 下载文件不完整，请重试。')
        progress('download',received,total)
    _check(cancelled)
    return digest.hexdigest()


def _unpack(archive_path,destination,cancelled):
    with zipfile.ZipFile(archive_path) as archive:
        executable=[];members={}
        for entry in archive.infolist():
            path=PurePosixPath(entry.filename.replace('\\','/'))
            if (path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts)
                    or stat.S_ISLNK(entry.external_attr>>16)):
                raise FFmpegInstallError('FFmpeg 安装包包含不安全的路径。')
            if len(path.parts)==3 and path.parts[-2:]==('bin','ffmpeg.exe'):
                executable.append((path,entry))
            if str(path) in members:raise FFmpegInstallError('FFmpeg 安装包包含重复文件。')
            members[str(path)]=entry
        if len(executable)!=1:raise FFmpegInstallError('FFmpeg 安装包未包含唯一的 ffmpeg.exe。')
        path,entry=executable[0]
        if not 0<entry.file_size<=_MAX_EXECUTABLE:raise FFmpegInstallError('FFmpeg 可执行文件大小不受支持。')
        selected=[(entry,Path('bin/ffmpeg.exe'),_MAX_EXECUTABLE)]
        for name in ('LICENSE','LICENSE.txt','README.txt'):
            entry=members.get(path.parts[0]+'/'+name)
            if entry is not None:selected.append((entry,Path(name),1024*1024))
        for entry,relative,limit in selected:
            if entry.file_size>limit:raise FFmpegInstallError('FFmpeg 安装包文件大小不受支持。')
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
            count=0
            with archive.open(entry) as source,target.open('wb') as output:
                while block:=source.read(65536):
                    _check(cancelled);count+=len(block)
                    if count>limit:raise FFmpegInstallError('FFmpeg 安装包文件大小不受支持。')
                    output.write(block)


def ffmpeg_version(executable):
    """Minimal installed-tool check; do not decode or convert any audio."""
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    command=[str(executable),'-version']
    run=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=10,creationflags=flags)
    text=run.stdout.decode('utf-8',errors='replace')
    if run.returncode or not text.startswith('ffmpeg version '):
        raise FFmpegInstallError('下载的 FFmpeg 无法正常运行。')
    return text.splitlines()[0]


def verify_ffmpeg(executable):
    version=ffmpeg_version(executable)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    # Exercise the same WAV input, PCM output, mono mixing and resampling used
    # by Loka rather than trusting the executable's version banner alone.
    fmt=struct.pack('<HHIIHH',1,2,48000,192000,4,16);body=b'\0'*480*4
    wave=b'RIFF'+struct.pack('<I',36+len(body))+b'WAVEfmt '+struct.pack('<I',16)+fmt+b'data'+struct.pack('<I',len(body))+body
    command=[str(executable),'-hide_banner','-v','error','-nostdin','-i','pipe:0',
             '-acodec','pcm_u8','-ac','1','-ar','44100','-f','u8','pipe:1']
    run=subprocess.run(command,input=wave,capture_output=True,timeout=10,creationflags=flags)
    if run.returncode or len(run.stdout)!=441:
        raise FFmpegInstallError('下载的 FFmpeg 未通过音频处理验证。')
    return version


def _existing_install(executable,cancelled):
    _check(cancelled)
    try:version=ffmpeg_version(executable)
    except (OSError,ValueError,subprocess.SubprocessError):return None
    _check(cancelled)
    # Keep audio lookup aligned with the tool we actually ran, even if an
    # unusable managed copy would otherwise shadow a valid PATH executable.
    # This choice is process-local; registry and environment remain untouched.
    global _runtime_ffmpeg
    _runtime_ffmpeg=str(Path(executable).absolute())
    return FFmpegInstallResult(Path(_runtime_ffmpeg),version,True)


def _path_key(value):
    return ntpath.normcase(ntpath.normpath(os.path.expandvars(value.strip().strip('"'))))


def _broadcast_environment_change():
    import ctypes
    from ctypes import wintypes
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.SendMessageTimeoutW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPCWSTR,
                                      wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype=wintypes.LPARAM
    result=ctypes.c_size_t()
    user.SendMessageTimeoutW(0xffff,0x001A,0,'Environment',2,1000,ctypes.byref(result))


def configure_user_path(folder):
    import winreg
    directory=str(folder);identity=_path_key(directory)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,'Environment',0,winreg.KEY_READ|winreg.KEY_WRITE) as key:
        try:original,kind=winreg.QueryValueEx(key,'Path')
        except FileNotFoundError:original,kind='',winreg.REG_EXPAND_SZ
        if not isinstance(original,str) or kind not in (winreg.REG_SZ,winreg.REG_EXPAND_SZ):
            raise FFmpegInstallError('用户 PATH 格式无效，无法自动配置。')
        if identity not in {_path_key(part) for part in original.split(';') if part.strip()}:
            updated=original+('' if not original or original.endswith(';') else ';')+directory
            if len(updated)>32766:raise FFmpegInstallError('用户 PATH 过长，无法自动配置。')
            winreg.SetValueEx(key,'Path',0,kind,updated)
    current=os.environ.get('PATH','')
    others=[part for part in current.split(';') if part and _path_key(part)!=identity]
    os.environ['PATH']=';'.join([directory,*others])
    _broadcast_environment_change()


def install_ffmpeg(*,proxy_url='',install_root=None,cancelled=None,on_progress=None):
    global _runtime_ffmpeg
    progress=on_progress or (lambda phase,done,total:None)
    _check(cancelled);progress('detect',0,0)
    # A working PATH installation needs neither a managed installation folder
    # nor valid proxy settings. Probe before any network/configuration work.
    existing=shutil.which('ffmpeg')
    if existing:
        result=_existing_install(existing,cancelled)
        if result is not None:return result
    root=Path(install_root) if install_root is not None else default_install_root()
    executable=root/'bin/ffmpeg.exe'
    if executable.is_file() and (not existing or _path_key(str(executable))!=_path_key(existing)):
        result=_existing_install(executable,cancelled)
        if result is not None:return result
    try:proxy=validate_proxy(proxy_url)
    except UpdateCheckError as error:raise FFmpegInstallError('代理地址无效，请检查更新与网络设置。') from error
    try:
        _check(cancelled)
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.install-',dir=root) as folder:
            staging=Path(folder);progress('metadata',0,0)
            expected,url=_checksum(proxy,cancelled)
            archive=staging/'download.zip'
            actual=_download(url,archive,proxy,cancelled,progress)
            if actual!=expected:raise FFmpegInstallError('FFmpeg 下载文件 SHA256 校验失败，请重试。')
            _check(cancelled);progress('verify',0,0)
            unpacked=staging/'unpacked';_unpack(archive,unpacked,cancelled)
            version=verify_ffmpeg(unpacked/'bin/ffmpeg.exe')
            _check(cancelled)
            executable.parent.mkdir(parents=True,exist_ok=True)
            for name in ('LICENSE','LICENSE.txt','README.txt'):
                source=unpacked/name
                if source.is_file():os.replace(source,root/name)
            os.replace(unpacked/'bin/ffmpeg.exe',executable)
        progress('configure',0,0);configure_user_path(executable.parent)
        _runtime_ffmpeg=str(executable.absolute())
        return FFmpegInstallResult(executable,version)
    except (InterruptedError,FFmpegInstallError):raise
    except (OSError,ValueError,http.client.HTTPException,zipfile.BadZipFile,subprocess.SubprocessError) as error:
        raise FFmpegInstallError('FFmpeg 下载或配置失败：'+str(error)) from error
