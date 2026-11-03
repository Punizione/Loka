"""Target-resolution synthesis and VTF 7.2 encoding using Pillow core only.

VTF stores mip levels smallest first, with all frames inside each mip level.
BCN codec usage follows Pillow's DdsImagePlugin, without importing that plugin.
"""
from __future__ import annotations

import math
import struct

from .sequence_material_recipes import OutputRule


MAX_SOURCE_BYTES = 128 * 1024 * 1024
_FORMATS = frozenset((0, 2, 3, 12, 13, 14, 15, 16, 20))


def check_cancel(cancelled):
    if cancelled():
        raise InterruptedError('序列帧材质任务已取消。')


def inspect_source_header(header: bytes, file_size: int) -> tuple[int, int, int]:
    if len(header) < 65 or header[:4] != b'VTF\0':
        raise ValueError('VTF 头部无效或不完整。')
    major, minor, header_size = struct.unpack_from('<III', header, 4)
    if major != 7 or minor > 5 or not (80 if minor >= 3 else 65 if minor >= 2 else 64) <= header_size <= file_size:
        raise ValueError('VTF 版本或头部大小不支持。')
    width, height, flags, frames = struct.unpack_from('<HHIH', header, 16)
    if frames != 1:
        raise ValueError('仅支持单帧源 VTF。')
    if not width or width != height or width > 4096 or width & (width - 1):
        raise ValueError('源 VTF 必须是边长不超过 4096 的二维方形、2 的幂次贴图。')
    depth = struct.unpack_from('<H', header, 63)[0] if minor >= 2 else 1
    if depth != 1 or flags & 0x4000:
        raise ValueError('不支持体积贴图或立方体贴图。')
    fmt = struct.unpack_from('<I', header, 52)[0]
    if fmt not in _FORMATS:
        raise ValueError('源 VTF 图像格式不支持：' + str(fmt))
    if not 1 <= header[56] <= width.bit_length():
        raise ValueError('源 VTF mip 数量无效。')
    if file_size > MAX_SOURCE_BYTES:
        raise ValueError('源 VTF 超过 128 MiB 读取上限。')
    return width, height, fmt


def _linear(value):
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


def _srgb(value):
    return value * 12.92 if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055


_LINEAR_LUT = [_linear(i / 255) for i in range(256)]
_SRGB_LUT = bytes(round(min(1., max(0., _srgb(i / 65535))) * 255) for i in range(65536))


def _normalize(image, cancelled=lambda: False):
    """Normalize tangent-space normals; preserve the source alpha separately."""
    from PIL import Image
    raw = bytearray(image.tobytes())
    for at in range(0, len(raw), 4):
        if at % (image.width * 4 * 16) == 0:
            check_cancel(cancelled)
        x = raw[at] / 127.5 - 1
        y = raw[at + 1] / 127.5 - 1
        z = raw[at + 2] / 127.5 - 1
        length = math.sqrt(x*x + y*y + z*z)
        if length < 1e-9:
            x, y, z, length = 0., 0., 1., 1.
        for channel, value in enumerate((x, y, z)):
            raw[at + channel] = round((max(-1., min(1., value / length)) + 1) * 127.5)
    return Image.frombytes('RGBA', image.size, bytes(raw))


def resize_texture(image, size, *, normal=False, cancelled=lambda: False):
    from PIL import Image
    if image.size == size:
        return image.copy()
    check_cancel(cancelled)
    if normal:
        # Resize channels separately: normal RGB must not be alpha-premultiplied.
        resized = Image.merge('RGBA', tuple(b.resize(size, Image.Resampling.BOX)
                                             for b in image.split()))
        return _normalize(resized, cancelled=cancelled)
    channels = []
    for band in image.split()[:3]:
        check_cancel(cancelled)
        linear = band.point(_LINEAR_LUT, 'F').resize(size, Image.Resampling.BOX)
        encoded = bytes(_SRGB_LUT[max(0, min(65535, round(value * 65535)))]
                        for value in linear.getdata())
        channels.append(Image.frombytes('L', size, encoded))
    channels.append(image.getchannel('A').resize(size, Image.Resampling.BOX))
    return Image.merge('RGBA', tuple(channels))


def _block_size(size, fmt):
    return ((size + 3) // 4) ** 2 * (8 if fmt == 13 else 16)


def _encode_blocks(image, fmt):
    from PIL import Image
    mode = 'RGB' if fmt == 13 else 'RGBA'
    image = image.convert(mode)
    # Edge-pad tiny mips so codec padding cannot darken the last 2x2/1x1 mips.
    width, height = image.size
    if width < 4 or height < 4:
        padded = Image.new(mode, (max(4, width), max(4, height)))
        padded.paste(image, (0, 0))
        if width < padded.width:
            padded.paste(image.crop((width-1, 0, width, height)).resize(
                (padded.width-width, height)), (width, 0))
        if height < padded.height:
            padded.paste(padded.crop((0, height-1, padded.width, height)).resize(
                (padded.width, padded.height-height)), (0, height))
        image = padded
    image.load()
    try:
        encoder = Image._getencoder(mode, 'bcn', (1 if fmt == 13 else 3,), ())
    except (AttributeError, OSError) as error:
        raise ValueError('当前 Pillow 不提供 DXT 编码器，请使用支持 BCN 编码的 Pillow 版本。') from error
    output = bytearray()
    try:
        encoder.setimage(image.im, (0, 0, *image.size))
        while True:
            _, status, chunk = encoder.encode(65536)
            output.extend(chunk)
            if status:
                if status < 0:
                    raise ValueError('DXT 压缩失败：' + str(status))
                break
    finally:
        encoder.cleanup()
    expected = _block_size(width, fmt)
    if len(output) != expected:
        raise ValueError('DXT 压缩结果长度不符合贴图尺寸。')
    return bytes(output)


def _header(rule):
    header = bytearray(80)
    header[:4] = b'VTF\0'
    struct.pack_into('<III', header, 4, 7, 2, 80)
    struct.pack_into('<HHIHH', header, 16, rule.size, rule.size, rule.flags, rule.frames, 0)
    struct.pack_into('<f', header, 48, 1.)  # bumpmap scale
    struct.pack_into('<I', header, 52, rule.image_format)
    header[56] = rule.size.bit_length()
    struct.pack_into('<I', header, 57, 0xffffffff)  # no thumbnail
    struct.pack_into('<H', header, 63, 1)  # depth
    return bytes(header)


def validate_product_header(data, rule):
    """Validate the declared reference/product layout without decoding pixels."""
    mips = rule.size.bit_length()
    stride = sum(_block_size(max(1, rule.size >> mip), rule.image_format)
                 for mip in range(mips))
    if len(data) != 80 + rule.frames * stride or data[:4] != b'VTF\0':
        raise ValueError('生成的 VTF 未通过头部或文件长度校验。')
    if (struct.unpack_from('<III', data, 4) != (7, 2, 80)
            or struct.unpack_from('<HHIHH', data, 16) != (rule.size, rule.size, rule.flags, rule.frames, 0)
            or struct.unpack_from('<I', data, 52)[0] != rule.image_format
            or data[56] != mips or data[61:63] != b'\0\0'
            or struct.unpack_from('<H', data, 63)[0] != 1):
        raise ValueError('生成的 VTF 未通过头部或文件长度校验。')


def decode_product_frame(data, rule, index, cancelled=lambda: False):
    from .action_materials import TextureImage, _decode_blocks
    validate_product_header(data, rule)
    if not 0 <= index < rule.frames:
        raise ValueError('VTF 预览帧索引超出范围。')
    check_cancel(cancelled)
    frame_size = _block_size(rule.size, rule.image_format)
    start = len(data) - frame_size * rule.frames
    raw = data[start + index*frame_size:start + (index+1)*frame_size]
    rgba = _decode_blocks(raw, rule.size, rule.size, rule.image_format)
    if len(rgba) != rule.size * rule.size * 4:
        raise ValueError('生成的 VTF 帧解码长度不正确。')
    return TextureImage(rule.size, rule.size, rgba)


def iter_product_frames(data, rule, cancelled=lambda: False):
    """Check the product contract, then decode mip-zero frames from its bytes."""
    for index in range(rule.frames):
        yield decode_product_frame(data, rule, index, cancelled)


def decode_product_frames(data, rule, cancelled=lambda: False):
    return tuple(iter_product_frames(data, rule, cancelled))


def encode_sequence(source, rule: OutputRule, *, template, sweep_color=None, wire_color=None, cancelled=lambda: False,
                    on_progress=lambda stage, done, total: None):
    """Transfer the embedded effect; return bytes, frames and encoding PSNR."""
    from PIL import Image
    from .sequence_material_templates import transfer_template_frame
    from .sequence_material_colors import recolor_sweep, recolor_wire
    normal = rule.layer == 'normal'
    original = Image.frombytes('RGBA', (source.width, source.height), source.rgba)
    static = resize_texture(original, (rule.size, rule.size), normal=normal, cancelled=cancelled)
    if rule.layer == 'glow':
        static.putalpha(255)
    del original
    if sweep_color is None and wire_color is None and static.tobytes() == template.baseline:
        on_progress('复用原始效果模板', rule.frames, rule.frames)
        return template.reference, decode_product_frames(template.reference, rule, cancelled), math.inf
    levels = [bytearray() for _ in range(rule.size.bit_length())]
    final_image = None
    for index, reference_frame in enumerate(iter_product_frames(template.reference, rule, cancelled)):
        check_cancel(cancelled)
        on_progress('合成与压缩', index, rule.frames)
        image = transfer_template_frame(static, reference_frame.rgba, template, rule, index, cancelled=cancelled)
        if wire_color is not None:
            image = recolor_wire(image, reference_frame.rgba, template, wire_color,
                                 rule, index, cancelled=cancelled)
        if sweep_color is not None:
            image = recolor_sweep(image, reference_frame.rgba, template, sweep_color,
                                 rule, index, cancelled=cancelled)
        if index == rule.frames - 1:
            final_image = image.convert('RGB').tobytes()
        for mip in range(len(levels)):
            check_cancel(cancelled)
            if mip:
                side = max(1, rule.size >> mip)
                image = resize_texture(image, (side, side), normal=normal, cancelled=cancelled)
            levels[mip].extend(_encode_blocks(image, rule.image_format))
    check_cancel(cancelled)
    on_progress('校验与解码', rule.frames, rule.frames)
    data = _header(rule) + b''.join(reversed(levels))
    del levels
    decoded = decode_product_frames(data, rule, cancelled)
    reference = final_image
    final_rgb = Image.frombytes('RGBA', static.size, decoded[-1].rgba).convert('RGB').tobytes()
    squared = sum((a-b)**2 for a, b in zip(reference, final_rgb))
    psnr = 10 * math.log10(255**2 * len(reference) / squared) if squared else math.inf
    return data, decoded, psnr
