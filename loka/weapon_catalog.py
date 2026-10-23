"""Supported firearm and ordinary-melee names and native L4D2 bindings."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Weapon:
    id: str
    name: str
    view_models: tuple[str, ...]
    world_models: tuple[str, ...]
    category: str = 'firearm'


def _weapon(id, name, view, world):
    return Weapon(id, name, tuple('models/v_models/'+p+'.mdl' for p in view.split()),
                  tuple('models/w_models/weapons/'+p+'.mdl' for p in world.split()))


FIREARMS = (
    _weapon('pistol','小手枪(单持+双持)','v_pistola v_dual_pistola v_pistol','w_pistol_a w_pistol_b'),
    _weapon('pistol_magnum','马格南，沙鹰','v_desert_eagle','w_desert_eagle'),
    _weapon('smg','UZI 冲锋枪','v_smg','w_smg_uzi'),
    _weapon('smg_silenced','MAC10 消音冲锋枪','v_silenced_smg','w_smg_a'),
    _weapon('smg_mp5','MP5 冲锋枪','v_smg_mp5','w_smg_mp5'),
    _weapon('pumpshotgun','木喷，一代单喷','v_pumpshotgun','w_shotgun'),
    _weapon('shotgun_chrome','铁喷，二代单喷','v_shotgun_chrome','w_pumpshotgun_a'),
    _weapon('autoshotgun','XM1014，一代连喷','v_autoshotgun','w_autoshot_m4super'),
    _weapon('shotgun_spas','SPAS12，二代连喷','v_shotgun_spas','w_shotgun_spas'),
    _weapon('rifle','M16','v_rifle','w_rifle_m16a2'),
    _weapon('rifle_ak47','AK47','v_rifle_ak47','w_rifle_ak47'),
    _weapon('rifle_desert','SCAR，三连发','v_desert_rifle','w_desert_rifle'),
    _weapon('rifle_sg552','SG552','v_rif_sg552','w_rifle_sg552'),
    _weapon('hunting_rifle','猎枪，15连，一代连狙','v_huntingrifle','w_sniper_mini14'),
    _weapon('sniper_military','30连，二代连狙','v_sniper_military','w_sniper_military'),
    _weapon('sniper_awp','AWP','v_snip_awp','w_sniper_awp'),
    _weapon('sniper_scout','Scout','v_snip_scout','w_sniper_scout'),
    _weapon('rifle_m60','M60','v_m60','w_m60'),
    _weapon('grenade_launcher','榴弹发射器','v_grenade_launcher','w_grenade_launcher'),
)
MELEE_WEAPONS = tuple(Weapon(identifier,name,
    ('models/weapons/melee/v_'+model+'.mdl',),('models/weapons/melee/w_'+model+'.mdl',),'melee')
    for identifier,name,model in (
        ('baseball_bat','棒球棍','bat'),('cricket_bat','板球拍','cricket_bat'),
        ('crowbar','撬棍','crowbar'),('electric_guitar','电吉他','electric_guitar'),
        ('fireaxe','消防斧','fireaxe'),('frying_pan','平底锅','frying_pan'),
        ('golfclub','高尔夫球杆','golfclub'),('katana','武士刀','katana'),
        ('machete','砍刀','machete'),('pitchfork','草叉','pitchfork'),
        ('shovel','铁铲','shovel'),('tonfa','警棍','tonfa')))
MELEE_WEAPONS += (Weapon('knife','小刀',('models/v_models/v_knife_t.mdl',),
    ('models/w_models/weapons/w_knife_t.mdl',),'melee'),)
MELEE_IDS = frozenset(w.id for w in MELEE_WEAPONS)
WEAPONS = FIREARMS + MELEE_WEAPONS
WEAPONS_BY_ID = {w.id: w for w in WEAPONS}
TARGET_WEAPONS = (_weapon('pistol_single','小手枪(单持)','v_pistola','w_pistol_a w_pistol_b'),
                  _weapon('pistol_dual','小手枪(双持)','v_dual_pistola','w_pistol_b')) + WEAPONS
TARGETS_BY_ID = {w.id:w for w in TARGET_WEAPONS}
SHOTGUN_IDS = frozenset(('pumpshotgun','shotgun_chrome','autoshotgun','shotgun_spas'))


def allowed_targets(source_id: str) -> tuple[Weapon, ...]:
    source=get_weapon(source_id)
    source_family=target_family_id(source_id)
    return tuple(w for w in TARGET_WEAPONS if target_family_id(w.id)!=source_family
                 and w.category==source.category
                 and (source_family in SHOTGUN_IDS or target_family_id(w.id) not in SHOTGUN_IDS))


def validate_target(source_id: str, target_id: str) -> None:
    get_weapon(target_id)
    if not any(w.id==target_id for w in allowed_targets(source_id)):
        raise ValueError('目标不能与来源相同；枪械和近战分别互转；非霰弹枪不能转为霰弹枪')


def get_weapon(id: str) -> Weapon:
    try:
        return TARGETS_BY_ID[id]
    except KeyError as error:
        raise ValueError(f'不支持的目标武器: {id}') from error


def target_family_id(target_id: str) -> str:
    get_weapon(target_id)
    return 'pistol' if target_id in ('pistol_single','pistol_dual') else target_id


def target_profiles_for(target_id: str) -> tuple[str,...]:
    get_weapon(target_id)
    if target_id=='pistol_single':return ('pistol',)
    if target_id=='pistol_dual':return ('dual_pistol',)
    if target_id=='pistol':return ('pistol','dual_pistol')
    return (target_id,)
