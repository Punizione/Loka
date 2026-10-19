"""Read-only LOD0 geometry and compiled sequence previews, without GUI dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import struct
import weakref

from .action_pose import PoseEvaluator, multiply, world_matrices
from .core.mdl_data import SourceMdlFileData49
from .decompiler import MdlDecompiler, DecompileOptions, detect_mdl_version
from .core.constants import MDL_VERSIONS_USING_SOURCE_MODEL_49


@dataclass(frozen=True)
class PreviewMesh:
    material: str
    positions: tuple
    normals: tuple
    uvs: tuple
    bone_indices: tuple
    bone_weights: tuple
    indices: tuple[int, ...]


@dataclass(frozen=True)
class PreviewPose:
    bone_matrices: tuple[tuple[float, ...], ...]
    attachments: dict[str, tuple[float, ...]]
    # Keep bone-to-world separate from world * inverse-bind skinning matrices.
    # Bone-merged models have their own bind space and must use the former.
    bone_world_matrices: tuple[tuple[float, ...], ...] = ()


@dataclass(frozen=True)
class SequenceTimeline:
    """Neutral compiled sequence clock; FPS and duration are independent sums."""
    frame_count: int
    fps: float
    duration: float


def _validate_mdl_ranges(path):
    """Bound header-driven allocations before invoking permissive legacy readers."""
    size = path.stat().st_size
    if size < 408 or size > 512 * 1024 * 1024:
        raise ValueError('MDL file size is outside preview limits')
    with path.open('rb') as handle:
        header = handle.read(408)
        if struct.unpack_from('<i', header, 76)[0] != size:
            raise ValueError('MDL declared file length does not match file')
        # Minimum strides, shared by the supported Source 49-family readers.
        for offset, stride in ((156, 176), (164, 56), (172, 12), (180, 100),
                               (188, 176), (204, 64), (212, 4), (232, 16),
                               (240, 92), (260, 4), (268, 20), (276, 12),
                               (284, 16), (292, 20), (300, 20), (320, 12),
                               (336, 8), (352, 8), (384, 20)):
            count, start = struct.unpack_from('<ii', header, offset)
            if count < 0 or count > 100000 or (count and (start < 408 or start+count*stride > size)):
                raise ValueError(f'MDL table at {offset} exceeds file boundary/preview limits')
        animations, offset = struct.unpack_from('<ii', header, 180)
        for index in range(animations):
            position = offset+index*100
            handle.seek(position); record = handle.read(100)
            frames = struct.unpack_from('<i', record, 16)[0]
            section_offset, section_frames = struct.unpack_from('<ii', record, 80)
            if not 0 <= frames <= 1000000 or section_frames < 0:
                raise ValueError('MDL animation frame count exceeds preview limits')
            if section_offset:
                count = frames//section_frames+2 if section_frames else 0
                if count > 100000 or not section_frames or section_offset < 0 or position+section_offset+count*8 > size:
                    raise ValueError('MDL animation sections exceed file boundary/preview limits')


def build_meshes(data, vvd, vtx):
    """Flatten default bodygroups, preserving VVD skin weights and VTX topology."""
    if vvd is None or vtx is None:
        return ()
    vertices = vvd.theFixedVertexesByLod[0] if vvd.fixupCount else vvd.theVertexes
    if vertices is None:
        raise ValueError('VVD LOD0 vertex data is unavailable')
    textures = data.theTextures or []
    skin = (data.theSkinFamilies or [[]])[0]
    result = []; model_start = 0
    parts = data.theBodyParts or []
    if len(vtx.theVtxBodyParts or []) != len(parts):
        raise ValueError('MDL/VTX bodypart count mismatch')
    for part_index, part in enumerate(parts):
        models = part.theModels or []
        if not models:
            continue
        model = models[0]
        vtx_models = vtx.theVtxBodyParts[part_index].theVtxModels or []
        if len(vtx_models) != len(models):
            raise ValueError('MDL/VTX model count mismatch')
        lods = vtx_models[0].theVtxModelLods or []
        meshes = model.theMeshes or []
        vtx_meshes = lods[0].theVtxMeshes or [] if lods else []
        if len(meshes) != len(vtx_meshes):
            raise ValueError('MDL/VTX mesh count mismatch')
        for mesh, optimized in zip(meshes, vtx_meshes):
            positions, normals, uvs, bones, weights, indices = [], [], [], [], [], []
            mapped = {}
            def vertex(index):
                if not 0 <= index < mesh.vertexCount:
                    raise ValueError('VTX mesh vertex exceeds MDL mesh boundary')
                source = model_start + mesh.vertexIndexStart + index
                if not 0 <= source < len(vertices):
                    raise ValueError('VTX vertex exceeds VVD boundary')
                if source in mapped:
                    return mapped[source]
                v = vertices[source]; bw = v.boneWeight
                p = (v.positionX, v.positionY, v.positionZ)
                n = (v.normalX, v.normalY, v.normalZ); uv = (v.texCoordX, v.texCoordY)
                if not all(math.isfinite(x) for x in (*p, *n, *uv, *bw.weight)):
                    raise ValueError('Nonfinite VVD vertex/weight')
                if not 1 <= bw.boneCount <= 3:
                    raise ValueError('Unsupported VVD bone influence count')
                w = tuple(bw.weight[i] if i < bw.boneCount else 0. for i in range(3))
                if any(x < 0 for x in w) or sum(w) <= 0:
                    raise ValueError('Invalid VVD bone weights')
                b = tuple(bw.bone[i] if i < bw.boneCount else 0 for i in range(3))
                if any(weight > 0 and not 0 <= bone < len(data.theBones or []) for bone, weight in zip(b, w)):
                    raise ValueError('VVD bone index exceeds MDL bone table')
                mapped[source] = len(positions)
                positions.append(p); normals.append(n); uvs.append(uv)
                bones.append(b); weights.append(w)
                return mapped[source]
            for group in optimized.theVtxStripGroups or []:
                refs, raw_indices = group.theVtxVertexes or [], group.theVtxIndexes or []
                for strip in group.theVtxStrips or []:
                    start, count = strip.indexMeshIndex, strip.indexCount
                    if start < 0 or count < 0 or start+count > len(raw_indices):
                        raise ValueError('VTX strip index range exceeds strip group')
                    sequence = raw_indices[start:start+count]
                    if any(i < 0 or i >= len(refs) for i in sequence):
                        raise ValueError('VTX index exceeds strip-group vertex table')
                    if strip.flags & 1:
                        if count % 3:
                            raise ValueError('VTX triangle list has incomplete triangle')
                        triangles = [sequence[i:i+3] for i in range(0, count, 3)]
                    elif strip.flags & 2:
                        triangles = [(sequence[i+1], sequence[i], sequence[i+2]) if i % 2
                                     else sequence[i:i+3] for i in range(count-2)]
                    else:
                        raise ValueError('Unknown VTX strip topology')
                    for triangle in triangles:
                        if len(set(triangle)) == 3:
                            indices.extend(vertex(refs[i].originalMeshVertexIndex) for i in triangle)
            material_index = mesh.materialIndex
            if skin:
                if not 0 <= material_index < len(skin):
                    raise ValueError('MDL mesh material exceeds skin table')
                material_index = skin[material_index]
            if not 0 <= material_index < len(textures):
                raise ValueError('MDL material exceeds texture table')
            if indices:
                result.append(PreviewMesh(textures[material_index].thePathFileName, tuple(positions), tuple(normals),
                                          tuple(uvs), tuple(bones), tuple(weights), tuple(indices)))
        # All alternate bodygroup vertices occupy space, even though only the
        # default model contributes drawn triangles.
        model_start += sum(m.vertexCount for m in models)
    return tuple(result)


class PreviewModel:
    def __init__(self, data, meshes=(), warnings=()):
        self.data = data
        self.meshes = tuple(meshes)
        self.warnings = list(warnings)
        warning_target=weakref.WeakMethod(self._warn)
        def report(text):
            callback=warning_target()
            if callback is not None:callback(text)
        self._evaluator=PoseEvaluator(data,report)
        self._inverse_bind = tuple(tuple(value for j in range(4)
                                        for value in (*getattr(b, 'poseToBoneColumn'+str(j)).as_tuple(), float(j == 3)))
                                   for b in data.theBones or [])

    def _warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    @classmethod
    def load(cls, model_path):
        path = Path(model_path).resolve(strict=True)
        if path.suffix.lower() != '.mdl':
            raise ValueError('Expected a compiled .mdl model')
        version = detect_mdl_version(str(path))
        if version not in MDL_VERSIONS_USING_SOURCE_MODEL_49:
            raise ValueError('Unsupported compiled model version: ' + str(version))
        _validate_mdl_ranges(path)
        # Only read APIs: no decompile(), post-decompile writers, global config,
        # subtract correction setup, filesystem staging, or include copying.
        reader = MdlDecompiler(DecompileOptions(handle_vmodel_includes=False))
        reader.mdl_file_data = SourceMdlFileData49()
        reader._read_mdl_file(str(path))
        d = reader.mdl_file_data
        if d.fileSize != path.stat().st_size:
            raise ValueError('MDL declared file length does not match file')
        if len(d.theBones or []) != d.boneCount or len(d.theSequenceDescs or []) != d.localSequenceCount:
            raise ValueError('Compiled MDL bone/sequence table is incomplete')
        if d.animBlockCount:
            ani = path.with_suffix('.ani')
            external = any(a.animBlock > 0 or any(s.animBlock > 0 for s in a.theSections or [])
                           for a in d.theAnimationDescs or [])
            if external and not ani.exists():
                raise ValueError('External compiled animation blocks require the matching ANI companion')
            if ani.exists():
                with ani.open('rb') as handle: header = handle.read(12)
                if len(header) < 12 or header[:4] != b'IDAG' or struct.unpack_from('<i', header, 8)[0] != d.checksum:
                    raise ValueError('ANI header/checksum does not match MDL')
        reader._read_ani_file(str(path))
        if any('Failed to read ANI' in w for w in reader.result.warnings):
            raise ValueError('Compiled ANI data is incomplete: ' + '; '.join(reader.result.warnings))
        # VTX preflight validates companion VVD ranges and fixup mapping before
        # the existing VVD reader allocates and reads its arrays.
        reader._read_vtx_file(str(path))
        vvd_path = path.with_suffix('.vvd')
        if vvd_path.exists():
            with vvd_path.open('rb') as handle: header = handle.read(64)
            if len(header) < 64 or header[:4] != b'IDSV' or struct.unpack_from('<ii', header, 4) != (4, d.checksum):
                raise ValueError('VVD header/checksum does not match MDL')
        reader._read_vvd_file(str(path))
        has_geometry = any(m.vertexCount for p in d.theBodyParts or [] for m in p.theModels or [])
        if has_geometry and (reader.vtx_file_data is None or reader.vvd_file_data is None):
            raise ValueError('Compiled model geometry requires matching VVD and VTX companions')
        meshes = build_meshes(d, reader.vvd_file_data, reader.vtx_file_data)
        return cls(d, meshes, reader.result.warnings)

    def frame_count(self, sequence_index):
        return self._evaluator.frame_count(sequence_index)

    def fps(self, sequence_index):
        return self._evaluator.fps(sequence_index)

    def timeline(self, sequence_index, rate_scale=1.0):
        """Source MaxFrame/FPS/Duration at the evaluator's neutral pose blend.

        Duration is inverse weighted cycles-per-second, not weighted frames
        divided by weighted FPS. Proposed speed scaling follows the retimer:
        moving references and every occurrence of the primary descriptor scale;
        other single-frame reference metadata stay unchanged.
        Neither compiled data nor the legacy frame-count/sample APIs are changed.
        """
        rate_scale = float(rate_scale)
        if not math.isfinite(rate_scale) or rate_scale <= 0:
            raise ValueError('Timeline rate scale must be finite and positive')
        evaluator = self._evaluator
        sequence = evaluator.checked_sequence(sequence_index)
        primary_index = (sequence.theAnimDescIndexes or [None])[0]
        weighted_frames = fps = cps = 0.
        moving = False
        for index, weight in evaluator._grid(sequence):
            animation = evaluator._animation(index)
            count, rate = animation.frameCount, animation.fps
            if count > 1 or index == primary_index:
                rate *= rate_scale
            if count > 1:
                moving = True
                cps += weight*rate/(count-1)
            weighted_frames += weight*count
            fps += weight*rate
        if not all(math.isfinite(value) and value >= 0 for value in (weighted_frames, fps, cps)):
            raise ValueError('Invalid compiled sequence clock')
        # Studio_MaxFrame truncates (weighted numframes - 1 + .01).
        # Approved single-frame UI policy avoids its all-static off-by-one.
        maximum = weighted_frames-1 if weighted_frames > 1 else weighted_frames
        count = int(maximum+.01)+1 if moving else 1
        return SequenceTimeline(count, fps, 1/cps if cps > 0 else 0.)

    def sample(self, sequence_index, frame, *, frame_span=None):
        """Sample a compiled frame, or map an explicit UI frame span to cycle.

        The UI may time the sequence by its primary QC animation reference,
        whose frame count need not match every blend-grid reference. Supplying
        ``frame_span=primary_frame_count - 1`` preserves its complete cycle.
        """
        frame = float(frame)
        if not math.isfinite(frame):
            raise ValueError('Animation frame must be finite')
        if frame_span is not None:
            span = float(frame_span)
            if not math.isfinite(span) or span < 0:
                raise ValueError('Animation frame span must be finite and nonnegative')
            frame = frame / span * max(0, self.frame_count(sequence_index)-1) if span else 0.
        local = self._evaluator.sample(sequence_index, frame)
        world = world_matrices(self.data.theBones or [], local)
        skin = tuple(multiply(w, inverse) for w, inverse in zip(world, self._inverse_bind))
        attachments = {}
        for a in self.data.theAttachments or []:
            if not 0 <= a.localBoneIndex < len(world):
                raise ValueError('Attachment references an invalid bone')
            local_matrix = tuple(getattr(a, 'localM'+str(r)+str(c)) if r < 4 else float(c == 4)
                                 for c in range(1, 5) for r in range(1, 5))
            attachments[a.theName] = multiply(world[a.localBoneIndex], local_matrix)
        camera_names = ('attach_camera', 'camera', 'valvebiped.camera')
        if not any(name.casefold() in camera_names for name in attachments):
            # The fallback is the evaluated bone world transform, never its
            # skinning matrix; an actual attachment always wins across aliases.
            named_bones = {b.theName.casefold(): (i, b.theName)
                           for i, b in enumerate(self.data.theBones or [])}
            for name in camera_names:
                if name in named_bones:
                    index, original = named_bones[name]
                    attachments['camera'] = world[index]
                    attachments.setdefault(original, world[index])
                    break
        if not all(math.isfinite(x) for m in (*skin, *attachments.values()) for x in m):
            raise ValueError('Nonfinite compiled model pose')
        return PreviewPose(skin, attachments, world)
