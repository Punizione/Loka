"""Exact-frame PCM writer and cancellable direct FFmpeg conversion."""
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID
import errno,hashlib,os,struct,subprocess,tempfile,threading,time
from .weapon_audio_metadata import WaveMetadata,PCM_WIDTHS,validate_target
from .ffmpeg_setup import find_ffmpeg, UNAVAILABLE_MESSAGE


class UnsupportedWaveFormatError(ValueError):
    """A structurally valid WAV describes a codec we do not handle."""


@dataclass(frozen=True)
class RemovedWaveChunk:
    kind:str
    offset:int
    declared_bytes:int
    actual_bytes:int
    reason:str

@dataclass(frozen=True)
class WaveContainerRepair:
    original_sha256:str
    declared_file_bytes:int
    actual_file_bytes:int
    removed_bytes:int
    removed_chunks:tuple[RemovedWaveChunk,...]
    reason:str

@dataclass(frozen=True)
class AnalyzedWave:
    payload:bytes
    metadata:WaveMetadata
    source_sha256:str
    container_repair:WaveContainerRepair|None=None

@dataclass(frozen=True)
class NormalizedWave:
    payload:bytes
    metadata:WaveMetadata
    source_sha256:str
    output_sha256:str
    converted_frames:int
    pad_frames:int
    trim_frames:int
    method:str
    source_metadata:WaveMetadata|None=None
    target_reference:WaveMetadata|None=None
    container_repair:WaveContainerRepair|None=None

def _check(cancelled):
    if cancelled and cancelled():raise InterruptedError('WAV normalization cancelled')

def _bytes(source):return source if isinstance(source,bytes) else Path(source).read_bytes()

def _info_kind(kind):
    return len(kind)==4 and kind[:1]==b'I' and all(65<=byte<=90 or 48<=byte<=57 for byte in kind[1:])

def _validate_info(data,start,end):
    position=start
    while position<end:
        if position+8>end or not all(32<=byte<=126 for byte in data[position:position+4]):raise ValueError('Invalid WAV LIST/INFO field')
        amount=struct.unpack_from('<I',data,position+4)[0]
        position+=8+amount
        if position>end:raise ValueError('WAV LIST/INFO field exceeds bounds')
        # Editors may omit the final INFO field's odd-byte padding. Its full
        # declared payload must fit; no next field or audio boundary is guessed.
        if position<end:position+=amount&1

def _parse(data):
    if len(data)<12 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE':raise ValueError('Invalid RIFF WAV header')
    end=8+struct.unpack_from('<I',data,4)[0]
    if end!=len(data):raise ValueError('RIFF WAV physical length mismatch')
    position=12;fmt=None;fmt_extension=b'';body=None;fact=None
    def viable(position):
        if position==end:return True
        if position+8>end:return False
        tag=data[position:position+4]
        return all(32<=byte<=126 for byte in tag) and position+8+struct.unpack_from('<I',data,position+4)[0]<=end
    while position<end:
        if position+8>end:raise ValueError('Truncated WAV chunk')
        kind=data[position:position+4];amount=struct.unpack_from('<I',data,position+4)[0];start=position+8
        next_position=start+amount+(amount&1)
        if kind==b'data' and start+amount==end:next_position=end
        # Some shipped editor chunks omit an odd-byte pad. Admit only the
        # unambiguous physically bounded next header, never guess audio size.
        if amount&1 and not viable(next_position) and viable(start+amount):next_position=start+amount
        if next_position>end:raise ValueError('WAV chunk exceeds physical bounds')
        if kind==b'fmt ':
            if fmt is not None or amount<16:raise ValueError('Invalid/duplicate WAV fmt')
            fmt=struct.unpack_from('<HHIIHH',data,start)
            if amount>16 and (amount<18 or 18+struct.unpack_from('<H',data,start+16)[0]>amount):raise ValueError('Invalid fmt extension')
            fmt_extension=data[start+16:start+min(amount,40)]
        elif kind==b'data':
            if body is not None or fmt is None:raise ValueError('Invalid/duplicate WAV data')
            body=(start,amount)
        elif kind==b'fact':
            if amount<4:raise ValueError('Invalid WAV fact')
            fact=struct.unpack_from('<I',data,start)[0]
        elif kind==b'LIST' and amount>=4 and data[start:start+4]==b'INFO':
            _validate_info(data,start+4,start+amount)
        position=next_position
    if fmt is None or body is None:raise ValueError('WAV missing fmt/data')
    tag,channels,rate,byte_rate,alignment,bits=fmt;offset,size=body;channel_mask=0
    if not channels or not rate or not alignment or not size:raise ValueError('Invalid WAV sample format')
    if tag==0xfffe:
        if len(fmt_extension)<24 or struct.unpack_from('<H',fmt_extension)[0]<22:
            raise ValueError('Invalid WAV extensible fmt: missing 22-byte extension')
        valid_bits,channel_mask=struct.unpack_from('<HI',fmt_extension,2)
        if channel_mask and channel_mask.bit_count()!=channels:raise ValueError('Invalid WAV extensible channel mask')
        subformat=UUID(bytes_le=fmt_extension[8:24])
        tag={UUID('00000001-0000-0010-8000-00aa00389b71'):1,
             UUID('00000003-0000-0010-8000-00aa00389b71'):3}.get(subformat)
        if tag is None:raise UnsupportedWaveFormatError(f'WAV 格式尚未支持：0xFFFE，SubFormat={subformat}')
        # Samples is a union: it describes precision only for PCM/float.
        if not 1<=valid_bits<=bits:raise ValueError('Invalid WAV extensible valid bits')
        if tag==3 and valid_bits!=bits:raise ValueError('Invalid WAV extensible float precision')
        # Integer samples with fewer valid bits are left-aligned in their
        # containers, so their existing PCM codec preserves sample amplitude.
    if tag in (1,3):
        codecs={(1,8):'pcm_u8',(1,16):'pcm_s16le',(1,24):'pcm_s24le',(1,32):'pcm_s32le',(3,32):'pcm_f32le',(3,64):'pcm_f64le'}
        codec=codecs.get((tag,bits))
        if codec is None:raise UnsupportedWaveFormatError(f'WAV 格式尚未支持：PCM/float tag=0x{tag:04X}，bits={bits}')
        width=bits//8
        if alignment!=width*channels or byte_rate!=rate*alignment or size%alignment:raise ValueError('Invalid PCM frame alignment')
        frames=size//alignment
    else:
        codec={2:'adpcm_ms',17:'adpcm_ima_wav',85:'mp3'}.get(tag)
        if codec is None:raise UnsupportedWaveFormatError(f'WAV 格式尚未支持：tag=0x{tag:04X}')
        width=None;frames=fact
    return codec,channels,rate,width,frames,offset,data[offset:offset+size],channel_mask


def _recover_info(data,position):
    """Find a unique INFO layout using only shortened, null-only text fields.

    This walks metadata from the already established audio boundary. It never
    scans for, changes, or infers a data chunk or an audio sample count.
    """
    start=position+8
    amount=struct.unpack_from('<I',data,position+4)[0]
    if amount<4 or data[start:start+4]!=b'INFO':raise ValueError('Unsupported trailing WAV LIST')
    declared_end=start+amount
    pending=[(start+4,0,())];matches=[];visited=0
    while pending:
        cursor,missing,reasons=pending.pop();visited+=1
        # Refuse ambiguous/adversarial metadata rather than guessing a path.
        if visited>1024:raise ValueError('Ambiguous WAV LIST/INFO layout')
        if cursor==declared_end-missing and cursor<=len(data):
            matches.append((cursor,missing,reasons))
            if len(matches)>1:raise ValueError('Ambiguous WAV LIST/INFO layout')
            continue
        if cursor+8>min(len(data),declared_end-missing) or not _info_kind(data[cursor:cursor+4]):continue
        size=struct.unpack_from('<I',data,cursor+4)[0];body=cursor+8
        normal=body+size+(size&1)
        if normal<=min(len(data),declared_end-missing):pending.append((normal,missing,reasons))
        # A shortened empty text field consists only of its surviving NULs.
        # Its next header must begin immediately, and the complete continuation
        # must explain exactly the same number of missing LIST bytes.
        boundary=body
        while boundary<min(body+size,len(data)) and data[boundary]==0:boundary+=1
        successor=data[boundary:boundary+4]
        has_header=boundary+8<=len(data) and (_info_kind(successor) or successor in (b'LIST',b'id3 '))
        if body<boundary<body+size and boundary!=normal and has_header:
            kind=data[cursor:cursor+4].decode('ascii')
            difference=normal-boundary
            detail=f'{kind} 空字符串长度声明 {size}，实际含填充 {boundary-body} 字节'
            pending.append((boundary,missing+difference,reasons+(detail,)))
    if len(matches)!=1:raise ValueError('Unverified WAV LIST/INFO damage')
    return matches[0]


def _recover_container(data):
    if len(data)<12 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE':raise ValueError('Invalid RIFF WAV header')
    # Only a physically bounded, standalone fmt followed by data can establish
    # the safe recovery boundary; unknown/duplicate chunks are never discarded.
    position=12
    if data[position:position+4]!=b'fmt ' or position+8>len(data):raise ValueError('WAV missing fmt')
    fmt_size=struct.unpack_from('<I',data,position+4)[0]
    position+=8+fmt_size+(fmt_size&1)
    if position+8>len(data) or data[position:position+4]!=b'data':raise ValueError('WAV missing bounded data')
    size=struct.unpack_from('<I',data,position+4)[0];audio_end=position+8+size
    if audio_end>len(data):raise ValueError('Truncated WAV data')
    tail=audio_end+(size&1)
    if tail>=len(data):raise ValueError('WAV has no recoverable trailing metadata')
    clean=data[:audio_end]+(b'\0' if size&1 else b'')
    clean=clean[:4]+struct.pack('<I',len(clean)-8)+clean[8:]
    parsed=_parse(clean)
    if parsed[0] not in ('pcm_u8','pcm_s16le','pcm_s24le','pcm_s32le','pcm_f32le','pcm_f64le'):
        raise ValueError('Only complete PCM supports WAV container recovery')
    position=tail;removed=[];missing=0
    while position<len(data):
        if position+8>len(data):raise ValueError('Truncated trailing WAV metadata')
        kind=data[position:position+4];amount=struct.unpack_from('<I',data,position+4)[0]
        if kind==b'LIST':
            end,shortfall,reasons=_recover_info(data,position)
            missing+=shortfall
            reason='；'.join(reasons) or '随损坏尾部元数据一并移除有效 LIST/INFO'
            declared_bytes=8+amount+(amount&1)
            # Shortened fields may also have lost their original alignment pad.
            if not shortfall:end+=(amount&1)
        elif kind==b'id3 ':
            end=position+8+amount+(amount&1);declared_bytes=8+amount+(amount&1)
            header=data[position+8:position+18]
            if amount<10 or len(header)!=10 or header[:3]!=b'ID3' or header[3] not in (2,3,4) or any(byte&128 for byte in header[6:10]):
                raise ValueError('Invalid trailing WAV id3')
            inner_size=0
            for byte in header[6:10]:inner_size=(inner_size<<7)|byte
            footer=10 if header[3]==4 and header[5]&16 else 0
            if 10+inner_size+footer>amount:raise ValueError('Truncated inner WAV id3 tag')
            reason='随损坏 LIST/INFO 一并移除完整 id3 元数据'
        else:raise ValueError('Unsupported trailing WAV chunk during recovery')
        if end>len(data) or end<=position:raise ValueError('Truncated trailing WAV metadata')
        removed.append(RemovedWaveChunk(kind.decode('ascii'),position,declared_bytes,end-position,reason))
        position=end
    declared=8+struct.unpack_from('<I',data,4)[0]
    if not missing or declared-len(data) not in (0,missing):raise ValueError('Unexplained WAV physical length mismatch')
    repair=WaveContainerRepair(hashlib.sha256(data).hexdigest(),declared,len(data),len(data)-tail,tuple(removed),
        'PCM fmt/data 完整且帧对齐；尾部 INFO 空字符串长度失配已由唯一元数据布局验证，内存重封装 fmt/data')
    return clean,parsed,repair


def _prepare_wave(data):
    try:return data,_parse(data),None
    except UnsupportedWaveFormatError:raise
    except ValueError as original:
        try:return _recover_container(data)
        except ValueError:
            raise ValueError('WAV 容器校验失败，无法安全恢复：'+str(original)) from original


def _decode(data,codec,channels,rate,limit,ffmpeg_path,cancelled,*,_short_retry=False,_details=None):
    executable=find_ffmpeg(ffmpeg_path)
    if not executable:raise FileNotFoundError(UNAVAILABLE_MESSAGE)
    raw_format='u8' if codec=='pcm_u8' else 's16le'
    kept=bytearray();total=[0];errors=[];stderr=bytearray()
    with tempfile.TemporaryDirectory(prefix='loka_audio_') as folder:
        source=Path(folder)/'source.wav';source.write_bytes(data)
        command=[executable,'-hide_banner','-v','error','-nostdin','-i',str(source),'-map','0:a:0','-acodec',codec,'-ac',str(channels),'-ar',str(rate),'-f',raw_format,'pipe:1']
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        try:
            process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
        except OSError as error:
            if (isinstance(error,(FileNotFoundError,PermissionError)) or error.errno in (errno.ENOEXEC,errno.ENOENT,errno.EACCES)
                    or getattr(error,'winerror',None) in (126,193,216)):
                raise FileNotFoundError(UNAVAILABLE_MESSAGE) from error
            raise
        def read(handle,output):
            try:
                with handle:
                    while True:
                        chunk=handle.read(65536)
                        if not chunk:break
                        if output:
                            total[0]+=len(chunk);kept.extend(chunk[:max(0,limit-len(kept))])
                        else:
                            stderr.extend(chunk);del stderr[:-65536]
            except BaseException as error:errors.append(error)
        threads=[threading.Thread(target=read,args=(process.stdout,True),daemon=True),threading.Thread(target=read,args=(process.stderr,False),daemon=True)]
        for thread in threads:thread.start()
        try:
            while process.poll() is None:
                _check(cancelled)
                if errors:raise errors[0]
                time.sleep(.02)
            for thread in threads:
                deadline=time.monotonic()+5
                while thread.is_alive():
                    _check(cancelled);thread.join(.02)
                    if time.monotonic()>deadline:raise ValueError('FFmpeg pipe reader did not complete')
            _check(cancelled)
            if errors:raise errors[0]
            if process.returncode:raise ValueError('FFmpeg decoding failed: '+stderr.decode('utf-8',errors='replace'))
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            for thread in threads:thread.join(timeout=2)
    if not total[0]:
        original_codec,original_channels,original_rate,original_width,frames,offset,body,_=_parse(data)
        eligible=(not _short_retry and original_codec in ('pcm_u8','pcm_s16le','pcm_s24le','pcm_s32le')
            and 1<=frames<=32 and original_rate!=rate and original_rate<=4*rate and rate<=4*original_rate)
        if eligible:
            _check(cancelled)
            # Default swresample can flush zero samples for a clip shorter than
            # its filter lookahead. Extend only the private endpoint, retain the
            # normal kernel, then exclude the guard from output duration/counts.
            original_frames=(frames*rate+original_rate-1)//original_rate
            frame_bytes=original_width*original_channels
            guarded_body=body+body[-frame_bytes:]*128
            # Retain fmt (including precision and speaker mask) when extending
            # this private input. Rewriting it as plain PCM would lose layout.
            snapshot=data[:offset-4]+struct.pack('<I',len(guarded_body))+guarded_body+(b'\0' if len(guarded_body)&1 else b'')
            snapshot=snapshot[:4]+struct.pack('<I',len(snapshot)-8)+snapshot[8:]
            needed=original_frames*channels*(1 if codec=='pcm_u8' else 2)
            payload,decoded=_decode(snapshot,codec,channels,rate,min(limit,needed),ffmpeg_path,cancelled,_short_retry=True)
            if decoded<original_frames:raise ValueError('FFmpeg short PCM retry produced insufficient frames')
            if _details is not None:_details['short_pcm_edge']=True
            return payload,original_frames
        raise ValueError('FFmpeg decoding failed: empty output')
    width=1 if codec=='pcm_u8' else 2
    if total[0]%(width*channels):raise ValueError('FFmpeg produced partial PCM frame')
    return bytes(kept),total[0]//(width*channels)


def analyze_wave(source,*,ffmpeg_path='',cancelled=None):
    _check(cancelled);original=_bytes(source);data,parsed,repair=_prepare_wave(original)
    codec,channels,rate,width,frames,offset,_,_=parsed
    if codec not in PCM_WIDTHS:
        _,decoded=_decode(data,'pcm_s16le',channels,rate,0,ffmpeg_path,cancelled)
        if frames is not None and frames!=decoded:raise ValueError('WAV fact/decoded sample count mismatch')
        frames=decoded
    _check(cancelled)
    return AnalyzedWave(data,WaveMetadata(codec,channels,rate,width,frames,offset),hashlib.sha256(original).hexdigest(),repair)


def read_wave_metadata(source,*,ffmpeg_path='',cancelled=None):
    return analyze_wave(source,ffmpeg_path=ffmpeg_path,cancelled=cancelled).metadata


def _write_pcm(body,target):
    fmt=struct.pack('<HHIIHH',1,target.channels,target.rate,target.rate*target.channels*target.width,target.channels*target.width,target.width*8)
    if target.data_offset==46:fmt+=b'\0\0'
    chunks=b'fmt '+struct.pack('<I',len(fmt))+fmt
    missing=target.data_offset-(12+len(chunks)+8)
    if missing:
        if missing<8 or missing%2:raise ValueError('Unsupported official WAV data layout')
        chunks+=b'JUNK'+struct.pack('<I',missing-8)+b'\0'*(missing-8)
    chunks+=b'data'+struct.pack('<I',len(body))+body+(b'\0' if len(body)%2 else b'')
    return b'RIFF'+struct.pack('<I',len(chunks)+4)+b'WAVE'+chunks


def normalize_wave(source,target_metadata,*,ffmpeg_path='',expected_source_sha256=None,cancelled=None):
    _check(cancelled);target=validate_target(target_metadata);data=_bytes(source);digest=hashlib.sha256(data).hexdigest()
    if expected_source_sha256 is not None and digest!=expected_source_sha256:raise ValueError('Source WAV SHA/hash changed')
    data,parsed,repair=_prepare_wave(data)
    codec,channels,rate,width,frames,offset,body,channel_mask=parsed
    source_metadata=(WaveMetadata(codec,channels,rate,width,frames,offset) if codec in PCM_WIDTHS
        else read_wave_metadata(data,ffmpeg_path=ffmpeg_path,cancelled=cancelled))
    size=target.frames*target.channels*target.width
    if size+target.data_offset+(size&1)-8>0xffffffff:raise ValueError('WAV exceeds RIFF32 size bounds')
    # Ordinary stereo is FL+FR. A two-channel extensible stream can instead
    # contain other speaker positions, which require mixing before dropping
    # its channel mask. Mono and unspecified layouts preserve channel order.
    same_layout=channels==1 or channel_mask in (0,3)
    if same_layout and (codec,channels,rate,width)==(target.codec,target.channels,target.rate,target.width):
        method='pcm';converted=frames;body=body[:size]
    else:
        details={};body,converted=_decode(data,target.codec,target.channels,target.rate,size,ffmpeg_path,cancelled,_details=details)
        method='ffmpeg_short_pcm_edge' if details.get('short_pcm_edge') else 'ffmpeg'
    _check(cancelled)
    pad=max(0,target.frames-converted);trim=max(0,converted-target.frames)
    body+=((b'\x80' if target.width==1 else b'\0\0')*target.channels)*pad
    payload=_write_pcm(body,target);actual=read_wave_metadata(payload)
    if (actual.codec,actual.channels,actual.rate,actual.width,actual.frames,actual.data_offset)!=(target.codec,target.channels,target.rate,target.width,target.frames,target.data_offset):raise ValueError('Normalized WAV format/frame/layout verification failed')
    if not isinstance(source,bytes) and hashlib.sha256(_bytes(source)).hexdigest()!=digest:raise ValueError('Source WAV changed during conversion')
    return NormalizedWave(payload,actual,digest,hashlib.sha256(payload).hexdigest(),converted,pad,trim,method,source_metadata,target,repair)
