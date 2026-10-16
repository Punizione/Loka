"""Generate official audio metadata directly from installed game WAV files."""
from pathlib import Path
import argparse,hashlib,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from loka.weapon_audio import sound_reference
from loka.weapon_audio_metadata import validate_target
from loka.vpk import VpkArchive,safe_archive_path
from loka.vmodel import game_search_roots
from loka.wav_normalize import read_wave_metadata

PACKAGES=('update','left4dead2_dlc3','left4dead2_dlc2','left4dead2_dlc1','left4dead2','hl2')


def stock_wave_bytes(path,game_path,*,archives=None):
    key=safe_archive_path(path.replace('\\','/')).lower()
    if not key.startswith('sound/') or not key.endswith('.wav'):raise ValueError('Invalid official sound path')
    archives={} if archives is None else archives
    packages={'update','left4dead2_dlc3','left4dead2_dlc2','left4dead2_dlc1','left4dead2','hl2'}
    for root in game_search_roots(game_path):
        if root.name.lower() not in packages:continue
        loose=root/key
        if loose.is_file():return loose.read_bytes(),str(loose)
        for file in sorted(root.glob('*_dir.vpk')):
            archive=archives.setdefault(file,None)
            if archive is None:archive=archives[file]=VpkArchive(file,include_all=True)
            payload=archive.read(key)
            if payload is not None:return payload,str(file)+'!'+key
    raise ValueError('Missing official stock WAV reference: '+key)


def portable_provider(provider):
    parts=provider.replace('\\','/').split('/')
    indexes=[i for i,part in enumerate(parts) if part.lower() in PACKAGES]
    if not indexes:raise ValueError('Official WAV provider has no game package')
    start=indexes[-1]
    return '/'.join(parts[start:]),parts[start].lower()


def build_subset(game_path):
    needed={p for record in sound_reference().events.values() for p in record.paths}
    archives={};records={}
    for path in sorted(needed):
        raw,provider=stock_wave_bytes(path,game_path,archives=archives)
        stock=validate_target(read_wave_metadata(raw))
        provider,package=portable_provider(provider)
        provenance={'stock_provider':provider,'stock_sha256':hashlib.sha256(raw).hexdigest(),'package':package}
        records[path]=dict(codec=stock.codec,channels=stock.channels,rate=stock.rate,width=stock.width,
                           frames=stock.frames,data_offset=stock.data_offset,provenance=provenance)
    return {'schema_version':1,'records':records}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--game',required=True)
    parser.add_argument('--output',default='loka/data/weapon_audio_metadata.json')
    args=parser.parse_args()
    result=build_subset(args.game)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
    print('Generated',len(result['records']),'records')
