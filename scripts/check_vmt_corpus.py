"""Read-only VMT checks for supplied VPKs or every VPK in a directory."""
from pathlib import Path
import argparse,json,sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from loka.vpk import VpkArchive
from loka.vmt_normalize import normalize_vmt
from loka.vmt_parser import parse_vmt


def check_packages(inputs):
    packages=set()
    for value in inputs:
        path=Path(value).resolve()
        if path.is_dir():
            packages.update(p.resolve() for p in path.iterdir() if p.is_file() and p.suffix.lower()=='.vpk')
        elif path.is_file() and path.suffix.lower()=='.vpk':packages.add(path)
        else:raise ValueError('请选择现有 VPK 或目录: '+str(path))
    # A selected directory can contain an index and its raw data volumes.
    # Exclude only volumes actually referenced by a successfully read index.
    for index in tuple(packages):
        if not index.name.lower().endswith('_dir.vpk'):continue
        try:archive=VpkArchive(index,include_all=True)
        except (OSError,ValueError):continue
        for number in {entry.archive for entry in archive.entries.values() if entry.archive!=0x7fff}:
            packages.discard(index.with_name(index.name[:-8]+f'_{number:03}.vpk'))
    reports=[]
    for path in sorted(packages):
        report={'path':str(path),'vmt':0,'repaired':0,'errors':[]}
        reports.append(report)
        try:archive=VpkArchive(path,include_all=True)
        except (OSError,ValueError) as error:
            report['errors'].append({'path':str(path),'error':str(error)})
            continue
        for name in archive.entries:
            if not name.lower().endswith('.vmt'):continue
            report['vmt']+=1
            try:
                raw=archive.read(name)
                after,repairs=normalize_vmt(raw,available_resources=archive.entries)
                parse_vmt(after)
                replay=raw
                for repair in repairs:
                    at=repair['offset'];old=repair['old'].encode('ascii');new=repair['new'].encode('ascii')
                    if replay[at:at+len(old)]!=old:raise ValueError('VMT 修复记录无法重放')
                    replay=replay[:at]+new+replay[at+len(old):]
                if replay!=after:raise ValueError('VMT 修复重放结果不一致')
                if normalize_vmt(after)!=(after,[]):raise ValueError('VMT 修复结果不幂等')
                report['repaired']+=bool(repairs)
            except (OSError,ValueError) as error:
                report['errors'].append({'path':name,'error':str(error)})
    return reports


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths',nargs='+',type=Path)
    args=parser.parse_args(argv)
    reports=check_packages(args.paths)
    print(json.dumps(reports,ensure_ascii=True,indent=2))
    return int(any(report['errors'] for report in reports))


if __name__=='__main__':raise SystemExit(main())
