"""Read packed Source VTX geometry and material replacement tables."""

from __future__ import annotations

import struct
from bisect import bisect_right

from .constants import (
    MAX_NUM_BONES_PER_VERT,
    SOURCE_STRIP_GROUP_DELTA_FIXED,
    SOURCE_STRIP_GROUP_FLEXED,
)
from .reader import BinaryReader, SEEK_BEGIN
from .vtx_data import (
    SourceVtxBodyPart07,
    SourceVtxBoneStateChange07,
    SourceVtxFileData07,
    SourceVtxMaterialReplacement07,
    SourceVtxMaterialReplacementList07,
    SourceVtxMesh07,
    SourceVtxModel07,
    SourceVtxModelLod07,
    SourceVtxStrip07,
    SourceVtxStripGroup07,
    SourceVtxVertex07,
)

__all__ = ["SourceVtxFile07", "VtxValidationError"]


class VtxValidationError(ValueError):
    """A fatal structural error in an existing VTX companion."""

    def __init__(self,message,*,field=None,offset=None):
        super().__init__(message)
        self.field,self.offset=field,offset


class _VtxInputValidator:
    """Validate packed tables without allocating the geometry object tree.

    Limits cover total records, table visits, and string bytes, including aliases.
    Physical bounds alone do not limit repeated traversal of overlapping tables.
    """

    MAX_RECORDS = 2_000_000
    MAX_TABLES = 100_000
    MAX_STRING_BYTES = 16 * 1024 * 1024

    def __init__(self, parser, mdl=None, vvd=None):
        self.parser, self.reader = parser, parser.theInputFileReader
        self.data, self.base = parser.theVtxFileData, parser.theVtxFileOffsetStart
        self.mdl, self.vvd = mdl, vvd
        self.records = self.strings = 0
        self.intervals = []
        self.names = []

    def fail(self, field, position, detail):
        raise VtxValidationError(
            f"VTX 结构无效: {self.reader.path or '<buffer>'}: "
            f"{field}（字段偏移 {position} / 0x{position:X}）: {detail}",
            field=field,offset=position,
        )

    def unpack(self, fmt, position):
        return struct.unpack(fmt, self.reader.slice(position, struct.calcsize(fmt)))

    def table(self, base, count, offset, size, field, count_position, offset_position=None):
        offset_field = field.replace('Count', 'Offset')
        if offset_position is None:
            offset_position = count_position + 4
        if count < 0:
            self.fail(field, count_position, f"数量 {count} 不能为负数")
        if not count:
            return range(0)
        start = base + offset
        if offset == 0 or start < self.base + 36 or start > self.reader.length:
            self.fail(offset_field, offset_position, f"相对偏移 {offset} 指向无效位置 {start}")
        if count > (self.reader.length - start) // size:
            self.fail(field, count_position,
                      f"数量 {count} × 记录大小 {size} 超出文件边界（表位置 {start}，文件大小 {self.reader.length}）")
        self.records += count
        if self.records > self.MAX_RECORDS or len(self.intervals) >= self.MAX_TABLES:
            self.fail(field, count_position, "超过 VTX 解析总工作预算")
        self.intervals.append((start, start + count * size, field, count_position))
        return range(start, start + count * size, size)

    def same(self, actual, expected, field, position):
        if actual != expected:
            self.fail(field, position, f"数量 {actual} 与关联 MDL/VVD 的 {expected} 不一致")

    def header(self):
        data = self.data
        if data.version not in (6, 7):
            self.fail('version', self.base, f"不支持的版本 {data.version}")
        if not 0 <= data.lodCount <= 8 or (data.bodyPartCount and data.lodCount == 0):
            self.fail('lodCount', self.base + 20, f"LOD 数量 {data.lodCount} 无效")
        self.table(self.base, data.bodyPartCount, data.bodyPartOffset, 8,
                   'bodyPartCount', self.base + 28)
        if data.materialReplacementListOffset:
            self.table(self.base, data.lodCount, data.materialReplacementListOffset, 8,
                       'materialReplacementListCount', self.base + 20, self.base + 24)
        if self.mdl is not None:
            self.same(data.bodyPartCount, self.mdl.bodyPartCount, 'bodyPartCount', self.base + 28)
            self.same(data.checksum, self.mdl.checksum, 'checksum', self.base + 16)
        if self.vvd is not None:
            self.same(data.checksum, self.vvd.checksum, 'checksum', self.base + 16)
            if data.lodCount > self.vvd.lodCount:
                self.fail('lodCount', self.base + 20, "超过关联 VVD 的 LOD 数量")

    def validate(self, extra):
        self.header()
        data = self.data
        bodies = self.mdl.theBodyParts if self.mdl is not None else None
        if bodies is not None:
            self.same(len(bodies), data.bodyPartCount, 'bodyPartCount', self.base + 28)
        # Header validation already registered these two top-level tables.
        for bi in range(data.bodyPartCount):
            body = self.base + data.bodyPartOffset + bi * 8
            count, offset = self.unpack('<ii', body)
            mdl_body = bodies[bi] if bodies is not None else None
            if mdl_body is not None:
                self.same(count, mdl_body.modelCount, 'modelCount', body)
            for mi, model in enumerate(self.table(body, count, offset, 8, 'modelCount', body)):
                lods, lod_offset = self.unpack('<ii', model)
                self.same(lods, data.lodCount, 'lodCount', model)
                models = mdl_body.theModels if mdl_body is not None else None
                if models is not None:
                    self.same(len(models), count, 'modelCount', body)
                mdl_model = models[mi] if models is not None else None
                for lod in self.table(model, lods, lod_offset, 12, 'lodCount', model):
                    meshes, mesh_offset = self.unpack('<ii', lod)
                    if mdl_model is not None:
                        self.same(meshes, mdl_model.meshCount, 'meshCount', lod)
                    for mesh_i, mesh in enumerate(self.table(lod, meshes, mesh_offset, 9, 'meshCount', lod)):
                        mdl_meshes = mdl_model.theMeshes if mdl_model is not None else None
                        if mdl_meshes is not None:
                            self.same(len(mdl_meshes), meshes, 'meshCount', lod)
                        mdl_mesh = mdl_meshes[mesh_i] if mdl_meshes is not None else None
                        groups, group_offset = self.unpack('<ii', mesh)
                        for group in self.table(mesh, groups, group_offset, 25 + extra, 'stripGroupCount', mesh):
                            vc, vo, ic, io, sc, so = self.unpack('<6i', group)
                            vertices = self.table(group, vc, vo, 9, 'vertexCount', group)
                            indexes = self.table(group, ic, io, 2, 'indexCount', group + 8)
                            strips = self.table(group, sc, so, 27 + extra, 'stripCount', group + 16)
                            if extra:
                                tc, to = self.unpack('<ii', group + 25)
                                self.table(group, tc, to, 2, 'topologyIndexCount', group + 25)
                            for vertex in vertices:
                                original, = self.unpack('<H', vertex + 4)
                                if mdl_mesh is not None and original >= mdl_mesh.vertexCount:
                                    self.fail('originalMeshVertexIndex', vertex + 4, "超过关联 MDL mesh 顶点数量")
                                if self.vvd is not None and mdl_model is not None and mdl_mesh is not None:
                                    # MDL model vertexOffset is a byte offset into 48-byte VVD vertices.
                                    absolute = mdl_model.vertexOffset // 48 + mdl_mesh.vertexIndexStart + original
                                    if not 0 <= absolute < self.vvd.lodVertexCount[0]:
                                        self.fail('originalMeshVertexIndex', vertex + 4, "超过关联 VVD 顶点数量")
                            for index in indexes:
                                value, = self.unpack('<H', index)
                                if value >= vc:
                                    self.fail('vertexIndex', index, f"索引 {value} 超过 strip group 顶点数量 {vc}")
                            for strip in strips:
                                sic, sio, svc, svo, bones, flags, changes, change_offset = self.unpack('<4ihBii', strip)
                                for value, name, field_offset in (
                                    (sic, 'indexCount', 0), (sio, 'indexMeshIndex', 4),
                                    (svc, 'vertexCount', 8), (svo, 'vertexMeshIndex', 12),
                                    (bones, 'boneCount', 16),
                                ):
                                    if value < 0:
                                        self.fail(name, strip + field_offset, f"strip 字段值 {value} 不能为负数")
                                if sio > ic or sic > ic - sio:
                                    self.fail('indexCount', strip, "strip 的索引范围越界")
                                if svo > vc or svc > vc - svo:
                                    self.fail('vertexCount', strip + 8, "strip 的顶点范围越界")
                                self.table(strip, changes, change_offset, 8, 'boneStateChangeCount', strip + 19)
        self.materials()
        tables = sorted(self.intervals)
        previous_end = self.base + 36
        for start, end, field, position in tables:
            if start < previous_end:
                self.fail(field, position, f"数据表在位置 {start} 与其他表重叠")
            previous_end = end
        starts = [table[0] for table in tables]
        for start, end, position in self.names:
            index = bisect_right(starts, start)
            if ((index and tables[index - 1][1] > start)
                    or (index < len(tables) and tables[index][0] < end)):
                self.fail('nameOffset', position, "材质名称与数据表重叠")

    def materials(self):
        data = self.data
        if not data.materialReplacementListOffset:
            return
        for li in range(data.lodCount):
            pos = self.base + data.materialReplacementListOffset + li * 8
            count, offset = self.unpack('<ii', pos)
            for replacement in self.table(pos, count, offset, 6, 'replacementCount', pos):
                index, name_offset = self.unpack('<hi', replacement)
                name = replacement + name_offset
                if not name_offset or not self.base + 36 <= name < self.reader.length:
                    self.fail('nameOffset', replacement + 2, f"字符串偏移 {name_offset} 无效")
                if self.mdl is not None and not 0 <= index < self.mdl.textureCount:
                    self.fail('materialIndex', replacement, f"材质索引 {index} 超出关联 MDL")
                # Scan bounded chunks, so long/shared strings also consume a budget.
                cursor = name
                while cursor < self.reader.length:
                    size = min(4096, self.reader.length - cursor)
                    chunk = self.reader.slice(cursor, size)
                    terminator = chunk.find(b'\0')
                    consumed = terminator + 1 if terminator >= 0 else size
                    self.strings += consumed
                    if self.strings > self.MAX_STRING_BYTES:
                        self.fail('nameOffset', replacement + 2, "超过 VTX 字符串总工作预算")
                    if terminator >= 0:
                        self.names.append((name, cursor + consumed, replacement + 2))
                        break
                    cursor += size
                else:
                    self.fail('nameOffset', replacement + 2, "材质名称缺少 NUL 结束符")


class SourceVtxFile07:
    """Implement ``SourceVtxFile07``."""

    def __init__(
        self, reader: BinaryReader, vtx_file_data: SourceVtxFileData07, vtx_file_offset_start: int = 0
    ) -> None:
        self.theInputFileReader = reader
        self.theVtxFileOffsetStart = vtx_file_offset_start
        self.theVtxFileData = vtx_file_data

        # Layout is selected by bounded preflight instead of speculative parsing.
        self.theStripGroupAndStripUseExtraFields: bool = False
        self._input_validated = False
        self.recovery_records = []

    def validate_input(self, mdl_data=None, vvd_data=None) -> None:
        """Check both Source layouts before any geometry records are allocated."""
        self._input_validated=False
        error=self._validate_layouts(mdl_data,vvd_data)
        if error is None:return
        original=self.theVtxFileData.bodyPartCount
        if (error.field!='bodyPartCount' or mdl_data is None
                or original==mdl_data.bodyPartCount or not self._recovery_evidence(mdl_data,vvd_data)):
            raise error
        # Replace only the parsed count. All source bytes and relative offsets
        # remain untouched; the candidate must pass every normal geometry gate.
        self.theVtxFileData.bodyPartCount=mdl_data.bodyPartCount
        try:
            candidate_error=self._validate_layouts(mdl_data,vvd_data)
            if candidate_error is not None:
                raise VtxValidationError(str(error)+'；MDL 引导恢复未通过: '+str(candidate_error),
                                         field=candidate_error.field,offset=candidate_error.offset)
        except BaseException:
            self.theVtxFileData.bodyPartCount=original
            self._input_validated=False
            raise
        self.recovery_records.append(dict(path=self.theInputFileReader.path,field='bodyPartCount',
            offset=self.theVtxFileOffsetStart+28,original=original,recovered=mdl_data.bodyPartCount,
            source='MDL',validation='VTX bounds, MDL hierarchy, VVD linkage',original_error=str(error)))

    @staticmethod
    def _recovery_evidence(mdl,vvd):
        count=mdl.bodyPartCount;bodies=mdl.theBodyParts
        if type(count) is not int or count<1 or bodies is None or len(bodies)!=count:return False
        for body in bodies:
            models=body.theModels
            if body.modelCount<0 or body.modelCount!=len(models or []):return False
            for model in models or []:
                if model.meshCount<0 or model.meshCount!=len(model.theMeshes or []):return False
        return SourceVtxFile07._vvd_mapping_valid(vvd)

    @staticmethod
    def _vvd_fixup_count_allowed(vvd):
        return 0<=vvd.fixupCount<=_VtxInputValidator.MAX_RECORDS

    @staticmethod
    def _vvd_mapping_valid(vvd):
        if (vvd is None or vvd.id!='IDSV' or vvd.version!=4 or not 1<=vvd.lodCount<=8
                or not SourceVtxFile07._vvd_fixup_count_allowed(vvd)):
            return False
        counts=vvd.lodVertexCount[:vvd.lodCount]
        if len(counts)!=vvd.lodCount or any(n<0 or n>counts[0] for n in counts):return False
        if not vvd.fixupCount:return True
        fixups=vvd.theFixups
        if fixups is None or len(fixups)!=vvd.fixupCount:return False
        for fixup in fixups:
            if (not 0<=fixup.lodIndex<vvd.lodCount or fixup.vertexIndex<0 or fixup.vertexCount<0
                    or fixup.vertexIndex+fixup.vertexCount>counts[0]):return False
        return all(sum(f.vertexCount for f in fixups if f.lodIndex>=lod)==count
                   for lod,count in enumerate(counts))

    def _validate_layouts(self,mdl_data,vvd_data):
        first_error = None
        for extra in (0, 8):
            try:
                _VtxInputValidator(self, mdl_data, vvd_data).validate(extra)
            except VtxValidationError as error:
                if first_error is None:
                    first_error = error
            else:
                self.theStripGroupAndStripUseExtraFields = bool(extra)
                self._input_validated = True
                return None
        return first_error

    # -- header --------------------------------------------------------------
    def read_source_vtx_header(self, *, mdl_data=None, vvd_data=None) -> None:
        """Implement ``ReadSourceVtxHeader``."""
        reader = self.theInputFileReader
        data = self.theVtxFileData
        if reader.length - reader.position < 36:
            _VtxInputValidator(self).fail('header', reader.position, "文件头不足 36 字节")

        data.version = reader.read_int32()
        data.vertexCacheSize = reader.read_int32()
        data.maxBonesPerStrip = reader.read_uint16()
        data.maxBonesPerTri = reader.read_uint16()
        data.maxBonesPerVertex = reader.read_int32()
        data.checksum = reader.read_int32()
        data.lodCount = reader.read_int32()
        data.materialReplacementListOffset = reader.read_int32()
        data.bodyPartCount = reader.read_int32()
        data.bodyPartOffset = reader.read_int32()
        self._input_validated = False
        self.recovery_records=[]
        if mdl_data is None:_VtxInputValidator(self).header()
        else:self.validate_input(mdl_data,vvd_data)

    # -- body parts ----------------------------------------------------------
    def read_source_vtx_body_parts(self) -> None:
        """Read the already validated geometry using its selected packed layout."""
        if not self._input_validated:
            self.validate_input()
        if not self.theVtxFileData.bodyPartCount:
            return
        self.theInputFileReader.seek(
            self.theVtxFileOffsetStart + self.theVtxFileData.bodyPartOffset, SEEK_BEGIN
        )
        self._read_source_vtx_body_parts_internal()

    def _read_source_vtx_body_parts_internal(self) -> None:
        reader = self.theInputFileReader
        self.theVtxFileData.theVtxBodyParts = []
        for _ in range(self.theVtxFileData.bodyPartCount):
            body_part_pos = reader.position
            a_body_part = SourceVtxBodyPart07()

            a_body_part.modelCount = reader.read_int32()
            a_body_part.modelOffset = reader.read_int32()

            self.theVtxFileData.theVtxBodyParts.append(a_body_part)

            input_pos = reader.position
            self._read_source_vtx_models(body_part_pos, a_body_part)
            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_models(self, body_part_pos: int, a_body_part: SourceVtxBodyPart07) -> None:
        if a_body_part.modelCount <= 0 or a_body_part.modelOffset == 0:
            return

        reader = self.theInputFileReader
        reader.seek(body_part_pos + a_body_part.modelOffset, SEEK_BEGIN)

        a_body_part.theVtxModels = []
        for _ in range(a_body_part.modelCount):
            model_pos = reader.position
            a_model = SourceVtxModel07()

            a_model.lodCount = reader.read_int32()
            a_model.lodOffset = reader.read_int32()

            a_body_part.theVtxModels.append(a_model)

            input_pos = reader.position
            if a_model.lodCount > 0 and a_model.lodOffset != 0:
                self._read_source_vtx_model_lods(model_pos, a_model)
            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_model_lods(self, model_pos: int, a_model: SourceVtxModel07) -> None:
        reader = self.theInputFileReader
        reader.seek(model_pos + a_model.lodOffset, SEEK_BEGIN)

        a_model.theVtxModelLods = []
        for _ in range(a_model.lodCount):
            model_lod_pos = reader.position
            a_model_lod = SourceVtxModelLod07()

            a_model_lod.meshCount = reader.read_int32()
            a_model_lod.meshOffset = reader.read_int32()
            a_model_lod.switchPoint = reader.read_single()
            a_model_lod.theVtxModelLodUsesFacial = False

            a_model.theVtxModelLods.append(a_model_lod)

            input_pos = reader.position
            if a_model_lod.meshCount > 0 and a_model_lod.meshOffset != 0:
                self._read_source_vtx_meshes(model_lod_pos, a_model_lod)
            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_meshes(self, model_lod_pos: int, a_model_lod: SourceVtxModelLod07) -> None:
        reader = self.theInputFileReader
        reader.seek(model_lod_pos + a_model_lod.meshOffset, SEEK_BEGIN)

        a_model_lod.theVtxMeshes = []
        for _ in range(a_model_lod.meshCount):
            mesh_pos = reader.position
            a_mesh = SourceVtxMesh07()

            a_mesh.stripGroupCount = reader.read_int32()
            a_mesh.stripGroupOffset = reader.read_int32()
            a_mesh.flags = reader.read_byte()

            a_model_lod.theVtxMeshes.append(a_mesh)

            input_pos = reader.position

            if a_mesh.stripGroupCount > 0 and a_mesh.stripGroupOffset != 0:
                self._read_source_vtx_strip_groups(mesh_pos, a_model_lod, a_mesh)

            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_strip_groups(
        self, mesh_pos: int, a_model_lod: SourceVtxModelLod07, a_mesh: SourceVtxMesh07
    ) -> None:
        reader = self.theInputFileReader
        reader.seek(mesh_pos + a_mesh.stripGroupOffset, SEEK_BEGIN)

        a_mesh.theVtxStripGroups = []
        for _ in range(a_mesh.stripGroupCount):
            strip_group_pos = reader.position
            a_strip_group = SourceVtxStripGroup07()

            a_strip_group.vertexCount = reader.read_int32()
            a_strip_group.vertexOffset = reader.read_int32()
            a_strip_group.indexCount = reader.read_int32()
            a_strip_group.indexOffset = reader.read_int32()
            a_strip_group.stripCount = reader.read_int32()
            a_strip_group.stripOffset = reader.read_int32()
            a_strip_group.flags = reader.read_byte()

            if self.theStripGroupAndStripUseExtraFields:
                a_strip_group.topologyIndexCount = reader.read_int32()
                a_strip_group.topologyIndexOffset = reader.read_int32()

            a_mesh.theVtxStripGroups.append(a_strip_group)

            input_pos = reader.position

            if a_strip_group.vertexCount > 0 and a_strip_group.vertexOffset != 0:
                self._read_source_vtx_vertexes(strip_group_pos, a_strip_group)
            if a_strip_group.indexCount > 0 and a_strip_group.indexOffset != 0:
                self._read_source_vtx_indexes(strip_group_pos, a_strip_group)
            if a_strip_group.stripCount > 0 and a_strip_group.stripOffset != 0:
                self._read_source_vtx_strips(strip_group_pos, a_strip_group)
            if self.theStripGroupAndStripUseExtraFields:
                if (
                    a_strip_group.topologyIndexCount > 0
                    and a_strip_group.topologyIndexOffset != 0
                ):
                    self._read_source_vtx_topology_indexes(strip_group_pos, a_strip_group)

            # Marks whether this LOD uses facial (flex) vertexes.
            if (a_strip_group.flags & SOURCE_STRIP_GROUP_FLEXED) > 0 or (
                a_strip_group.flags & SOURCE_STRIP_GROUP_DELTA_FIXED
            ) > 0:
                a_model_lod.theVtxModelLodUsesFacial = True

            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_vertexes(
        self, strip_group_pos: int, a_strip_group: SourceVtxStripGroup07
    ) -> None:
        reader = self.theInputFileReader
        reader.seek(strip_group_pos + a_strip_group.vertexOffset, SEEK_BEGIN)

        a_strip_group.theVtxVertexes = []
        for _ in range(a_strip_group.vertexCount):
            a_vertex = SourceVtxVertex07()

            for i in range(MAX_NUM_BONES_PER_VERT):
                a_vertex.boneWeightIndex[i] = reader.read_byte()
            a_vertex.boneCount = reader.read_byte()
            a_vertex.originalMeshVertexIndex = reader.read_uint16()
            for i in range(MAX_NUM_BONES_PER_VERT):
                a_vertex.boneId[i] = reader.read_byte()

            a_strip_group.theVtxVertexes.append(a_vertex)

    def _read_source_vtx_indexes(
        self, strip_group_pos: int, a_strip_group: SourceVtxStripGroup07
    ) -> None:
        reader = self.theInputFileReader
        reader.seek(strip_group_pos + a_strip_group.indexOffset, SEEK_BEGIN)

        a_strip_group.theVtxIndexes = []
        for _ in range(a_strip_group.indexCount):
            a_strip_group.theVtxIndexes.append(reader.read_uint16())

    def _read_source_vtx_strips(self, strip_group_pos: int, a_strip_group: SourceVtxStripGroup07) -> None:
        reader = self.theInputFileReader
        reader.seek(strip_group_pos + a_strip_group.stripOffset, SEEK_BEGIN)

        a_strip_group.theVtxStrips = []
        for _ in range(a_strip_group.stripCount):
            a_strip = SourceVtxStrip07()

            a_strip.indexCount = reader.read_int32()
            a_strip.indexMeshIndex = reader.read_int32()
            a_strip.vertexCount = reader.read_int32()
            a_strip.vertexMeshIndex = reader.read_int32()
            a_strip.boneCount = reader.read_int16()
            a_strip.flags = reader.read_byte()
            a_strip.boneStateChangeCount = reader.read_int32()
            a_strip.boneStateChangeOffset = reader.read_int32()
            if self.theStripGroupAndStripUseExtraFields:
                a_strip.unknownBytes01 = reader.read_int32()
                a_strip.unknownBytes02 = reader.read_int32()

            a_strip_group.theVtxStrips.append(a_strip)

            # NOTE: the parser deliberately does not read the bone state changes here
            # (it used a lot of memory and mis-read alyx_sfm.mdl).
            reader.seek(reader.position, SEEK_BEGIN)

    def _read_source_vtx_topology_indexes(
        self, strip_group_pos: int, a_strip_group: SourceVtxStripGroup07
    ) -> None:
        reader = self.theInputFileReader
        reader.seek(strip_group_pos + a_strip_group.topologyIndexOffset, SEEK_BEGIN)

        a_strip_group.theVtxTopologyIndexes = []
        for _ in range(a_strip_group.topologyIndexCount):
            a_strip_group.theVtxTopologyIndexes.append(reader.read_uint16())

    def _read_source_vtx_bone_state_changes(self, strip_pos: int, a_strip: SourceVtxStrip07) -> None:
        """Implement ``ReadSourceVtxBoneStateChanges`` (not called by the default path)."""
        if (a_strip.flags & 1) == 0 or (a_strip.flags & 4) > 0:
            return

        bone_state_change_count = a_strip.boneStateChangeCount
        if a_strip.boneCount == 0 and a_strip.boneStateChangeOffset != 0:
            bone_state_change_count = 1

        if bone_state_change_count <= 0 or a_strip.boneStateChangeOffset == 0:
            return

        reader = self.theInputFileReader
        reader.seek(strip_pos + a_strip.boneStateChangeOffset, SEEK_BEGIN)
        a_strip.theVtxBoneStateChanges = []
        for _ in range(bone_state_change_count):
            a_bone_state_change = SourceVtxBoneStateChange07()
            a_bone_state_change.hardwareId = reader.read_int32()
            a_bone_state_change.newBoneId = reader.read_int32()
            a_strip.theVtxBoneStateChanges.append(a_bone_state_change)

    # -- material replacements ----------------------------------------------
    def read_source_vtx_material_replacement_lists(self) -> None:
        """Implement ``ReadSourceVtxMaterialReplacementLists``."""
        if not self._input_validated:
            self.validate_input()
        data = self.theVtxFileData
        if data.materialReplacementListOffset == 0:
            return

        reader = self.theInputFileReader
        reader.seek(self.theVtxFileOffsetStart + data.materialReplacementListOffset, SEEK_BEGIN)

        data.theVtxMaterialReplacementLists = []
        for _ in range(data.lodCount):
            list_pos = reader.position
            a_list = SourceVtxMaterialReplacementList07()

            a_list.replacementCount = reader.read_int32()
            a_list.replacementOffset = reader.read_int32()

            data.theVtxMaterialReplacementLists.append(a_list)

            input_pos = reader.position
            self._read_source_vtx_material_replacements(list_pos, a_list)
            reader.seek(input_pos, SEEK_BEGIN)

    def _read_source_vtx_material_replacements(
        self, list_pos: int, a_list: SourceVtxMaterialReplacementList07
    ) -> None:
        if a_list.replacementCount <= 0 or a_list.replacementOffset == 0:
            return

        reader = self.theInputFileReader
        reader.seek(list_pos + a_list.replacementOffset, SEEK_BEGIN)

        a_list.theVtxMaterialReplacements = []
        for _ in range(a_list.replacementCount):
            replacement_pos = reader.position
            a_replacement = SourceVtxMaterialReplacement07()

            a_replacement.materialIndex = reader.read_int16()
            a_replacement.nameOffset = reader.read_int32()

            a_list.theVtxMaterialReplacements.append(a_replacement)

            input_pos = reader.position
            if a_replacement.nameOffset != 0:
                reader.seek(replacement_pos + a_replacement.nameOffset, SEEK_BEGIN)
                a_replacement.theName = reader.read_null_terminated_string()
            reader.seek(input_pos, SEEK_BEGIN)

    def read_unread_bytes(self) -> None:
        """Implement ``ReadUnreadBytes`` (only feeds the debug seek log)."""
        return
