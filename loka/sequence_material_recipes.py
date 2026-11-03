"""Built-in sequence-material recipes; filenames are explicit lookup data.

The v2 reference's appendix A takes precedence over its abbreviated manifest:
lam uses 11 frames on all layers; box printsync36 contains 35 frames.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutputRule:
    part: str
    source_name: str
    output_name: str
    layer: str
    size: int
    frames: int
    variant: str = ''

    @property
    def image_format(self) -> int:
        return 13 if self.layer == 'glow' else 15

    @property
    def flags(self) -> int:
        if self.part == 'box' and self.layer == 'glow':
            return 0x0010  # authoritative props_collect_treasure_702_e_printsync36
        return {'print': 0x2052, 'normal': 0x2092,
                'glow': 0x0052, 'soliddepth': 0x2000}[self.layer]

    @property
    def layer_label(self) -> str:
        label = {'print': '基色', 'normal': '法线',
                 'glow': '辉光', 'soliddepth': '实体化'}[self.layer]
        return f'{label}（{self.variant}）' if self.variant else label


@dataclass(frozen=True)
class MaterialRecipe:
    identifier: str
    name: str
    rules: tuple[OutputRule, ...]


_LAM = 'dev_lam_peq25_005_mechrevodf_vk'
_WEAL = 'dev_weal_strobelight_006_mechrevodf_vk'
_FROS = 'fros_am17_045_mechrevodf_vk'
_HAN = 'han_am17_long_160_mechrevodf_vk'
_MUZ = 'muz_ak_pbs_04_012_mechrevodf_vk'
_REC = 'rec_smg_am17_066_mechrevodf_vk_01'
_REUSE = 'rec_smg_am17_066_mechrevodf_vk_01_reuse'
_REC_GLOW = 'rec_smg_am17_066_mechrevodf_vk_specail_bodynight'


def _r(part, source, output, layer, size, frames, *, variant=''):
    return OutputRule(part, source + '.vtf', output + '.vtf', layer,
                      size, frames, variant)


KC14 = MaterialRecipe('kc14-jiuci', 'KC17纪元造物·不会贴皮の久辞', (
    _r('lam', _LAM+'_d2', _LAM+'_print_softframe_colorfront', 'print', 512, 11),
    _r('lam', _LAM+'_d2_red', _LAM+'_print_softframe_colorfront_red', 'print', 512, 11),
    _r('lam', _LAM+'_n', _LAM+'_normal_clearoutline', 'normal', 512, 11),
    _r('lam', _LAM+'_specail_bodynight', _LAM+'_glow_colorfront', 'glow', 512, 11),
    _r('weal', _WEAL+'_d2', _WEAL+'_print13_allparts', 'print', 512, 13),
    _r('weal', _WEAL+'_d2_red', _WEAL+'_print13_allparts_red', 'print', 512, 13),
    _r('weal', _WEAL+'_n', _WEAL+'_normal13_allparts', 'normal', 512, 13),
    _r('weal', _WEAL+'_specail_bodynight', _WEAL+'_glow13_allparts', 'glow', 512, 13),
    _r('fros', _FROS+'_d2', _FROS+'_print_softframe_colorfront', 'print', 512, 7),
    _r('fros', _FROS+'_d2_red', _FROS+'_print_softframe_colorfront_red', 'print', 512, 7),
    _r('fros', _FROS+'_n', _FROS+'_normal_clearoutline', 'normal', 512, 7),
    _r('han', _HAN+'_d2', _HAN+'_print_softframe_colorfront', 'print', 512, 36),
    _r('han', _HAN+'_d2_red', _HAN+'_print_softframe_colorfront_red', 'print', 512, 36),
    _r('han', _HAN+'_n', _HAN+'_normal36_recstyle', 'normal', 256, 36),
    _r('han', _HAN+'_specail_bodynight', _HAN+'_glow_colorfront', 'glow', 256, 36),
    _r('muz', _MUZ+'_d2', _MUZ+'_print_softframe_colorfront', 'print', 256, 36),
    _r('muz', _MUZ+'_d2_red', _MUZ+'_print_softframe_colorfront_red', 'print', 256, 36),
    _r('muz', _MUZ+'_n', _MUZ+'_normal36_recstyle', 'normal', 256, 36),
    _r('muz', _MUZ+'_specail_bodynight', _MUZ+'_glow_colorfront', 'glow', 256, 36),
    _r('rec', _REC+'_d2', _REC+'_print36_silksync', 'print', 512, 36, variant='silksync'),
    _r('rec', _REC+'_d2_red', _REC+'_print36_silksync_red', 'print', 512, 36, variant='silksync'),
    _r('rec', _REC+'_d2', _REC+'_print36_smoothwhite', 'print', 512, 36, variant='smoothwhite'),
    _r('rec', _REC+'_d2_red', _REC+'_print36_smoothwhite_red', 'print', 512, 36, variant='smoothwhite'),
    _r('rec', _REC+'_n', _REC+'_normal36_silksync', 'normal', 256, 36, variant='silksync'),
    _r('rec', _REC+'_n', _REC+'_normal36_smoothwhite', 'normal', 256, 36, variant='smoothwhite'),
    _r('rec', _REC_GLOW, _REC+'_glow36_silksync', 'glow', 512, 36, variant='silksync'),
    _r('rec', _REC_GLOW, _REC+'_glow36_smoothwhite', 'glow', 512, 36, variant='smoothwhite'),
    _r('rec_reuse', _REUSE+'_d2', _REUSE+'_print36_silksync', 'print', 512, 36, variant='silksync'),
    _r('rec_reuse', _REUSE+'_d2_red', _REUSE+'_print36_silksync_red', 'print', 512, 36, variant='silksync'),
    _r('rec_reuse', _REUSE+'_d2', _REUSE+'_print36_smoothwhite', 'print', 512, 36, variant='smoothwhite'),
    _r('rec_reuse', _REUSE+'_d2_red', _REUSE+'_print36_smoothwhite_red', 'print', 512, 36, variant='smoothwhite'),
    _r('rec_reuse', _REC+'_n', _REUSE+'_normal36_silksync', 'normal', 256, 36, variant='silksync'),
    _r('rec_reuse', _REC+'_n', _REUSE+'_normal36_smoothwhite', 'normal', 256, 36, variant='smoothwhite'),
    _r('rec_reuse', _REC_GLOW, _REUSE+'_glow36_silksync', 'glow', 512, 36, variant='silksync'),
    _r('rec_reuse', _REC_GLOW, _REUSE+'_glow36_smoothwhite', 'glow', 512, 36, variant='smoothwhite'),
    _r('box', 'props_collect_treasure_702_d2', 'mi_props_box_am17_mechrevodf_vk_1p_soliddepth', 'soliddepth', 256, 35),
    _r('box', 'props_collect_treasure_702_e', 'props_collect_treasure_702_e_printsync36', 'glow', 256, 35),
))

BUILTIN_RECIPES = (KC14,)


def get_recipe(identifier: str) -> MaterialRecipe:
    for recipe in BUILTIN_RECIPES:
        if recipe.identifier == identifier:
            return recipe
    raise ValueError('未知的序列帧材质配方：' + str(identifier))
