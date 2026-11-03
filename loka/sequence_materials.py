"""Directory matching and one-file sequence material generation/publication."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from .sequence_material_recipes import OutputRule, get_recipe
from .sequence_material_vtf import (MAX_SOURCE_BYTES, check_cancel,
                                    inspect_source_header, encode_sequence)


@dataclass(frozen=True)
class MaterialChoice:
    root: Path
    source: Path
    relative_source: Path
    relative_output: Path
    recipe_id: str
    rule: OutputRule


@dataclass(frozen=True)
class MaterialScan:
    root: Path
    choices: tuple[MaterialChoice, ...]
    warnings: tuple[str, ...]
    skipped: int


@dataclass(frozen=True)
class GeneratedMaterial:
    choice: MaterialChoice
    source_digest: str
    data: bytes
    frames: tuple
    final_psnr: float
    template_digest: str
    sweep_color: tuple[int, int, int] | None = None
    wire_color: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class BatchMaterialResult:
    created: tuple[Path, ...]
    skipped: tuple[Path, ...]
    failed: tuple[tuple[MaterialChoice, str], ...]
    cancelled: bool


def generate_all_materials(choices, output_root, *, cancelled=lambda: False,
                           on_progress=lambda stage, done, total: None,
                           on_item=lambda index, state: None) -> BatchMaterialResult:
    """Process all scanned mappings with recipe defaults, retaining no images."""
    from .process_limits import exception_text
    created, skipped, failed = [], [], []
    interrupted = False
    for index, choice in enumerate(choices):
        if cancelled():
            interrupted = True
            break
        on_item(index, '处理中')
        product = target = None
        try:
            target = output_path(choice, output_root)
            if target.exists():
                skipped.append(target)
                on_item(index, '已跳过')
                continue
            # Explicitly omit single-option color settings and preview caches.
            product = generate_material(choice, cancelled=cancelled, on_progress=on_progress)
            path = save_material(product, output_root, cancelled=cancelled)
            created.append(path)
            on_item(index, '已生成')
        except InterruptedError:
            on_item(index, '已取消')
            interrupted = True
            break
        except FileExistsError as error:
            if target is not None and target.exists():
                skipped.append(target)
                on_item(index, '已跳过')
            else:
                failed.append((choice, exception_text(error)))
                on_item(index, '失败')
        except Exception as error:
            failed.append((choice, exception_text(error)))
            on_item(index, '失败')
        finally:
            del product
    return BatchMaterialResult(tuple(created), tuple(skipped), tuple(failed), interrupted)


def _package_root(root: Path) -> Path | None:
    for candidate in (root, *root.parents):
        if candidate.name.casefold() == 'materials':
            return candidate.parent
    return None


def default_output_directory(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    package = _package_root(root) or root
    if not package.name:
        return package / '序列帧输出'
    return package.with_name(package.name + '_序列帧')


def _material_relative(source: Path, root: Path) -> Path:
    # Accept both materials/models (or a child) and a whole material package.
    # A source that already lives below materials must not acquire it twice.
    package = _package_root(root) or _package_root(source.parent)
    return source.relative_to(package) if package else Path('materials', 'models') / source.relative_to(root)


def scan_material_directory(root, recipe_id, *, cancelled=lambda: False,
                            on_progress=lambda stage, done, total: None) -> MaterialScan:
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('请选择有效的材质输入目录。')
    recipe = get_recipe(recipe_id)
    by_name = {}
    for rule in recipe.rules:
        by_name.setdefault(rule.source_name.casefold(), []).append(rule)
    choices = []
    warnings = []
    skipped = 0
    visited = set()

    def warn(message):
        nonlocal skipped
        skipped += 1
        if len(warnings) < 200:
            warnings.append(message)

    def walk_error(error):
        warn(str(error))

    examined = 0
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        check_cancel(cancelled)
        folder = Path(directory)
        # Avoid junction escapes and cycles, including Windows junctions which
        # os.walk does not consistently treat as ordinary symlinks.
        resolved = folder.resolve()
        key = os.path.normcase(str(resolved))
        if not resolved.is_relative_to(root) or key in visited:
            directories[:] = []
            continue
        visited.add(key)
        kept = []
        for name in sorted(directories, key=str.casefold):
            check_cancel(cancelled)
            candidate = folder / name
            try:
                if not candidate.is_symlink() and not getattr(candidate, 'is_junction', lambda: False)():
                    if candidate.resolve().is_relative_to(root):
                        kept.append(name)
            except OSError as error:
                warn(str(error))
        directories[:] = kept
        for filename in sorted(filenames, key=str.casefold):
            check_cancel(cancelled)
            rules = by_name.get(filename.casefold())
            if not rules:
                continue
            examined += 1
            on_progress('扫描目录', examined, 0)
            source = folder / filename
            try:
                if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
                    raise ValueError('源贴图链接超出输入目录。')
                with source.open('rb') as stream:
                    inspect_source_header(stream.read(80), os.fstat(stream.fileno()).st_size)
                relative = _material_relative(source, root)
                for rule in rules:
                    choices.append(MaterialChoice(root, source, relative,
                                                  relative.with_name(rule.output_name),
                                                  recipe_id, rule))
            except (OSError, ValueError) as error:
                warn(f'{source}: {error}')
    check_cancel(cancelled)
    return MaterialScan(root, tuple(choices), tuple(warnings), skipped)


def _read_source(choice):
    source = choice.source.resolve(strict=True)
    if not source.is_relative_to(choice.root):
        raise ValueError('源贴图已移动到输入目录之外，请重新扫描。')
    with source.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        header = stream.read(80)
        inspect_source_header(header, size)
        stream.seek(0)
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES or len(data) != size:
        raise ValueError('源贴图读取期间发生变化或超出读取上限，请重新扫描。')
    return data


def generate_material(choice: MaterialChoice, *, cached: GeneratedMaterial | None = None, sweep_color=None, wire_color=None,
                      cancelled=lambda: False,
                      on_progress=lambda stage, done, total: None) -> GeneratedMaterial:
    from .action_materials import _decode_vtf_uncached
    from .sequence_material_templates import load_material_template
    from .sequence_material_colors import validate_sweep_color
    sweep_color = validate_sweep_color(sweep_color)
    wire_color = validate_sweep_color(wire_color)
    if sweep_color is not None and choice.rule.layer == 'normal':
        raise ValueError('法线层不支持扫过颜色自定义。')
    if wire_color is not None and choice.rule.layer == 'normal':
        raise ValueError('法线层不支持线框色彩自定义。')
    check_cancel(cancelled)
    on_progress('读取源贴图', 0, 1)
    data = _read_source(choice)
    digest = hashlib.sha256(data).hexdigest()
    check_cancel(cancelled)
    on_progress('读取材质规则', 0, 1)
    template = load_material_template(choice.recipe_id, choice.rule, cancelled=cancelled)
    if sweep_color is not None and template.sweep_color is None:
        raise ValueError('该项参考动画没有可提取的打印前沿，暂不支持调色。')
    if wire_color is not None and template.wire_color is None:
        raise ValueError('该项参考动画没有可提取的线框，暂不支持线框调色。')
    if sweep_color == template.sweep_color:
        sweep_color = None
    if wire_color == template.wire_color:
        wire_color = None
    if (cached is not None and cached.choice == choice and cached.source_digest == digest
            and cached.template_digest == template.digest and cached.sweep_color == sweep_color
            and cached.wire_color == wire_color):
        on_progress('复用预览产物', 1, 1)
        return cached
    source = _decode_vtf_uncached(data)
    del data
    check_cancel(cancelled)
    product, frames, psnr = encode_sequence(source, choice.rule, template=template, sweep_color=sweep_color,
                                           wire_color=wire_color, cancelled=cancelled,
                                           on_progress=on_progress)
    check_cancel(cancelled)
    return GeneratedMaterial(choice, digest, product, frames, psnr, template.digest, sweep_color, wire_color)


def output_path(choice: MaterialChoice, output_root) -> Path:
    root = Path(output_root).expanduser().resolve()
    relative = choice.relative_output
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('材质输出相对路径无效。')
    destination = root / relative
    if not destination.resolve().is_relative_to(root):
        raise ValueError('材质输出路径超出所选目录。')
    if destination.resolve() == choice.source.resolve():
        raise ValueError('输出路径不能覆盖源贴图。')
    return destination


def save_material(result: GeneratedMaterial, output_root, *, cancelled=lambda: False) -> Path:
    """Publish exactly one complete VTF. Never replace an existing output."""
    check_cancel(cancelled)
    destination = output_path(result.choice, output_root)
    if destination.exists():
        raise FileExistsError('输出文件已存在，已跳过：' + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Recheck containment after parent creation/junction resolution.
    destination = output_path(result.choice, output_root)
    descriptor, name = tempfile.mkstemp(prefix='.loka-sequence-', suffix='.tmp',
                                      dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            for offset in range(0, len(result.data), 1024 * 1024):
                check_cancel(cancelled)
                stream.write(result.data[offset:offset + 1024 * 1024])
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancelled)
        if os.name == 'nt':
            # Unlike os.replace, Windows rename refuses an existing target.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
