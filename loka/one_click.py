"""One-click pipeline: backup originals -> decompile -> compile with studiomdl.

The "一键处理" feature chains, for a single ``.mdl``:

1. the original model and its companion files (``.vvd``/``.vtx``/``.ani``/
   ``.phy``/...) are moved into ``<mdl dir>/source`` as a backup;
2. the model is decompiled (from the backup location) into ``<mdl dir>/dec``
   -- the caller is expected to enable the subtract-correction and
   bodygroup-events features on the passed :class:`DecompileOptions`;
3. ``studiomdl.exe`` compiles the generated ``.qc``; the compiled model files
   are moved from the game's ``models`` folder back next to the original
   model's location;
4. if any step after the backup fails, the backup is moved back so the model
   directory is left in its original state.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .decompiler import DecompileOptions, MdlDecompiler, DecompileResult
from .compile_game import prepare_compile_game
from .process_limits import exception_text

__all__ = [
    "OneClickResult",
    "find_studiomdl",
    "find_game_dir",
    "run_one_click",
]


@dataclass
class OneClickResult:
    """Outcome of one :func:`run_one_click` call."""

    mdl_path: str = ""
    succeeded: bool = False
    decompile_result: Optional[DecompileResult] = None
    dec_dir: str = ""
    source_dir: str = ""
    compiled_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _find_file_up_and_down(root: str, file_name: str, max_depth: int = 3) -> Optional[str]:
    """Depth-limited search for ``file_name`` under ``root``."""
    root = os.path.abspath(root)
    for current, dirs, files in os.walk(root):
        depth = os.path.relpath(current, root).count(os.sep)
        if depth >= max_depth:
            dirs[:] = []
        if file_name in [f.lower() for f in files]:
            return os.path.join(current, file_name)
    return None


_PREFERRED_GAME_DIRS = ("left4dead2", "left4dead", "hl2", "tf2", "cstrike")


def find_game_dir(game_path: str) -> Optional[str]:
    """Locate the ``-game`` folder (the one holding ``gameinfo.txt``).

    Preferred engine folders (left4dead2, hl2, ...) win over arbitrary ones so
    a game root containing several mod folders picks the usual suspect.
    """
    game_path = os.path.abspath(game_path)
    if os.path.isfile(os.path.join(game_path, "gameinfo.txt")):
        return game_path

    hits: list[str] = []
    try:
        entries = sorted(os.listdir(game_path))
    except OSError:
        return None
    for name in entries:
        candidate = os.path.join(game_path, name)
        if os.path.isfile(os.path.join(candidate, "gameinfo.txt")):
            hits.append(candidate)
    for preferred in _PREFERRED_GAME_DIRS:
        for hit in hits:
            if os.path.basename(hit).lower() == preferred:
                return hit
    return hits[0] if hits else None


def find_studiomdl(game_path: str) -> tuple[Optional[str], Optional[str]]:
    """Locate ``studiomdl.exe`` and the ``-game`` folder from a game path.

    ``game_path`` may also be a direct path to a ``studiomdl.exe`` (any
    third-party build); the game folder is then derived from its location.
    Returns ``(exe_path, game_dir)``, either of which may be ``None``.
    """
    game_path = os.path.abspath(game_path)
    if os.path.isfile(game_path) and game_path.lower().endswith(".exe"):
        exe_path = game_path
        # <game root>/bin/studiomdl.exe is the usual layout.
        game_root = os.path.dirname(os.path.dirname(exe_path))
        return exe_path, find_game_dir(game_root)

    if not os.path.isdir(game_path):
        return None, None

    for relative in ("bin", os.path.join("game", "bin")):
        candidate = os.path.join(game_path, relative, "studiomdl.exe")
        if os.path.isfile(candidate):
            return candidate, find_game_dir(game_path)

    found = _find_file_up_and_down(game_path, "studiomdl.exe", max_depth=3)
    if found:
        game_root = os.path.dirname(os.path.dirname(found))
        return found, find_game_dir(game_root) or find_game_dir(
            os.path.dirname(found)
        )
    return None, find_game_dir(game_path)


def _move_model_files(directory: str, stem: str, target_dir: str) -> list[str]:
    """Move ``<stem>.*`` model files (mdl + companions) from dir to target.

    Returns the list of moved source paths (in move order, for restoring).
    """
    moved: list[str] = []
    if not os.path.isdir(directory):
        return moved
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        base, dot, ext = name.partition(".")
        if base.lower() != stem.lower() or not dot or not ext:
            continue
        os.makedirs(target_dir, exist_ok=True)
        shutil.move(full, os.path.join(target_dir, name))
        moved.append(full)
    return moved


def _qc_model_subdir(qc_path: str) -> str:
    """The subdirectory of ``$modelname`` inside the game's ``models`` folder."""
    try:
        with open(qc_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r'\s*\$modelname\s+"([^"]+)"', line)
                if match:
                    return os.path.dirname(match.group(1).replace("/", os.sep))
    except OSError:
        pass
    return ""


def _fix_melee_layer_fadein(qc_path: str, log: Callable[[str], None]) -> None:
    """处理淡入: force ``fadein 0.2`` on every ACT_VM_MELEE_LAYER sequence.

    The QC may contain several such sequences, and Source-style delta
    sequence blocks can lack a fadein line entirely (the options writer
    aborts early) - missing counts as "not 0.2" and gets the line inserted.
    """
    import re

    with open(qc_path, encoding="utf-8") as handle:
        text = handle.read()
    changed: list[str] = []

    def fix_block(match: "re.Match[str]") -> str:
        block = match.group(0)
        if 'activity "ACT_VM_MELEE_LAYER"' not in block:
            return block
        name_match = re.search(r'\$sequence\s+"([^"]+)"', block)
        name = name_match.group(1) if name_match else "?"
        fade = re.search(r"(?m)^\tfadein\s+([0-9.]+)\s*$", block)
        if fade:
            if float(fade.group(1)) == 0.2:
                return block
            changed.append(f"{name}: fadein {fade.group(1)} -> 0.2")
            return block[:fade.start()] + "\tfadein 0.200000" + block[fade.end():]
        changed.append(f"{name}: 补充 fadein 0.2（原 QC 无 fadein 行）")
        return block[:block.rfind("\n}")] + "\n\tfadein 0.200000\n}" + block[block.rfind("\n}") + 3:]

    new_text = re.sub(r'\$sequence\s+"[^"]+"\s*\{.*?\n\}', fix_block, text, flags=re.S)
    if changed:
        with open(qc_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        log(f"    处理淡入: {len(changed)} 处 ACT_VM_MELEE_LAYER 已改为 fadein 0.2")
        for line in changed:
            log("      " + line)


def run_one_click(
    mdl_path: str,
    options: DecompileOptions,
    *,
    studiomdl_path: str = "",
    game_path: str = "",
    output_path: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> OneClickResult:
    """Run the full backup -> decompile -> compile -> restore pipeline."""
    result = OneClickResult(mdl_path=os.path.abspath(mdl_path))
    mdl_dir = os.path.dirname(result.mdl_path)
    stem = os.path.splitext(os.path.basename(result.mdl_path))[0]
    result.dec_dir = os.path.abspath(output_path) if output_path else os.path.join(mdl_dir, "dec")
    result.source_dir = os.path.join(mdl_dir, "source")
    dec_mdl_path = os.path.join(result.source_dir, os.path.basename(result.mdl_path))
    qc_path = os.path.join(result.dec_dir, stem + ".qc")

    def fail(message: str, restore: bool = False) -> OneClickResult:
        result.errors.append(message)
        log("    错误: " + message)
        if restore:
            log("    一键处理: 恢复原始模型 ...")
            _move_model_files(result.source_dir, stem, mdl_dir)
        return result

    # 1. Locate studiomdl.exe and the -game folder.
    exe_path = studiomdl_path.strip()
    game_dir = ""
    if exe_path:
        if not os.path.isfile(exe_path):
            return fail(f"studiomdl.exe 不存在: {exe_path}")
        game_dir = find_game_dir(
            game_path.strip() or os.path.dirname(os.path.dirname(exe_path))
        ) or ""
    elif game_path.strip():
        exe_path, found_game_dir = find_studiomdl(game_path.strip())
        if not exe_path:
            return fail(f"在游戏路径中未找到 studiomdl.exe: {game_path.strip()}")
        game_dir = found_game_dir or ""
        log(f"    使用 studiomdl.exe: {exe_path}")
    else:
        return fail("需要提供游戏路径或 studiomdl.exe 路径")
    if not game_dir or not os.path.isdir(game_dir):
        return fail(
            f"未找到包含 gameinfo.txt 的游戏目录（编译需要 -game 参数），"
            f"游戏路径: {game_path.strip() or os.path.dirname(exe_path)}"
        )

    # 2. Back up the original model + companions into <mdl dir>/source.
    log("    备份原始模型到 " + result.source_dir)
    moved = _move_model_files(mdl_dir, stem, result.source_dir)
    if not any(os.path.basename(path) == os.path.basename(result.mdl_path) for path in moved):
        return fail(f"未找到原始模型文件: {result.mdl_path}", restore=True)

    try:
        # 3. Decompile (from the backup location) into <mdl dir>/dec.
        log(f"    反编译 {os.path.basename(dec_mdl_path)} -> {result.dec_dir}")
        decompiler = MdlDecompiler(replace(options, game_path=game_dir))
        result.decompile_result = decompiler.decompile(
            dec_mdl_path, result.dec_dir, progress=log
        )
        for error in result.decompile_result.errors:
            log(f"    警告: 反编译报告错误: {error}")
        if not result.decompile_result.success:
            return fail("反编译失败，已恢复原始模型。", restore=True)
        if options.melee_layer_fadein_fix and os.path.isfile(qc_path):
            _fix_melee_layer_fadein(qc_path, log)

        # 4. Compile with studiomdl.
        log(f"    编译 {qc_path}")
        compile_game = game_dir
        if result.decompile_result.model_role in ("viewmodel", "animation_library"):
            stage = tempfile.mkdtemp(prefix=".loka_compile_", dir=result.dec_dir)
            compile_game = prepare_compile_game(stage, game_dir, result.dec_dir)
        process = subprocess.run(
            [exe_path, "-nop4", "-game", compile_game, "-i", qc_path],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=result.dec_dir,
        )
        tail = [line for line in (process.stdout or "").splitlines() if line.strip()][-8:]
        for line in tail:
            log("    studiomdl: " + line)
        if process.returncode != 0:
            for line in (process.stderr or "").splitlines()[-4:]:
                log("    studiomdl: " + line)
            return fail(f"studiomdl 退出码 {process.returncode}，已恢复原始模型。", restore=True)

        # 5. Move the compiled model back next to the original's location.
        output_dir = os.path.join(compile_game, "models", _qc_model_subdir(qc_path))
        compiled = _move_model_files(output_dir, stem, mdl_dir)
        if not any(path.lower().endswith(".mdl") for path in compiled):
            return fail(
                f"studiomdl 退出码 0 但未在 {output_dir} 找到编译产物，已恢复原始模型。",
                restore=True,
            )
        result.compiled_files = [os.path.basename(path) for path in compiled]
        log("    编译完成: " + ", ".join(result.compiled_files))
        result.succeeded = True
        return result
    except Exception as error:  # noqa: BLE001 - the backup must always be restored
        return fail(f"一键处理异常: {exception_text(error)}", restore=True)
