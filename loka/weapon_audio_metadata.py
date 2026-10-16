"""Immutable built-in official WAV facts; runtime never reads game WAVs."""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import json
from .vpk import safe_archive_path

PCM_WIDTHS={'pcm_u8':1,'pcm_s16le':2,'pcm_s24le':3,'pcm_s32le':4,'pcm_f32le':4,'pcm_f64le':8}

@dataclass(frozen=True)
class WaveMetadata:
    codec:str
    channels:int
    rate:int
    width:int|None
    frames:int
    data_offset:int
    provenance:tuple[tuple[str,str],...]=()

    def __post_init__(self):
        if self.codec not in PCM_WIDTHS and self.codec not in ('adpcm_ms','adpcm_ima_wav','mp3'):
            raise ValueError('Unsupported WAV codec: '+self.codec)
        if any(type(v) is not int or v<=0 for v in (self.channels,self.rate,self.frames,self.data_offset)):
            raise ValueError('Invalid WAV sample metadata')
        if self.codec in PCM_WIDTHS and self.width!=PCM_WIDTHS[self.codec]:raise ValueError('WAV codec/sample width mismatch')
        if self.channels>65535 or self.rate>0xffffffff:raise ValueError('WAV format fields exceed RIFF bounds')

    @property
    def duration(self):return Fraction(self.frames,self.rate)


def validate_target(metadata):
    if not isinstance(metadata,WaveMetadata) or metadata.codec not in ('pcm_u8','pcm_s16le') or metadata.channels not in (1,2):
        raise ValueError('Unsupported official WAV reference format')
    return metadata

def target_wave_metadata(path,*,records=None):
    key=safe_archive_path(path.replace('\\','/')).lower()
    if records is None:records=load_target_metadata()
    if key in records:return validate_target(records[key])
    raise ValueError('内置官方音频元数据缺少条目: '+key)


def load_target_metadata(*,paths=None):
    file=Path(__file__).parent/'data/weapon_audio_metadata.json'
    data=json.loads(file.read_text(encoding='utf-8'))
    if data['schema_version']!=1:
        raise ValueError('Official audio metadata version mismatch')
    result={}
    for key,item in data['records'].items():
        if safe_archive_path(key)!=key or key!=key.lower():raise ValueError('Invalid official metadata path')
        result[key]=validate_target(WaveMetadata(item['codec'],item['channels'],item['rate'],item['width'],item['frames'],item['data_offset'],tuple(sorted(item['provenance'].items()))))
    if paths is not None:return {p:target_wave_metadata(p,records=result) for p in sorted(set(paths))}
    return result
