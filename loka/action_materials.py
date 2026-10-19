"""Bounded VTF decoding and read-only Source material lookup.

The preview uses the largest mip of animation frame zero. It does not emulate
Source's shader/proxy system; unsupported rendering behavior is reported.
Disk format reference: ValveSoftware/source-sdk-2013/src/public/vtf/vtf.h.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
import hashlib
import math
from pathlib import Path
import re
import struct
from threading import Lock

from .vmt_parser import parse_vmt
from .vpk import VpkArchive, safe_archive_path

_MAX_PIXELS = 16_777_216
_MAX_RESOURCE_BYTES = 128 * 1024 * 1024
_TEXTURE_CACHE_BYTES = 128 * 1024 * 1024
_texture_cache = OrderedDict()
_texture_cache_size = 0
_texture_cache_lock = Lock()


def clear_texture_cache():
    """Release shared decoded images at a preview unload boundary."""
    global _texture_cache_size
    with _texture_cache_lock:
        _texture_cache.clear()
        _texture_cache_size = 0


@dataclass(frozen=True)
class TextureImage:
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class PreviewMaterial:
    name: str
    texture: TextureImage | None = None
    normal_texture: TextureImage | None = None
    tint: tuple[float, float, float] = (1., 1., 1.)
    translucent: bool = False
    alpha_test: bool = False
    no_cull: bool = False
    self_illum: bool = False
    phong: bool = False
    warnings: tuple[str, ...] = ()
    additive: bool = False


def _image_size(width, height, fmt):
    if fmt in (0, 12, 16):
        return width * height * 4
    if fmt in (2, 3):
        return width * height * 3
    if fmt in (13, 20):
        return ((width + 3) // 4) * ((height + 3) // 4) * 8
    if fmt in (14, 15):
        return ((width + 3) // 4) * ((height + 3) // 4) * 16
    raise ValueError(f'Unsupported VTF image format: {fmt}')


def _rgb565(value):
    r, g, b = value >> 11, (value >> 5) & 63, value & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _decode_blocks_native(raw, width, height, fmt):
    # Image.frombytes invokes the C BCN decoder directly; Image.open/init would
    # import unrelated file format plugins. Keep the source's original mip size.
    from PIL import Image
    kind = {13: 1, 20: 1, 14: 2, 15: 3}[fmt]
    return Image.frombytes('RGBA', (width, height), raw, 'bcn', (kind,)).tobytes()


def _decode_blocks(raw, width, height, fmt):
    try:
        return _decode_blocks_native(raw, width, height, fmt)
    except (ImportError, AttributeError, OSError, ValueError):
        # Source-only/minimal installations still support every preview format.
        return _decode_blocks_python(raw, width, height, fmt)


def _decode_blocks_python(raw, width, height, fmt):
    output = bytearray(width * height * 4)
    stride = 8 if fmt in (13, 20) else 16
    offset = 0
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            block = raw[offset:offset + stride]
            offset += stride
            colors_at = 0 if stride == 8 else 8
            c0, c1, indices = struct.unpack_from('<HHI', block, colors_at)
            rgb0, rgb1 = _rgb565(c0), _rgb565(c1)
            colors = [(*rgb0, 255), (*rgb1, 255)]
            if c0 > c1 or stride == 16:
                colors.extend([tuple((2*a+b)//3 for a, b in zip(rgb0, rgb1)) + (255,),
                               tuple((a+2*b)//3 for a, b in zip(rgb0, rgb1)) + (255,)])
            else:
                colors.extend([tuple((a+b)//2 for a, b in zip(rgb0, rgb1)) + (255,), (0, 0, 0, 0)])
            if fmt == 14:
                alpha_bits = int.from_bytes(block[:8], 'little')
            elif fmt == 15:
                a, b = block[0], block[1]
                alphas = [a, b]
                if a > b:
                    alphas.extend(((7-i)*a+i*b)//7 for i in range(1, 7))
                else:
                    alphas.extend(((5-i)*a+i*b)//5 for i in range(1, 5))
                    alphas.extend((0, 255))
                alpha_bits = int.from_bytes(block[2:8], 'little')
            for row in range(min(4, height-y)):
                for col in range(min(4, width-x)):
                    pixel = row*4 + col
                    color = colors[(indices >> (pixel*2)) & 3]
                    if fmt == 14:
                        color = (*color[:3], ((alpha_bits >> (pixel*4)) & 15)*17)
                    elif fmt == 15:
                        color = (*color[:3], alphas[(alpha_bits >> (pixel*3)) & 7])
                    target = ((y+row)*width+x+col)*4
                    output[target:target+4] = bytes(color)
    return bytes(output)


def _decode_vtf_uncached(data: bytes) -> TextureImage:
    """Decode RGBA/BGRA/BGRX8888, RGB/BGR888, DXT1/one-bit-alpha, DXT3/5.

    Return mip zero/frame zero. Cubemaps and volume textures are rejected;
    unsupported formats and malformed/beyond-budget inputs raise ValueError.
    """
    if len(data) < 64 or data[:4] != b'VTF\0':
        raise ValueError('Invalid or truncated VTF header')
    major, minor, header_size = struct.unpack_from('<III', data, 4)
    minimum = 80 if minor >= 3 else 65 if minor >= 2 else 64
    if major != 7 or minor > 5 or not minimum <= header_size <= len(data):
        raise ValueError('Unsupported VTF version or invalid header size')
    width, height, flags, frames = struct.unpack_from('<HHIH', data, 16)
    fmt = struct.unpack_from('<I', data, 52)[0]
    mips = data[56]
    depth = struct.unpack_from('<H', data, 63)[0] if minor >= 2 else 1
    if not width or not height or width*height > _MAX_PIXELS or not frames:
        raise ValueError('VTF dimensions/frame count outside preview limits')
    if depth != 1 or flags & 0x4000:
        raise ValueError('Unsupported VTF volume texture or cubemap')
    if not 1 <= mips <= max(width, height).bit_length():
        raise ValueError('Invalid VTF mip count')
    largest_size = _image_size(width, height, fmt)
    skipped = sum(_image_size(max(1, width >> mip), max(1, height >> mip), fmt)*frames
                  for mip in range(1, mips))
    total = skipped + largest_size*frames
    if total > _MAX_RESOURCE_BYTES:
        raise ValueError('VTF image data exceeds preview byte limit')
    if minor >= 3:
        if len(data) < 80:
            raise ValueError('Truncated VTF resource header')
        count = struct.unpack_from('<I', data, 68)[0]
        if count > 32 or 80 + count*8 > header_size:
            raise ValueError('VTF resource directory exceeds header')
        image_offsets = []
        for index in range(count):
            at = 80 + index*8
            tag, resource_flags = data[at:at+3], data[at+3]
            if tag == b'\x30\0\0':
                if resource_flags:
                    raise ValueError('Invalid VTF high-resolution image resource flags')
                image_offsets.append(struct.unpack_from('<I', data, at+4)[0])
        if len(image_offsets) != 1:
            raise ValueError('Missing or duplicate VTF high-resolution image resource')
        image_offset = image_offsets[0]
    else:
        low_width, low_height = data[61:63]
        low_fmt = struct.unpack_from('<I', data, 57)[0]
        if bool(low_width) != bool(low_height):
            raise ValueError('Invalid VTF thumbnail dimensions')
        low_size = _image_size(low_width, low_height, low_fmt) if low_width else 0
        image_offset = header_size + low_size
    if image_offset < header_size or image_offset + total > len(data):
        raise ValueError('VTF image data range out of bounds')
    start = image_offset + skipped
    raw = data[start:start+largest_size]
    if fmt == 0:
        rgba = raw
    elif fmt in (2, 3, 12, 16):
        converted = bytearray(width * height * 4)
        pixel_size = 3 if fmt in (2, 3) else 4
        converted[0::4] = raw[0::pixel_size] if fmt == 2 else raw[2::pixel_size]
        converted[1::4] = raw[1::pixel_size]
        converted[2::4] = raw[2::pixel_size] if fmt == 2 else raw[0::pixel_size]
        converted[3::4] = raw[3::4] if fmt == 12 else b'\xff' * (width*height)
        rgba = bytes(converted)
    else:
        rgba = _decode_blocks(raw, width, height, fmt)
    return TextureImage(width, height, rgba)


def decode_vtf(data: bytes) -> TextureImage:
    """Decode source resolution frame zero, sharing immutable images by content.

    Cache only successful bounded results, never file paths or mutable source
    bytes. New bytes at the same path therefore cannot reuse an outdated image.
    """
    global _texture_cache_size
    key = hashlib.sha256(data).digest()
    with _texture_cache_lock:
        image = _texture_cache.get(key)
        if image is not None:
            _texture_cache.move_to_end(key)
            return image
    image = _decode_vtf_uncached(data)
    size = len(image.rgba)
    with _texture_cache_lock:
        previous = _texture_cache.pop(key, None)
        if previous is not None:
            _texture_cache_size -= len(previous.rgba)
        if size <= _TEXTURE_CACHE_BYTES:
            _texture_cache[key] = image
            _texture_cache_size += size
        while _texture_cache and (_texture_cache_size > _TEXTURE_CACHE_BYTES or len(_texture_cache) > 64):
            _, removed = _texture_cache.popitem(last=False)
            _texture_cache_size -= len(removed.rgba)
    return image


def _resource_key(value, extension):
    value = value.replace('\\', '/')
    if value.lower().startswith('materials/'):
        value = value[10:]
    if not value.lower().endswith(extension):
        value += extension
    try:
        return safe_archive_path('materials/' + value).lower()
    except ValueError as error:
        raise ValueError(f'Unsafe material resource path: {value!r}') from error


def _validate_resolved_target(root, resolved, *, exclude_addons=False):
    """Apply the same containment policy to loose files, VPKs and chunks.

    Inputs are already resolved so the boundary can also be checked with pure
    Windows paths without needing OS privileges to create file symlinks.
    """
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f'Unsafe resource escapes root: {resolved}') from error
    if exclude_addons and relative.parts and relative.parts[0].lower() == 'addons':
        raise ValueError(f'Unsafe resource resolves into game addons: {resolved}')


def _case_path(root, relative, *, exclude_addons=False):
    """Match Windows-style resource spelling while confining symlinks/junctions."""
    root = root.resolve()
    current = root
    for part in relative.split('/'):
        if not current.is_dir():
            return None
        matches = [child for child in current.iterdir() if child.name.casefold() == part.casefold()]
        if len(matches) > 1:
            raise ValueError(f'Ambiguous resource case: {relative}')
        if not matches:
            return None
        current = matches[0]
        _validate_resolved_target(root, current.resolve(), exclude_addons=exclude_addons)
    return current if current.is_file() else None


def _game_roots(game_path):
    if not game_path:
        return []
    path = Path(game_path).resolve()
    if any(part.lower() == 'addons' for part in path.parts):
        return []
    names = ('update', 'left4dead2_dlc3', 'left4dead2_dlc2', 'left4dead2_dlc1', 'left4dead2', 'hl2')
    is_game_dir = path.name.lower() in names or (path/'gameinfo.txt').is_file()
    base = path.parent if is_game_dir else path
    roots = [base/name for name in names]
    if is_game_dir and path.name.lower() not in names:
        roots.insert(0, path)
    return roots


class MaterialResolver:
    """Resolve pack content first, then original game loose files and pak VPKs.

    The resolver owns a session cache; create a new resolver after pack edits.
    Lookup never traverses a game's addons directory and never extracts assets.
    """
    def __init__(self, pack_dir, game_path=''):
        self.pack_dir = Path(pack_dir).resolve()
        self.game_roots = _game_roots(game_path)
        self._archives = {}
        self._textures = {}
        self._texture_warnings = {}

    def _read(self, key, warnings):
        limit = 2*1024*1024 if key.endswith('.vmt') else _MAX_RESOURCE_BYTES
        for root in [self.pack_dir, *self.game_roots]:
            is_game = root != self.pack_dir
            if is_game and any(part.lower() == 'addons' for part in root.resolve().parts):
                continue
            path = _case_path(root, key, exclude_addons=is_game)
            if path is not None:
                if path.stat().st_size > limit:
                    raise ValueError(f'Resource exceeds preview byte limit: {key}')
                return path.read_bytes()
            if root == self.pack_dir or not root.is_dir():
                continue
            if root not in self._archives:
                archives = []
                for candidate in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                    if not re.fullmatch(r'pak\d+(?:_dir)?\.vpk', candidate.name, re.I):
                        continue
                    try:
                        _validate_resolved_target(root.resolve(), candidate.resolve(), exclude_addons=True)
                        archives.append(VpkArchive(candidate, include_all=True))
                    except (OSError, ValueError) as error:
                        warnings.append(f'Cannot index game VPK {candidate.name}: {error}')
                self._archives[root] = archives
            for archive in self._archives[root]:
                _validate_resolved_target(root.resolve(), archive.path.resolve(), exclude_addons=True)
                entry = archive.entries.get(key)
                if entry is None:
                    continue
                if len(entry.preload) + entry.length > limit:
                    raise ValueError(f'Resource exceeds preview byte limit: {key}')
                if entry.archive != 0x7fff:
                    base = archive.path.name[:-8] if archive.path.name.lower().endswith('_dir.vpk') else archive.path.stem
                    chunk = archive.path.with_name(base + f'_{entry.archive:03d}.vpk')
                    _validate_resolved_target(root.resolve(), chunk.resolve(), exclude_addons=True)
                return archive.read(key)
        return None

    def _tree(self, nodes, warnings):
        tree = {}
        for node in nodes:
            if node.conditions:
                warnings.append(f'Unsupported material condition on {node.key.value}; skipped')
                continue
            key = node.key.value.lower()
            if key in tree:
                warnings.append(f'Duplicate material parameter: {key}')
            tree[key] = self._tree(node.children, warnings) if node.is_block else node.value.value
        return tree

    def _material(self, key, stack, warnings):
        if key in stack:
            raise ValueError('Material include cycle: ' + ' -> '.join((*stack, key)))
        if len(stack) >= 32:
            raise ValueError('Material include depth exceeds preview limit')
        raw = self._read(key, warnings)
        if raw is None:
            raise ValueError(f'Missing material: {key}')
        doc = parse_vmt(raw, max_depth=32, max_tokens=50000)
        shaders = [node for node in doc.nodes if node.is_block]
        for node in doc.nodes:
            if not node.is_block:
                warnings.append(f'Unsupported material directive: {node.key.value}')
        if len(shaders) != 1:
            raise ValueError(f'Expected one material shader: {key}')
        root = shaders[0]
        if root.conditions:
            warnings.append('Unsupported material shader condition; preview is approximate')
        shader = root.key.value.lower()
        params = self._tree(root.children, warnings)
        if shader != 'patch':
            return shader, params
        include = params.get('include')
        if not isinstance(include, str):
            raise ValueError(f'Patch missing include: {key}')
        shader, base = self._material(_resource_key(include, '.vmt'), (*stack, key), warnings)
        for operation in ('replace', 'insert'):
            changes = params.get(operation, {})
            if not isinstance(changes, dict):
                raise ValueError(f'Invalid Patch {operation} block: {key}')
            self._patch(base, changes, operation)
        for name in params.keys() - {'include', 'insert', 'replace'}:
            warnings.append(f'Unsupported Patch parameter: {name}')
        return shader, base

    @staticmethod
    def _patch(base, changes, operation):
        for key, value in changes.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                MaterialResolver._patch(base[key], value, operation)
            elif (operation == 'replace' and key in base) or (operation == 'insert' and key not in base):
                base[key] = value

    def _texture(self, value, warnings):
        if not isinstance(value, str) or not value:
            return None
        key = _resource_key(value, '.vtf')
        if key not in self._textures:
            notes = []
            try:
                raw = self._read(key, notes)
                if raw is None:
                    raise ValueError(f'Missing texture: {key}')
                image = decode_vtf(raw)
                if struct.unpack_from('<H', raw, 24)[0] > 1:
                    notes.append(f'Animated texture {key}: only frame zero is previewed')
                self._textures[key] = image
            except (ValueError, OSError) as error:
                notes.append(f'Cannot preview texture {key}: {error}')
                self._textures[key] = None
            self._texture_warnings[key] = tuple(notes)
        warnings.extend(self._texture_warnings[key])
        return self._textures[key]

    @staticmethod
    def _tint(value, warnings):
        try:
            if not isinstance(value, str):
                raise ValueError()
            text = value.strip()
            scale = 255. if text.startswith('{') and text.endswith('}') else 1.
            parts = text.strip('[]{}').split()
            values = tuple(float(part)/scale for part in parts)
            if len(values) != 3 or any(not math.isfinite(part) or part < 0 for part in values):
                raise ValueError()
            return values
        except (ValueError, TypeError):
            warnings.append(f'Invalid material tint: {value!r}')
            return (1., 1., 1.)

    def resolve(self, material_name: str) -> PreviewMaterial:
        warnings = []
        try:
            key = _resource_key(material_name, '.vmt')
            shader, params = self._material(key, (), warnings)
        except (ValueError, OSError) as error:
            warnings.append(str(error))
            return PreviewMaterial(name=material_name, warnings=tuple(warnings))
        if shader not in ('vertexlitgeneric', 'unlitgeneric'):
            warnings.append(f'Unsupported Source shader: {shader}; basic texture preview only')
        elif shader == 'vertexlitgeneric':
            warnings.append('Source VertexLitGeneric lighting is approximated in preview')
        handled = {'$basetexture', '$bumpmap', '$normalmap', '$color', '$color2', '$translucent',
                   '$alphatest', '$nocull', '$selfillum', '$phong', '$model', '$surfaceprop',
                   '$surfaceprop2', '$keywords', '$additive'}
        for name, value in params.items():
            if name == 'proxies':
                warnings.append('Unsupported Source Proxies: dynamic material behavior is not previewed')
            elif name not in handled:
                warnings.append(f'Unsupported Source material parameter: {name}')
        def texture(name):
            try:
                if name in params and (not isinstance(params[name], str) or not params[name]):
                    raise ValueError(f'Invalid material texture parameter {name}')
                return self._texture(params.get(name), warnings)
            except (ValueError, OSError) as error:
                warnings.append(str(error))
                return None
        def flag(name):
            value = params.get(name, '0')
            try:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError()
                return number != 0
            except (ValueError, TypeError):
                warnings.append(f'Invalid material flag {name}: {value!r}')
                return False
        color = self._tint(params.get('$color', '[1 1 1]'), warnings)
        color2 = self._tint(params.get('$color2', '[1 1 1]'), warnings)
        tint = tuple(a*b for a, b in zip(color, color2))
        if not all(math.isfinite(value) for value in tint):
            warnings.append('Invalid combined material tint: numeric overflow')
            tint = (1., 1., 1.)
        additive = flag('$additive')
        return PreviewMaterial(
            name=material_name, texture=texture('$basetexture'),
            normal_texture=texture('$bumpmap' if '$bumpmap' in params else '$normalmap'),
            tint=tint,
            translucent=flag('$translucent') or additive, alpha_test=flag('$alphatest'),
            no_cull=flag('$nocull'), self_illum=flag('$selfillum') or shader == 'unlitgeneric', phong=flag('$phong'),
            warnings=tuple(dict.fromkeys(warnings)), additive=additive)
