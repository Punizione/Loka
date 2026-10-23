"""Extract user-supplied XLSX sound facts; no third-party runtime dependency."""
import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
PREFIXES={
    'pistol':['Pistol','Pistol_Silver'],'dual_pistol':['DualPistol'],
    'pistol_magnum':['Magnum'],'smg':['SMG'],'smg_silenced':['SMG_Silenced'],
    'smg_mp5':['Weapon_MP5Navy'],'pumpshotgun':['Shotgun'],'shotgun_chrome':['Shotgun_Chrome'],
    'autoshotgun':['AutoShotgun'],'shotgun_spas':['AutoShotgun_Spas','AutoShotgunSpas'],
    'rifle':['Rifle'],'rifle_ak47':['AK47'],'rifle_desert':['Rifle_Desert'],
    'rifle_sg552':['Weapon_SG552'],'hunting_rifle':['HuntingRifle'],
    'sniper_military':['Sniper_Military','Sniper__Military'],'sniper_awp':['Weapon_AWP'],
    'sniper_scout':['Weapon_Scout'],'rifle_m60':['M60'],'grenade_launcher':['GrenadeLauncher'],
    'baseball_bat':['Bat'],'cricket_bat':['CricketBat'],'crowbar':['Crowbar'],
    'electric_guitar':['Guitar'],'fireaxe':['Axe'],'frying_pan':['Pan'],
    'golfclub':['GolfClub'],'katana':['Katana'],'knife':['Weapon_Knife','Knife'],
    'machete':['Machete'],'pitchfork':['Pitchfork'],'shovel':['Shovel'],'tonfa':['Tonfa'],
}
OWNERS={p.lower():key for key,aliases in PREFIXES.items() for p in aliases}

# Normalize event ownership against the game's sound definitions.
EVENT_PATH_CORRECTIONS={
    ('shovel.miss',f'sound/weapons/shovel/shovel_impact_flesh{i}.wav'):'Shovel.ImpactFlesh'
    for i in (2,3,4)
}


def sheets(source):
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    rns='{http://schemas.openxmlformats.org/package/2006/relationships}'
    rid='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    with zipfile.ZipFile(source) as archive:
        strings=[''.join(si.itertext()) for si in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si',ns)]
        relationships={r.attrib['Id']:r.attrib['Target'].lstrip('/') for r in
                       ET.fromstring(archive.read('xl/_rels/workbook.xml.rels')).findall(rns+'Relationship')}
        result={}
        for sheet in ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet',ns):
            target=relationships[sheet.attrib[rid]]
            if not target.startswith('xl/'):target='xl/'+target
            rows=[]
            for row in ET.fromstring(archive.read(target)).findall('s:sheetData/s:row',ns):
                values={}
                for cell in row.findall('s:c',ns):
                    value=cell.find('s:v',ns)
                    if value is not None:
                        column=''.join(c for c in cell.attrib['r'] if c.isalpha())
                        values[column]=strings[int(value.text)] if cell.attrib.get('t')=='s' else value.text
                if values:rows.append((int(row.attrib['r']),values))
            result[sheet.attrib['name']]=rows
        return result


def build(source):
    tables=sheets(source);events={};path_owners={}
    for row,values in tables['All Sounds']:
        name=values.get('A','').strip();raw=values.get('C','').strip()
        if '.' not in name or not raw.lower().endswith('.wav'):continue
        path=raw.replace('\\','/').lower()
        if not path.startswith('sound/'):path='sound/'+path
        name=EVENT_PATH_CORRECTIONS.get((name.lower(),path),name)
        prefix,action=name.split('.',1)
        owner=OWNERS.get(prefix.lower())
        if name.lower()=='pistol.dualfire':owner='dual_pistol';action='fire'
        elif action.lower()=='single' and prefix.lower().startswith('weapon_'):action='fire'
        key=name.lower()
        item=events.setdefault(key,{'name':name,'owner':owner,'action':action.lower(),'paths':[],'rows':[]})
        if item['owner']!=owner or item['action']!=action.lower():raise ValueError('sound row conflict: '+name)
        if path not in item['paths']:item['paths'].append(path)
        item['rows'].append(row)
        family=owner or 'other:'+prefix.lower()
        path_owners.setdefault(path,set()).add(family)
    profiles={}
    for owner,aliases in PREFIXES.items():
        candidates=[e for e in events.values() if e['owner']==owner]
        candidates.sort(key=lambda e:(aliases.index(e['name'].split('.')[0]) if e['name'].split('.')[0] in aliases else len(aliases),
                                      e['name'].lower()))
        actions={}
        for event in candidates:actions.setdefault(event['action'],event['name'].lower())
        if not actions:raise ValueError('missing sound profile: '+owner)
        profiles[owner]=actions
    for event in events.values():event.pop('rows',None)
    return {'schema_version':1,'profiles':profiles,'events':events,
            'shared_paths':sorted(p for p,owners in path_owners.items() if len(owners)>1)}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Generate sound profiles from a supplied workbook')
    parser.add_argument('input',type=Path)
    args=parser.parse_args()
    data=build(args.input)
    output=ROOT/'loka/data/weapon_sounds.json'
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('profiles',len(data['profiles']),'events',len(data['events']),'shared paths',len(data['shared_paths']),
          'bytes',output.stat().st_size)
