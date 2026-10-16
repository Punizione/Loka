"""Read Source VVD vertex data and vertex-fixup tables."""

from __future__ import annotations

from typing import Optional

from .constants import MAX_NUM_BONES_PER_VERT, MAX_NUM_LODS
from .reader import BinaryReader, SEEK_BEGIN
from .vectors import SourceVector4D

__all__ = [
    "SourceBoneWeight",
    "SourceVertex",
    "SourceVvdExtraData04",
    "SourceVvdFileData04",
    "SourceVvdFixup04",
    "SourceVvdFile04",
]


class SourceBoneWeight:
    """``SourceBoneWeight`` -- up to three bone indices and their weights."""

    def __init__(self) -> None:
        self.weight: list[float] = [0.0] * MAX_NUM_BONES_PER_VERT
        self.bone: list[int] = [0] * MAX_NUM_BONES_PER_VERT
        self.boneCount: int = 0


class SourceVertex:
    """``SourceVertex`` -- one entry of the VVD vertex array."""

    def __init__(self) -> None:
        self.boneWeight: SourceBoneWeight = SourceBoneWeight()
        self.positionX: float = 0.0
        self.positionY: float = 0.0
        self.positionZ: float = 0.0
        self.normalX: float = 0.0
        self.normalY: float = 0.0
        self.normalZ: float = 0.0
        self.texCoordX: float = 0.0
        self.texCoordY: float = 0.0


class SourceTextureCoordinates:
    """``SourceTextureCoordinates`` -- one (u, v) pair of an extra UV channel.

    NOTE: The VB declaration is not captured by the class inventory because the
    type is declared in ``SourceTextureCoordinates``; the field names used by
    ``SourceVvdFile04.ReadExtraData`` are ``X`` and ``Y``.
    """

    def __init__(self) -> None:
        self.X: float = 0.0
        self.Y: float = 0.0


class SourceVvdFixup04:
    """``SourceVvdFixup04`` -- one ``vertexFileFixup_t``."""

    def __init__(self) -> None:
        self.lodIndex: int = 0
        self.vertexIndex: int = 0
        self.vertexCount: int = 0


class SourceVvdExtraData04:
    """``SourceVvdExtraData04`` -- one extra vertex attribute chunk (e.g. UV2)."""

    def __init__(self) -> None:
        self.type: int = 0
        self.offset: int = 0
        self.bytesPerVertex: int = 0
        self.theTextureCoordinates: Optional[list[SourceTextureCoordinates]] = None


class SourceVvdFileData04:
    """``SourceVvdFileData04`` -- VVD header plus parsed arrays."""

    def __init__(self) -> None:
        self.id: str = ""
        self.version: int = 0
        self.checksum: int = 0
        self.lodCount: int = 0
        self.lodVertexCount: list[int] = [0] * MAX_NUM_LODS
        self.fixupCount: int = 0
        self.fixupTableOffset: int = 0
        self.vertexDataOffset: int = 0
        self.tangentDataOffset: int = 0
        self.theExtraDatas: Optional[list[SourceVvdExtraData04]] = None
        self.theFixedVertexesByLod: list[Optional[list[SourceVertex]]] = [None] * MAX_NUM_LODS
        self.theFixups: Optional[list[SourceVvdFixup04]] = None
        self.theTangents: Optional[list[SourceVector4D]] = None
        self.theVertexes: Optional[list[SourceVertex]] = None


class SourceVvdFile04:
    """Implement ``SourceVvdFile04``."""

    def __init__(
        self,
        reader: BinaryReader,
        vvd_file_data: SourceVvdFileData04,
        vvd_file_offset_start: int = 0,
    ) -> None:
        self.theInputFileReader = reader
        self.theVvdFileOffsetStart = vvd_file_offset_start
        self.theVvdFileData = vvd_file_data

    # -- header --------------------------------------------------------------
    def read_source_vvd_header(self) -> None:
        """Implement ``ReadSourceVvdHeader``."""
        reader = self.theInputFileReader
        data = self.theVvdFileData

        data.id = reader.read_chars(4)
        data.version = reader.read_int32()
        data.checksum = reader.read_int32()
        data.lodCount = reader.read_int32()
        for i in range(MAX_NUM_LODS):
            data.lodVertexCount[i] = reader.read_int32()
        data.fixupCount = reader.read_int32()
        data.fixupTableOffset = reader.read_int32()
        data.vertexDataOffset = reader.read_int32()
        data.tangentDataOffset = reader.read_int32()

    # -- vertices ------------------------------------------------------------
    def read_vertexes(self, mdl_version: int = 0) -> None:
        """Implement ``ReadVertexes``.

        ``lodVertexCount[0]`` is the count of *all* vertices stored in the file.
        MDL versions 54..59 carry four extra floats per vertex, which are skipped.
        """
        data = self.theVvdFileData
        if data.lodCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(self.theVvdFileOffsetStart + data.vertexDataOffset, SEEK_BEGIN)

        vertex_count = data.lodVertexCount[0]
        data.theVertexes = []
        for _ in range(vertex_count):
            a_studio_vertex = SourceVertex()

            bone_weight = SourceBoneWeight()
            for x in range(MAX_NUM_BONES_PER_VERT):
                bone_weight.weight[x] = reader.read_single()
            for x in range(MAX_NUM_BONES_PER_VERT):
                bone_weight.bone[x] = reader.read_byte()
            bone_weight.boneCount = reader.read_byte()
            a_studio_vertex.boneWeight = bone_weight

            a_studio_vertex.positionX = reader.read_single()
            a_studio_vertex.positionY = reader.read_single()
            a_studio_vertex.positionZ = reader.read_single()
            a_studio_vertex.normalX = reader.read_single()
            a_studio_vertex.normalY = reader.read_single()
            a_studio_vertex.normalZ = reader.read_single()
            a_studio_vertex.texCoordX = reader.read_single()
            a_studio_vertex.texCoordY = reader.read_single()

            if 54 <= mdl_version <= 59:
                reader.read_single()
                reader.read_single()
                reader.read_single()
                reader.read_single()

            data.theVertexes.append(a_studio_vertex)

    # -- tangents ------------------------------------------------------------
    def read_tangents(self) -> None:
        """Implement ``ReadTangents`` (debug data; not needed for decompiling)."""
        data = self.theVvdFileData
        if data.lodCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(self.theVvdFileOffsetStart + data.tangentDataOffset, SEEK_BEGIN)

        vertex_count = data.lodVertexCount[0]
        data.theTangents = []
        for _ in range(vertex_count):
            a_vector = SourceVector4D()
            a_vector.x = reader.read_single()
            a_vector.y = reader.read_single()
            a_vector.z = reader.read_single()
            a_vector.w = reader.read_single()
            data.theTangents.append(a_vector)

    # -- fixups --------------------------------------------------------------
    def read_fixups(self) -> None:
        """Implement ``ReadFixups``."""
        data = self.theVvdFileData
        if data.fixupCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(self.theVvdFileOffsetStart + data.fixupTableOffset, SEEK_BEGIN)

        data.theFixups = []
        for _ in range(data.fixupCount):
            a_fixup = SourceVvdFixup04()
            a_fixup.lodIndex = reader.read_int32()
            a_fixup.vertexIndex = reader.read_int32()
            a_fixup.vertexCount = reader.read_int32()
            data.theFixups.append(a_fixup)

        if data.lodCount > 0:
            reader.seek(self.theVvdFileOffsetStart + data.vertexDataOffset, SEEK_BEGIN)
            for lod_index in range(data.lodCount):
                self._setup_fixed_vertexes(lod_index)

    def _setup_fixed_vertexes(self, lod_index: int) -> None:
        """Implement ``SetupFixedVertexes``."""
        data = self.theVvdFileData
        try:
            fixed: list[SourceVertex] = []
            for a_fixup in data.theFixups or []:
                if a_fixup.lodIndex >= lod_index:
                    for j in range(a_fixup.vertexCount):
                        fixed.append(data.theVertexes[a_fixup.vertexIndex + j])
            data.theFixedVertexesByLod[lod_index] = fixed
        except MemoryError:
            raise
        except Exception:
            pass

    # -- extra data (second UV channel) -------------------------------------
    def read_extra_data(self) -> None:
        """Implement ``ReadExtraData`` -- the optional extra UV channel for LOD 0."""
        data = self.theVvdFileData
        if data.lodCount <= 0:
            return

        reader = self.theInputFileReader
        try:
            # "* 16" is sizeof(SourceVector4D) -- the tangent array size.
            extra_data_offset = (
                self.theVvdFileOffsetStart
                + data.tangentDataOffset
                + data.lodVertexCount[0] * 16
            )
            if extra_data_offset >= reader.length:
                return  # no extra data stored

            reader.seek(extra_data_offset, SEEK_BEGIN)

            extra_data_count = reader.read_int32()
            reader.read_int32()  # extraDataByteCount -- unused

            vertex_count = data.lodVertexCount[0]
            data.theExtraDatas = []
            for _ in range(extra_data_count):
                an_extra_data = SourceVvdExtraData04()

                an_extra_data.type = reader.read_int32()
                an_extra_data.offset = reader.read_int32()
                an_extra_data.bytesPerVertex = reader.read_int32()
                data.theExtraDatas.append(an_extra_data)

                an_extra_data.theTextureCoordinates = []
                for _ in range(vertex_count):
                    a_texture_coordinates = SourceTextureCoordinates()
                    a_texture_coordinates.X = reader.read_single()
                    a_texture_coordinates.Y = reader.read_single()
                    an_extra_data.theTextureCoordinates.append(a_texture_coordinates)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_unread_bytes(self) -> None:
        """Implement ``ReadUnreadBytes`` (only feeds the debug seek log)."""
        return
