"""Build melee sequence metadata from supplied QC and model reference data."""
import argparse,csv,json,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from loka.weapon_catalog import MELEE_WEAPONS
from loka.weapon_qc import parse_qc,_tokens
from loka.weapon_sequences import _ACTIONS


def melee_semantic(activity,name):
    action=activity.removeprefix('ACT_VM_').removesuffix('_LAYER')
    special={'DRAW':'deploy','PRIMARYATTACK':'attack','SHOOT':'attack',
             'HITCENTER':'attack_center','HITLEFT':'attack_left','HITRIGHT':'attack_right',
             'SWINGHARD':'attack_hard','SECONDARYATTACK':'push','FIDGET':'fidget'}
    if action:return special.get(action,_ACTIONS.get(action,''))
    if name=='ref':return 'reference'
    if 'aim' in name.lower() or 'look_pose' in name.lower():return 'look_poses'
    return 'idle_raw'


def build(reference_dir):
    game=json.loads((reference_dir/'game_sequences.json').read_text(encoding='utf-8'))
    profiles={}
    for weapon in MELEE_WEAPONS:
        model=Path(weapon.view_models[0]).stem
        qc=reference_dir/('v_knife.qc' if weapon.id=='knife' else model+'.qc')
        raw=qc.read_bytes();commands=parse_qc(raw.decode('utf-8-sig'))
        sequences=[c for c in commands if c.kind=='sequence'];official=game[model]
        if [c.name for c in sequences]!=[s['name'] for s in official['sequences']]:
            raise ValueError('QC/game sequence order mismatch: '+weapon.id)
        facts=[]
        for i,(command,sequence) in enumerate(zip(sequences,official['sequences'])):
            tokens=_tokens(command.text)
            activity=next((t.value for t in tokens if t.value.startswith('ACT_VM_')),'')
            if activity!=sequence['activity']:raise ValueError('QC/game Activity mismatch: '+command.name)
            options={t.value.lower() for t in tokens if not t.quoted}
            layer=activity.endswith('_LAYER');semantic=melee_semantic(activity,command.name)
            hidden='hidden' in options
            previous=facts[-1] if facts else None
            unused=(layer and hidden and previous is not None and previous['activity'] in
                    ('ACT_VM_HITCENTER','ACT_VM_HITLEFT','ACT_VM_HITRIGHT'))
            unused=unused or semantic=='reference' or (weapon.id=='knife' and semantic=='fidget')
            animation=sequence['animations'][0];loop='loop' in options
            flexible=loop or semantic in ('idle','idle_raw','fidget')
            frames=None if flexible else animation['frames'];fps=None if flexible else animation['fps']
            facts.append(dict(index=i,name=command.name,activity=activity,semantic=semantic,layer=layer,
                hidden=hidden,loop=loop,unused=unused,end_frame=None if frames is None else frames-1,
                frame_count=frames,fps=fps,flexible=flexible))
        profiles[weapon.id]={'game_model':official['model'],'game_mdl_sha256':official['sha256'],'sequences':facts}
    return profiles


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Update melee sequence metadata from supplied references')
    parser.add_argument('reference',type=Path,help='Directory containing QC files and game_sequences.json')
    parser.add_argument('--output',type=Path,default=ROOT/'loka/data/weapon_sequences.json')
    parser.add_argument('--csv',type=Path,help='Optional CSV reference to update')
    args=parser.parse_args()
    profiles=build(args.reference);output=args.output
    data=json.loads(output.read_text(encoding='utf-8'));data['profiles'].update(profiles)
    data.pop('sources',None)
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.csv is not None:
        with args.csv.open(encoding='utf-8-sig',newline='') as handle:
            reader=csv.DictReader(handle);fields=reader.fieldnames;rows=list(reader)
        profile_field='weapon' if 'weapon' in fields else 'profile'
        rows=[row for row in rows if row[profile_field] not in profiles]
        for identifier,profile in profiles.items():
            for sequence in profile['sequences']:
                rows.append({field:identifier if field==profile_field else sequence.get(field,'') for field in fields})
        with args.csv.open('w',encoding='utf-8',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    print('melee profiles',len(profiles),'slots',sum(len(p['sequences']) for p in profiles.values()))
