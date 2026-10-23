"""L4D2 viewmodel roles and lossless packaging of included model dependencies."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from .core.mdl_data import SourceMdlFileData49
from .core.mdl_file import SourceMdlFile49
from .core.reader import BinaryReader
from .vpk import VpkArchive

# Standard L4D2 viewmodel paths.
# Animation libraries (anim_v_*, anims_v_*) are intentionally not engine entrypoints.
VIEWMODEL_PATHS = frozenset("""
v_models/v_adrenaline.mdl v_models/v_autoshotgun.mdl v_models/v_bile_flask.mdl
v_models/v_cola.mdl v_models/v_defibrillator.mdl v_models/v_desert_eagle.mdl
v_models/v_desert_rifle.mdl v_models/v_dual_pistola.mdl v_models/v_explosive_ammopack.mdl
v_models/v_grenade_launcher.mdl v_models/v_huntingrifle.mdl v_models/v_incendiary_ammopack.mdl
v_models/v_knife_t.mdl v_models/v_m60.mdl v_models/v_medkit.mdl v_models/v_molotov.mdl
v_models/v_painpills.mdl v_models/v_pipebomb.mdl v_models/v_pistol.mdl v_models/v_pistola.mdl
v_models/v_pumpshotgun.mdl v_models/v_rif_sg552.mdl v_models/v_rifle.mdl
v_models/v_rifle_ak47.mdl v_models/v_shotgun_chrome.mdl v_models/v_shotgun_spas.mdl
v_models/v_silenced_smg.mdl v_models/v_smg.mdl v_models/v_smg_mp5.mdl
v_models/v_snip_awp.mdl v_models/v_snip_scout.mdl v_models/v_sniper_military.mdl
v_models/weapons/v_claw_boomer.mdl v_models/weapons/v_claw_boomer_l4d1.mdl
v_models/weapons/v_claw_hulk.mdl v_models/weapons/v_claw_hulk_dlc3.mdl
v_models/weapons/v_claw_hulk_l4d1.mdl v_models/weapons/v_claw_hunter.mdl
v_models/weapons/v_claw_hunter_l4d1.mdl v_models/weapons/v_claw_smoker.mdl
v_models/weapons/v_claw_smoker_l4d1.mdl weapons/arms/v_arms_bill.mdl
weapons/arms/v_arms_coach_new.mdl weapons/arms/v_arms_francis.mdl
weapons/arms/v_arms_gambler_new.mdl weapons/arms/v_arms_louis.mdl
weapons/arms/v_arms_mechanic_new.mdl weapons/arms/v_arms_producer_new.mdl
weapons/arms/v_arms_zoey.mdl weapons/arms/v_charger_arms.mdl
weapons/arms/v_jockey_arms.mdl weapons/arms/v_spitter_arms.mdl
weapons/melee/v_bat.mdl weapons/melee/v_chainsaw.mdl weapons/melee/v_cricket_bat.mdl
weapons/melee/v_crowbar.mdl weapons/melee/v_electric_guitar.mdl weapons/melee/v_fireaxe.mdl
weapons/melee/v_frying_pan.mdl weapons/melee/v_gnome.mdl weapons/melee/v_golfclub.mdl
weapons/melee/v_katana.mdl weapons/melee/v_machete.mdl weapons/melee/v_pitchfork.mdl
weapons/melee/v_riotshield.mdl weapons/melee/v_shovel.mdl weapons/melee/v_tonfa.mdl
""".split())
_MAIN_NAMES = frozenset(PurePosixPath(p).name for p in VIEWMODEL_PATHS)
_COMPANIONS = (".ani", ".vvd", ".phy", ".dx11.vtx", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx")


def model_relative_path(value: str) -> str:
    """Return a safe model-root-relative path, retaining its spelling for QC."""
    value = value.replace("\\", "/")
    if value.lower().startswith("models/"):
        value = value[7:]
    parts = value.split("/")
    if (not value or any(p in ("", ".", "..") or ":" in p or
                         any(c in p for c in '\x00\r\n"<>|?*') or p.endswith((".", " "))
                         for p in parts)):
        raise ValueError(f"无效的模型引用路径: {value!r}")
    return "/".join(parts)


def read_reference_data(raw: bytes) -> SourceMdlFileData49:
    """Read only header and include table; never decode dependency geometry."""
    if raw[:4] != b"IDST" or int.from_bytes(raw[4:8], "little") not in (44, 45, 46, 47, 48, 49, 54, 55, 56, 58, 59):
        raise ValueError("include 依赖不是受支持的 Source MDL")
    data = SourceMdlFileData49()
    parser = SourceMdlFile49(BinaryReader(raw), data)
    try:
        parser.read_mdl_header00("dependency header")
        parser.read_mdl_header01("dependency header 01")
        parser.read_model_groups()
    except Exception as error:
        raise ValueError(f"include 依赖的 MDL 数据不完整: {error}") from error
    if len(data.theModelGroups or []) != data.includeModelCount:
        raise ValueError("include 依赖的模型引用表不完整")
    return data


def _models_root(path: Path) -> Path | None:
    return next((p for p in path.parents if p.name.lower() == "models"), None)


def _is_referenced_library(path: Path) -> bool:
    folders = [path.parent]
    if path.parent.name.lower() == "source":
        folders.append(path.parent.parent)
    root = _models_root(path)
    for folder in folders:
        for sibling in folder.glob("*.mdl"):
            if sibling.name.lower() not in _MAIN_NAMES:
                continue
            try:
                data = read_reference_data(sibling.read_bytes())
                for group in data.theModelGroups or []:
                    ref = model_relative_path(group.theFileName)
                    target = root / ref if root else folder / PurePosixPath(ref).name
                    if target.resolve() == path.resolve() or (
                            path.parent.name.lower() == "source" and target.name.lower() == path.name.lower()):
                        return True
            except Exception:
                # A damaged unrelated sibling must not break a dragged model.
                continue
    return False


def classify_model(mdl_path: str, data: SourceMdlFileData49) -> str:
    path = Path(mdl_path)
    if path.name.lower() in _MAIN_NAMES:
        return "viewmodel"
    try:
        declared = model_relative_path(data.theModelName).lower()
    except ValueError:
        return "other"
    in_scope = declared.startswith(("v_models/", "weapons/melee/", "weapons/arms/"))
    if not in_scope:
        return "other"
    if data.localAnimationCount <= 0 or data.localSequenceCount <= 0:
        return "other"
    has_geometry = path.with_suffix(".vvd").is_file() and any(
        path.with_suffix(s).is_file() for s in _COMPANIONS if s.endswith(".vtx"))
    if (_is_referenced_library(path) or data.theMdlFileOnlyHasAnimations or
            (declared in VIEWMODEL_PATHS and not has_geometry)):
        return "animation_library"
    return "other"


def prepare_animation_library(mdl_path: str, data: SourceMdlFileData49) -> None:
    path = Path(mdl_path)
    root = _models_root(path)
    if root:
        actual = path.parent.parent / path.name if path.parent.name.lower() == "source" else path
        target = actual.relative_to(root).as_posix()
    else:
        declared = model_relative_path(data.theModelName)
        target = str(PurePosixPath(declared).parent / path.name)
    data.theModelName = model_relative_path(target)
    data.theMdlFileOnlyHasAnimations = True
    # Keep source animation/bone tables; stale render metadata must not request VVD/VTX.
    data.theBodyParts = []
    data.bodyPartCount = 0
    data.theTexturePaths = []
    data.theTextures = []
    data.theFlexDescs = []
    data.theFlexControllers = []
    data.theFlexRules = []


def prepare_viewmodel(mdl_path: str, data: SourceMdlFileData49) -> None:
    """Use the actual engine filename when a mod retains a stale internal name."""
    name = Path(mdl_path).name.lower()
    target = next(path for path in VIEWMODEL_PATHS if PurePosixPath(path).name == name)
    try:
        declared = model_relative_path(data.theModelName).lower()
    except ValueError:
        declared = ""
    if declared != target:
        data.theModelName = target


def game_search_roots(game_path: str) -> list[Path]:
    if not game_path:
        return []
    path = Path(game_path).resolve()
    if (path / "gameinfo.txt").is_file():
        base = path.parent
    else:
        base = path
        path = base / "left4dead2"
    roots = [base / name for name in
             ("update", "left4dead2_dlc3", "left4dead2_dlc2", "left4dead2_dlc1", "left4dead2", "hl2")]
    if path.name.lower() != "left4dead2":
        roots.insert(0, path)
    return list(dict.fromkeys(roots))


def copy_include_dependencies(mdl_path: str, data: SourceMdlFileData49,
                              output_path: str, game_path: str = "", *, validate_only: bool = False) -> list[str]:
    """Preflight the complete include graph before copying any file."""
    source = Path(mdl_path).resolve()
    local_root = _models_root(source)
    pending: dict[str, tuple[str, bytes]] = {}
    visiting = set()
    source_files: set[Path] = {source}
    archives: dict[Path, VpkArchive] = {}

    def locate(rel: str, provider: tuple[str, Path] | None = None):
        if provider:
            kind, origin = provider
            if kind == "vpk":
                raw = archives[origin].read("models/" + rel)
                return (raw, provider) if raw is not None else None
            candidates = [origin / PurePosixPath(rel).name]
        else:
            candidates = []
            if local_root:
                candidates.append(local_root / rel)
            else:
            # Local model files take precedence over installed game content.
                declared_parent = PurePosixPath(model_relative_path(data.theModelName)).parent
                if str(PurePosixPath(rel).parent).lower() == str(declared_parent).lower():
                    candidates.append(source.parent / PurePosixPath(rel).name)
                    if source.parent.name.lower() == "source":
                        candidates.append(source.parent.parent / PurePosixPath(rel).name)
        for candidate in candidates:
            if candidate.is_file():
                source_files.add(candidate.resolve())
                return candidate.read_bytes(), ("loose", candidate.parent)
        if provider:
            return None
        for root in game_search_roots(game_path):
            candidate = root / "models" / rel
            if candidate.is_file():
                source_files.add(candidate.resolve())
                return candidate.read_bytes(), ("loose", candidate.parent)
            for vpk in sorted(root.glob("*_dir.vpk")):
                if vpk not in archives:
                    archives[vpk] = VpkArchive(vpk)
                raw = archives[vpk].read("models/" + rel)
                if raw is not None:
                    return raw, ("vpk", vpk)
        return None

    def visit(ref: str) -> None:
        rel = model_relative_path(ref)
        if not rel.lower().endswith(".mdl"):
            raise ValueError(f"include 引用不是 .mdl: {rel}")
        key = rel.lower()
        if key in visiting:
            return
        located = locate(rel)
        if located is None:
            raise FileNotFoundError(f"未找到 include 依赖 models/{rel}；请配置游戏路径或提供对应模型文件")
        raw, provider = located
        visiting.add(key)
        pending[key] = (rel, raw)
        dependency = read_reference_data(raw)
        from .core.bone_names import validate_binary_include_bones
        validate_binary_include_bones(data, raw)
        for suffix in _COMPANIONS:
            companion = str(PurePosixPath(rel).with_suffix(suffix))
            companion_source = locate(companion, provider)
            if companion_source is not None:
                pending[companion.lower()] = (companion, companion_source[0])
            elif suffix == ".ani" and dependency.animBlockCount > 0:
                raise FileNotFoundError(f"include 动画库缺少同版本的 ANI 文件: models/{companion}")
        for group in dependency.theModelGroups or []:
            visit(group.theFileName)

    for group in data.theModelGroups or []:
        visit(group.theFileName)
    if validate_only:
        return []
    output = Path(output_path).resolve() / "models"
    for rel, raw in pending.values():
        destination = output / rel
        if not destination.resolve().is_relative_to(output):
            raise ValueError(f"依赖输出路径越界: {rel}")
        if destination.resolve() in source_files:
            raise ValueError(f"依赖输出会覆盖输入文件，请选择其他输出目录: {destination}")
        if destination.exists() and destination.read_bytes() != raw:
            raise FileExistsError(f"输出目录已有不同的 include 依赖: {destination}")
    written = []
    for rel, raw in pending.values():
        destination = output / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        written.append(str(destination))
    return written
