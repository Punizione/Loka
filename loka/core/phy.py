"""Read collision solids and constraints; export transformed physics meshes."""

from __future__ import annotations

import math
import struct
from typing import Callable, Optional

from .constants import STUDIOHDR_FLAGS_STATIC_PROP
from .numfmt import fmt_fixed6
from .reader import BinaryReader, SEEK_BEGIN
from .vectors import SourceVector, degrees_to_radians

__all__ = [
    # data classes (the parser field names are preserved)
    "FileSeekLog",
    "SourcePhyAnimatedFrictionSection",
    "SourcePhyCollisionData",
    "SourcePhyCollisionPair",
    "SourcePhyConvexMesh",
    "SourcePhyEditParamsSection",
    "SourcePhyFace",
    "SourcePhyFileData",
    "SourcePhyPhysCollisionModel",
    "SourcePhyRagdollConstraint",
    "SourcePhyVertex",
    # KeyValues helper layer
    "read_key_value_line",
    "read_text_line",
    "read_phy_collision_text_section",
    # parser
    "SourcePhyFile",
    # transform helpers
    "angle_matrix",
    "cross_product",
    "matrix_invert",
    "vector_i_transform",
    "vector_normalize",
    "vector_transform",
    "process_transforms_for_physics",
    "transform_phy_vertex",
    # writer
    "write_triangles_section_for_physics",
]


# ---------------------------------------------------------------------------
# KeyValues / text helper layer
# ---------------------------------------------------------------------------

def read_text_line(reader: BinaryReader) -> Optional[str]:
    """Implement ``FileManager.ReadTextLine``.

    Reads characters until LF (``0x0A``) or NUL (``0x00``); the terminator is
    consumed.  Returns ``None`` when the line was empty **or** when the read ran
    past the end of the stream (the VB ``Catch`` swallows the exception).
    """
    chars: list[str] = []
    try:
        while True:
            a_char = reader.read_char()
            if a_char == "\x00" or a_char == "\x0a":
                break
            chars.append(a_char)
    except MemoryError:
        raise
    except Exception:
        # VB: Catch ex As Exception : Dim debug As Integer = 4242
        pass

    if len(chars) > 0:
        return "".join(chars)
    return None


def read_key_value_line(reader: BinaryReader) -> tuple[Optional[str], Optional[str], bool]:
    """Implement ``FileManager.ReadKeyValueLine`` -- returns ``(key, value, result)``.

    The ``ByRef oKey`` / ``ByRef oValue`` parameters become the first two tuple
    elements.  The line is split on ``"`` with empty entries removed:

    * 3 tokens -> ``key = tokens[0]``, ``value = tokens[2]``, ``True``
    * 2 tokens -> ``key = tokens[0]``, ``value = tokens[1]``, ``True``
    * 1 token  -> ``key = value = tokens[0]``, ``False`` (block terminator)
    * ``None`` line -> ``key = value = None``, ``False``
    """
    line = read_text_line(reader)
    if line is not None:
        tokens = [token for token in line.split('"') if token != ""]
        if len(tokens) == 3:
            return tokens[0], tokens[2], True
        if len(tokens) == 2:
            return tokens[0], tokens[1], True
        if len(tokens) == 1:
            return tokens[0], tokens[0], False

    return line, line, False


def read_phy_collision_text_section(reader: BinaryReader, end_offset: int) -> str:
    """Implement ``Common.ReadPhyCollisionTextSection``.

    Reads everything up to (but not including) the byte at ``end_offset``,
    consumes one further character (the NUL terminator) and then truncates at the
    first NUL.  If no NUL is present the VB ``Substring(0, -1)`` throws, the
    ``Catch`` swallows it and the *untruncated* value is returned.
    """
    result = ""
    try:
        stream_last_position = end_offset

        if stream_last_position > reader.position:
            # NOTE: Use -1 to avoid including the null terminator character.
            result = reader.read_chars(int(stream_last_position - reader.position - 1))
            # Read the NULL byte to help with debug logging.
            reader.read_char()
            # Only grab text to the first NULL byte.
            null_index = result.find("\x00")
            if null_index < 0:
                # VB: result.Substring(0, -1) raises ArgumentOutOfRangeException.
                raise ValueError("no NUL terminator in the collision text section")
            result = result[:null_index]
    except MemoryError:
        raise
    except Exception:
        # VB: Catch ex As Exception : Dim debug As Integer = 4242
        pass

    return result


def _parse_single(value: str) -> float:
    """``Single.Parse(value, TheApp.InternalNumberFormat)`` -- rounded to float32."""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _format_six_decimals(value: float) -> str:
    """``value.ToString("0.000000", TheApp.InternalNumberFormat)``.

    Delegates to :func:`loka.core.numfmt.fmt_fixed6` rather than Python's
    ``".6f"``, because .NET Framework rounds **half away from zero** and **drops the
    sign of a value that rounds to zero** (so ``-0.0`` prints ``0.000000``, and
    ``-1e-7`` prints ``0.000000`` too).  Python's ``format`` does neither: it rounds
    half to even and would emit ``-0.000000``.

    Every float the physics writer emits comes from a VB ``Double`` field
    (``SourceVector`` components, ``SourcePhyVertex.Normal``), so the exact-value
    variant is the right one here.
    """
    return fmt_fixed6(value)


def _parse_int(value: str) -> int:
    """``Integer.Parse(value, TheApp.InternalNumberFormat)``.

    Python's ``int()`` additionally accepts surrounding whitespace, which VB also
    does, and ``+``/``-`` prefixes, which VB allows too.
    """
    return int(value)


# ---------------------------------------------------------------------------
# FileSeekLog -- the diagnostic byte-range log the parser fills while parsing
# ---------------------------------------------------------------------------

class FileSeekLog:
    """
    Only the parts the PHY parser touches are ported, but the semantics are the
    original ones: the two ``SortedList`` collections become ``dict`` instances
    that are kept in ascending key order, duplicate ``startOffset`` with a
    different ``endOffset`` prefixes ``"[ERROR] "``, and exact duplicates append
    ``"; " + description``.
    """

    def __init__(self) -> None:
        self.theFileSize: int = 0
        self.theFileSeekList: dict[int, int] = {}
        self.theFileSeekDescriptionList: dict[int, str] = {}

    @property
    def file_size(self) -> int:
        """``FileSeekLog.FileSize``."""
        return self.theFileSize

    @file_size.setter
    def file_size(self, value: int) -> None:
        if self.theFileSize != value:
            self.theFileSize = value
            self.add(self.theFileSize, self.theFileSize, "END OF FILE + 1 (File size)")

    def contains_key(self, start_offset: int) -> bool:
        return start_offset in self.theFileSeekList

    def add(self, start_offset: int, end_offset: int, description: str) -> None:
        try:
            if (
                start_offset in self.theFileSeekList
                and self.theFileSeekList[start_offset] == end_offset
            ):
                self.theFileSeekDescriptionList[start_offset] += "; " + description
            elif start_offset in self.theFileSeekList:
                temp = self.theFileSeekDescriptionList[start_offset]
                self.theFileSeekDescriptionList[start_offset] = "[ERROR] "
                self.theFileSeekDescriptionList[start_offset] += (
                    temp + "; [" + str(start_offset) + " - " + str(end_offset) + "] " + description
                )
            else:
                self.theFileSeekList[start_offset] = end_offset
                self.theFileSeekDescriptionList[start_offset] = description
        except MemoryError:
            raise
        except Exception:
            # VB: Catch ex As Exception : Dim debug As Integer = 4242
            pass

    def remove(self, start_offset: int) -> None:
        try:
            self.theFileSeekList.pop(start_offset, None)
            self.theFileSeekDescriptionList.pop(start_offset, None)
        except MemoryError:
            raise
        except Exception:
            pass

    def clear(self) -> None:
        self.theFileSeekList.clear()
        self.theFileSeekDescriptionList.clear()

    # -- diagnostic walks ----------------------------------------------------
    def log_unread_bytes(self, reader: BinaryReader) -> None:
        """Implement ``FileSeekLog.LogUnreadBytes``.

        Walks the recorded ranges in ascending order and records the gaps as
        ``[ERROR] Unread bytes`` entries.  It does **not** read the file except to
        render the byte values of the gaps.
        """
        offset_start = -1
        offset_end = -1
        temp_file_seek_list: dict[int, int] = {}
        temp_file_seek_description_list: dict[int, str] = {}

        try:
            keys = sorted(self.theFileSeekList)
            for start in keys:
                offset_start = start

                if offset_end < offset_start - 1:
                    description = "[ERROR] Unread bytes"
                    all_zeroes_were_found = False
                    byte_values = self._get_byte_values(
                        reader, offset_end + 1, offset_start - 1
                    )
                    all_zeroes_were_found = self._last_all_zeroes_were_found
                    if all_zeroes_were_found:
                        description += " (all zeroes)"
                    else:
                        description += " (non-zero)"
                    description += byte_values

                    # Can't add into the list that is being iterated, so use temp list.
                    if (offset_end + 1) in self.theFileSeekList:
                        temp = self.theFileSeekDescriptionList[offset_end + 1]
                        self.theFileSeekDescriptionList[offset_end + 1] = "[ERROR] "
                        self.theFileSeekDescriptionList[offset_end + 1] += (
                            temp
                            + "; ["
                            + str(offset_end + 1)
                            + " - "
                            + str(offset_start - 1)
                            + "] "
                            + description
                        )
                    else:
                        temp_file_seek_list[offset_end + 1] = offset_start - 1
                        temp_file_seek_description_list[offset_end + 1] = description

                offset_end = self.theFileSeekList[start]

            for start in sorted(temp_file_seek_list):
                self.add(start, temp_file_seek_list[start], temp_file_seek_description_list[start])
        except MemoryError:
            raise
        except Exception:
            # VB: Catch ex As Exception : Dim debug As Integer = 4242
            pass

        self._log_errors()

    def _log_errors(self) -> None:
        try:
            keys = sorted(self.theFileSeekList)
            for index, start in enumerate(keys):
                if index < len(keys) - 1 and self.theFileSeekList[start] + 1 != keys[index + 1]:
                    self.theFileSeekDescriptionList[start] = (
                        "[ERROR] [End offset is incorrect] "
                        + self.theFileSeekDescriptionList[start]
                    )
        except MemoryError:
            raise
        except Exception:
            pass

    def _get_byte_values(self, reader: BinaryReader, start: int, end: int) -> str:
        """Implement ``FileSeekLog.GetByteValues``.

        ``ByRef allZeroesWereFound`` is returned through the
        ``_last_all_zeroes_were_found`` attribute (the VB helper has no other way
        to return two values).
        """
        input_file_stream_position = reader.position
        byte_values = ""
        self._last_all_zeroes_were_found = False

        if (end - start) > 20:
            adjusted_end = start + 20
        else:
            adjusted_end = end

        try:
            reader.seek(start, SEEK_BEGIN)
            self._last_all_zeroes_were_found = True
            byte_values = " ["
            for offset in range(start, adjusted_end + 1):
                byte_value = reader.read_byte()
                byte_values += " " + format(byte_value, "02X")
                if byte_value != 0:
                    self._last_all_zeroes_were_found = False
            if (end - start) > 20:
                byte_values += " ..."
                # NOTE: Indicate non-zeroes if more than 20 bytes unread.
                for offset in range(adjusted_end + 1, end + 1):
                    byte_value = reader.read_byte()
                    if byte_value != 0:
                        self._last_all_zeroes_were_found = False
                        break
            byte_values += " ]"

            reader.seek(input_file_stream_position, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            self._last_all_zeroes_were_found = False
            byte_values = "[incomplete read due to error]"

        return byte_values

    # Set by ``_get_byte_values``; a plain attribute because that is how VB's
    # ``ByRef allZeroesWereFound`` behaves.
    _last_all_zeroes_were_found: bool = False


# ---------------------------------------------------------------------------
# Data classes (field names identical to the parser's)
# ---------------------------------------------------------------------------

class SourcePhyFace:
    """``SourcePhyFace`` -- one ``ledge_t`` triangle (only the 3 indices survive)."""

    def __init__(self) -> None:
        self.vertexIndex: list[int] = [0, 0, 0]


class SourcePhyVertex:
    """``SourcePhyVertex`` -- a collision vertex plus its accumulated normal.

    ``Normal`` normalizes ``theNormal`` lazily (once) and ``UnnormalizedNormal``
    is a live alias of the same list of components.
    """

    def __init__(self) -> None:
        self.vertex: SourceVector = SourceVector()
        self.theNormal: SourceVector = SourceVector()
        self.theNormalIsNormalized: bool = False

    @property
    def normal(self) -> SourceVector:
        """``SourcePhyVertex.Normal`` -- normalizes on first access."""
        if not self.theNormalIsNormalized:
            vector_normalize(self.theNormal)
            self.theNormalIsNormalized = True
        return self.theNormal

    @property
    def unnormalized_normal(self) -> SourceVector:
        """``SourcePhyVertex.UnnormalizedNormal`` -- the mutable accumulator."""
        return self.theNormal


class SourcePhyConvexMesh:
    """``SourcePhyConvexMesh`` -- one convex piece of a solid."""

    def __init__(self) -> None:
        self.flags: int = 0
        self.theBoneIndex: int = 0
        self.theFaces: list[SourcePhyFace] = []
        self.theVertices: list[SourcePhyVertex] = []


class SourcePhyCollisionData:
    """``SourcePhyCollisionData`` -- one collision lump ("solid").

    ``theVertices`` is declared and initialised by the parser but never populated and
    never read: it is dead storage, kept here so the shape matches.
    """

    def __init__(self) -> None:
        self.size: int = 0
        self.theConvexMeshes: list[SourcePhyConvexMesh] = []
        self.theVertices: list[SourcePhyVertex] = []


class SourcePhyPhysCollisionModel:
    """``SourcePhyPhysCollisionModel`` -- one ``solid { ... }`` KeyValues block."""

    def __init__(self) -> None:
        self.theIndex: int = 0
        self.theName: str = ""
        self.theParentIsValid: bool = False
        self.theParentName: str = ""
        self.theMass: float = 0.0
        self.theSurfaceProp: str = ""
        self.theDamping: float = 0.0
        self.theRotDamping: float = 0.0
        self.theDragCoefficientIsValid: bool = False
        self.theDragCoefficient: float = 0.0
        self.theInertia: float = 0.0
        self.theVolume: float = 0.0
        self.theMassBiasIsValid: bool = False
        self.theMassBias: float = 0.0
        self.theRollingDragCoefficientIsValid: bool = False
        self.theRollingDragCoefficient: float = 0.0


class SourcePhyRagdollConstraint:
    """``SourcePhyRagdollConstraint`` -- one ``ragdollconstraint { ... }`` block."""

    def __init__(self) -> None:
        self.theParentIndex: int = 0
        self.theChildIndex: int = 0
        self.theXMin: float = 0.0
        self.theXMax: float = 0.0
        self.theXFriction: float = 0.0
        self.theYMin: float = 0.0
        self.theYMax: float = 0.0
        self.theYFriction: float = 0.0
        self.theZMin: float = 0.0
        self.theZMax: float = 0.0
        self.theZFriction: float = 0.0


class SourcePhyCollisionPair:
    """``SourcePhyCollisionPair`` -- one ``"collisionpair" "a,b"`` line.

    ``name0`` / ``name1`` exist in the parser (from ``collisionpair_t``) but are never
    populated by the parser.
    """

    def __init__(self) -> None:
        self.obj0: int = 0
        self.obj1: int = 0
        self.name0: str = ""
        self.name1: str = ""


class SourcePhyAnimatedFrictionSection:
    """``SourcePhyAnimatedFrictionSection`` -- the ``animatedfriction { ... }`` block."""

    def __init__(self) -> None:
        self.animFrictionMin: float = 0.0
        self.animFrictionMax: float = 0.0
        self.animFrictionTimeIn: float = 0.0
        self.animFrictionTimeOut: float = 0.0
        self.animFrictionTimeHold: float = 0.0


class SourcePhyEditParamsSection:
    """``SourcePhyEditParamsSection`` -- the ``editparams { ... }`` block.

    ``concave`` and ``rootName`` are stored as raw strings (the QC writer compares
    ``concave`` against ``"1"``).  ``jointMergeMap`` is never populated by the parser.
    """

    def __init__(self) -> None:
        self.concave: str = ""
        self.jointMergeMap: dict[str, list[str]] = {}
        self.rootName: str = ""
        self.totalMass: float = 0.0


class SourcePhyFileData:
    """``SourcePhyFileData`` -- everything parsed out of one ``.phy`` file."""

    def __init__(self) -> None:
        # --- phyheader_t -----------------------------------------------------
        self.size: int = 0
        self.id: int = 0
        self.solidCount: int = 0
        self.checksum: int = 0

        self.thePhysicsMeshSmdFileName: str = ""
        self.theSourcePhyKeyValueDataOffset: int = 0
        # NOTE: Nothing (not an empty list) when solidCount <= 0.
        self.theSourcePhyCollisionDatas: Optional[list[SourcePhyCollisionData]] = None
        self.theSourcePhyPhysCollisionModels: Optional[
            list[SourcePhyPhysCollisionModel]
        ] = None
        self.theSourcePhyRagdollConstraintDescs: dict[int, SourcePhyRagdollConstraint] = {}
        self.theSourcePhyCollisionPairs: Optional[list[SourcePhyCollisionPair]] = None
        self.theSourcePhySelfCollides: bool = True
        self.theSourcePhyAnimatedFrictionSection: Optional[
            SourcePhyAnimatedFrictionSection
        ] = None
        self.theSourcePhyEditParamsSection: Optional[SourcePhyEditParamsSection] = None
        self.theSourcePhyPhysCollisionModelMostUsedValues: Optional[
            SourcePhyPhysCollisionModel
        ] = None
        self.theSourcePhyCollisionText: str = ""
        self.theSourcePhyMaxConvexPieces: int = 0

        # --- inherited from SourceFileData -----------------------------------
        self.theFileSeekLog: FileSeekLog = FileSeekLog()


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

class SourcePhyFile:
    """``SourcePhyFile`` -- the ``.phy`` reader.

    The third constructor argument is the parser's ``endOffset``: ``0`` means
    "standalone .phy, use the stream length" and a non-zero value is used only for
    PHY data embedded inside an MDL (MDL v53).
    """

    def __init__(
        self,
        theInputFileReader: BinaryReader,
        thePhyFileData: SourcePhyFileData,
        thePhyEndOffset: int = 0,
    ) -> None:
        self.theInputFileReader = theInputFileReader
        self.thePhyFileData = thePhyFileData
        self.thePhyEndOffset = thePhyEndOffset

        self.theDampingToCountMap: dict[float, int] = {}
        self.theInertiaToCountMap: dict[float, int] = {}
        self.theRotDampingToCountMap: dict[float, int] = {}
        self.theBoneIndexes: set[int] = set()

        self.thePhyFileData.theFileSeekLog.file_size = self.theInputFileReader.length

    # -- 1. header -----------------------------------------------------------
    def read_source_phy_header(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhyHeader`` (``phyheader_t``, 16 bytes)."""
        reader = self.theInputFileReader

        file_offset_start = reader.position

        # Offsets: 0x00, 0x04, 0x08, 0x0C (12)
        # FROM: Zoey_TeenAngst
        # 10 00 00 00
        # 00 00 00 00
        # 12 00 00 00
        # 1f de 9d 20
        self.thePhyFileData.size = reader.read_int32()
        self.thePhyFileData.id = reader.read_int32()
        self.thePhyFileData.solidCount = reader.read_int32()
        self.thePhyFileData.checksum = reader.read_int32()

        # NOTE: If header size ever increases, this will at least skip over extra stuff.
        reader.seek(file_offset_start + self.thePhyFileData.size, SEEK_BEGIN)

        file_offset_end = reader.position - 1
        self.thePhyFileData.theFileSeekLog.add(file_offset_start, file_offset_end, "Header")

    # -- 2. collision lumps --------------------------------------------------
    def read_source_collision_data(self) -> None:
        """Implement ``SourcePhyFile.ReadSourceCollisionData`` -- the solid loop.

        The next solid starts at ``solid_start + 4 + size``; everything the parser
        does not understand inside a lump is skipped by that seek.
        """
        reader = self.theInputFileReader

        self.thePhyFileData.theSourcePhyMaxConvexPieces = 0
        self.thePhyFileData.theSourcePhyCollisionDatas = []

        for solid_index in range(self.thePhyFileData.solidCount):
            collision_data = SourcePhyCollisionData()
            collision_data.theConvexMeshes = []
            collision_data.theVertices = []

            self.theBoneIndexes = set()

            file_offset_start = reader.position

            # b8 01 00 00   size
            collision_data.size = reader.read_int32()
            next_solid_data_stream_position = reader.position + collision_data.size

            phy_data_stream_position = reader.position
            # 56 50 48 59   VPHY
            vphy_id = reader.read_chars(4)
            reader.seek(phy_data_stream_position, SEEK_BEGIN)
            if vphy_id != "VPHY":
                self.read_phy_data_version37()
            else:
                self.read_phy_data_version48()

            # 49 56 50 53   IVPS
            # NOTE: This marker is read but never validated by the parser.
            reader.read_chars(4)

            vertices: list[int] = []
            vertex_data_stream_position = reader.position + collision_data.size
            while reader.position < vertex_data_stream_position:
                convex_mesh = SourcePhyConvexMesh()

                face_data_stream_position = reader.position

                # d0 00 00 00
                # 29 00 00 00
                # 04 15 00 00
                vertex_data_offset = reader.read_int32()
                vertex_data_stream_position = face_data_stream_position + vertex_data_offset

                convex_mesh.theBoneIndex = reader.read_int32() - 1
                self.theBoneIndexes.add(convex_mesh.theBoneIndex)

                # flags: has_children: (self.flags >> 0) & 3  ' 0 = false; > 0 true
                convex_mesh.flags = reader.read_int32()

                # 0c 00 00 00    count of lines after this (00 - 0b)
                triangle_count = reader.read_int32()

                for _ in range(triangle_count):
                    phy_triangle = SourcePhyFace()
                    # The first three reads are discarded (ledge_t padding).
                    reader.read_byte()
                    reader.read_byte()
                    reader.read_uint16()

                    for j in range(3):
                        phy_triangle.vertexIndex[j] = reader.read_uint16()
                        reader.read_uint16()
                        if phy_triangle.vertexIndex[j] not in vertices:
                            vertices.append(phy_triangle.vertexIndex[j])
                    convex_mesh.theFaces.append(phy_triangle)

                collision_data.theConvexMeshes.append(convex_mesh)

            if (
                self.thePhyFileData.theSourcePhyMaxConvexPieces
                < len(collision_data.theConvexMeshes)
            ):
                self.thePhyFileData.theSourcePhyMaxConvexPieces = len(
                    collision_data.theConvexMeshes
                )

            reader.seek(vertex_data_stream_position, SEEK_BEGIN)

            # Vertex data section.
            #   # 8 distinct vertices
            # ae 69 29 3b 01 6f 4d bd f6 4a 9b 3c 78 f7 18 00
            # ...
            # NOTE: the parser dereferences theConvexMeshes(0) unconditionally, so a
            #       lump with zero convex meshes raises (there is no guard).
            convex_mesh0_vertices = collision_data.theConvexMeshes[0].theVertices
            for _ in range(len(vertices)):
                phy_vertex = SourcePhyVertex()

                phy_vertex.vertex.x = reader.read_single()
                phy_vertex.vertex.y = reader.read_single()
                phy_vertex.vertex.z = reader.read_single()
                reader.read_single()  # w -- discarded (VB: Dim w As Double)

                convex_mesh0_vertices.append(phy_vertex)

            for convex_mesh_index in range(1, len(collision_data.theConvexMeshes)):
                convex_mesh = collision_data.theConvexMeshes[convex_mesh_index]
                for i in range(len(vertices)):
                    phy_vertex = SourcePhyVertex()

                    phy_vertex.vertex.x = convex_mesh0_vertices[i].vertex.x
                    phy_vertex.vertex.y = convex_mesh0_vertices[i].vertex.y
                    phy_vertex.vertex.z = convex_mesh0_vertices[i].vertex.z

                    convex_mesh.theVertices.append(phy_vertex)

            # TODO: Figure out this section.  (Section after the vertex section --
            # the compact-surface index chunk.  Deliberately not parsed.)
            # TODO: Figure out this section.  (Last section of the CollisionData --
            # the text chunk.  Deliberately not parsed.)

            self.thePhyFileData.theSourcePhyCollisionDatas.append(collision_data)

            bone_indexes_text = ""
            for bone_index in sorted(self.theBoneIndexes):
                bone_indexes_text += "  " + str(bone_index)
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(
                file_offset_start,
                file_offset_end,
                "Solid("
                + str(solid_index)
                + ")   Convex mesh count = "
                + str(len(collision_data.theConvexMeshes))
                + "   Bone indexes:"
                + bone_indexes_text,
            )

            reader.seek(next_solid_data_stream_position, SEEK_BEGIN)

        self.thePhyFileData.theSourcePhyKeyValueDataOffset = reader.position

    def read_phy_data_version37(self) -> None:
        """Implement ``SourcePhyFile.ReadPhyData_VERSION37`` -- 11 discarded int32s."""
        reader = self.theInputFileReader
        # FROM: HL2 leak - bullsquid.phy
        # 66 c5 36 bc / d8 ac 31 bd / d9 14 dd bd / 19 bd 01 3d / ae 4f 31 3d
        # 52 d0 4a 3d / e1 01 0b 3f / d8 1c 03 00 / 00 03 00 00 / 00.. / 00..
        for _ in range(11):
            reader.read_int32()

    def read_phy_data_version48(self) -> None:
        """Implement ``SourcePhyFile.ReadPhyData_VERSION48`` -- 72 bytes, all discarded."""
        reader = self.theInputFileReader

        # 56 50 48 59   VPHY
        reader.read_chars(4)

        # 00 01         version?
        # 00 00         model type?
        reader.read_uint16()
        reader.read_uint16()

        # 9c 01 00 00   surface size?
        reader.read_int32()

        # 00 00 30 3f   dragAxisAreas x?
        # 00 00 80 3f   dragAxisAreas y?
        # 00 00 80 3f   dragAxisAreas z?
        # 00 00 00 00   axisMapSize?
        reader.read_int32()
        reader.read_int32()
        reader.read_int32()
        reader.read_int32()

        # 2c fe 80 3d ... 5e 08 a8 3d / ec 9c 01 00 / 80 01 00 00 / 00 00 00 00 / 00 00 00 00
        for _ in range(11):
            reader.read_int32()

    # -- 3. normals ----------------------------------------------------------
    def calculate_vertex_normals(self) -> None:
        """Implement ``SourcePhyFile.CalculateVertexNormals``."""
        collision_datas = self.thePhyFileData.theSourcePhyCollisionDatas
        if collision_datas is not None:
            for collision_data in collision_datas:
                for face_section in collision_data.theConvexMeshes:
                    for a_triangle in face_section.theFaces:
                        self._calculate_face_normal(face_section, a_triangle)

    def _calculate_face_normal(
        self, face_section: SourcePhyConvexMesh, a_triangle: SourcePhyFace
    ) -> None:
        """Implement the private ``SourcePhyFile.CalculateFaceNormal``."""
        vertex: list[SourceVector] = [SourceVector(), SourceVector(), SourceVector()]

        for vertex_index in range(3):
            vertex[vertex_index] = face_section.theVertices[
                a_triangle.vertexIndex[vertex_index]
            ].vertex

        vector0 = SourceVector()
        vector1 = SourceVector()

        vector0.x = vertex[0].x - vertex[1].x
        vector0.y = vertex[0].y - vertex[1].y
        vector0.z = vertex[0].z - vertex[1].z

        vector1.x = vertex[1].x - vertex[2].x
        vector1.y = vertex[1].y - vertex[2].y
        vector1.z = vertex[1].z - vertex[2].z

        normal_vector = cross_product(vector0, vector1)
        # NOTE: Do not need to normalize here.  It will be normalized once after
        #       all of the normals are added together.

        for vertex_index in range(3):
            # NOTE: Instead of storing all of the normals, just store one and keep
            #       adding to it.
            phy_vertex = face_section.theVertices[a_triangle.vertexIndex[vertex_index]]
            phy_vertex.unnormalized_normal.x += normal_vector.x
            phy_vertex.unnormalized_normal.y += normal_vector.y
            phy_vertex.unnormalized_normal.z += normal_vector.z

    # -- 4. KeyValues sections ----------------------------------------------
    def read_source_phys_collision_models(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhysCollisionModels``.

        The VB ``Catch`` rethrows, so no exception is swallowed here.
        """
        reader = self.theInputFileReader

        file_offset_start = reader.position

        self.thePhyFileData.theSourcePhyPhysCollisionModels = []
        self.theDampingToCountMap = {}
        self.theInertiaToCountMap = {}
        self.theRotDampingToCountMap = {}

        there_is_a_value = True
        key: Optional[str] = ""
        value: Optional[str] = ""
        line: Optional[str] = None

        while True:  # VB: Do ... Loop Until line Is Nothing
            a_source_phys_collision_model = SourcePhyPhysCollisionModel()
            temp_stream_offset = reader.position
            line = read_text_line(reader)
            if line is None or line != "solid {":
                reader.seek(temp_stream_offset, SEEK_BEGIN)
                break

            while there_is_a_value:
                key, value, there_is_a_value = read_key_value_line(reader)
                if there_is_a_value:
                    if key == "index":
                        a_source_phys_collision_model.theIndex = _parse_int(value or "")
                    elif key == "name":
                        a_source_phys_collision_model.theName = value or ""
                    elif key == "parent":
                        a_source_phys_collision_model.theParentIsValid = True
                        a_source_phys_collision_model.theParentName = value or ""
                    elif key == "mass":
                        a_source_phys_collision_model.theMass = _parse_single(value or "")
                    elif key == "surfaceprop":
                        a_source_phys_collision_model.theSurfaceProp = value or ""
                    elif key == "damping":
                        a_source_phys_collision_model.theDamping = _parse_single(value or "")
                        if a_source_phys_collision_model.theDamping in self.theDampingToCountMap:
                            self.theDampingToCountMap[
                                a_source_phys_collision_model.theDamping
                            ] += 1
                        else:
                            self.theDampingToCountMap[
                                a_source_phys_collision_model.theDamping
                            ] = 1
                    elif key == "rotdamping":
                        a_source_phys_collision_model.theRotDamping = _parse_single(value or "")
                        if (
                            a_source_phys_collision_model.theRotDamping
                            in self.theRotDampingToCountMap
                        ):
                            self.theRotDampingToCountMap[
                                a_source_phys_collision_model.theRotDamping
                            ] += 1
                        else:
                            self.theRotDampingToCountMap[
                                a_source_phys_collision_model.theRotDamping
                            ] = 1
                    elif key == "drag":
                        a_source_phys_collision_model.theDragCoefficientIsValid = True
                        a_source_phys_collision_model.theDragCoefficient = _parse_single(
                            value or ""
                        )
                    elif key == "rollingDrag":
                        a_source_phys_collision_model.theRollingDragCoefficientIsValid = True
                        a_source_phys_collision_model.theRollingDragCoefficient = _parse_single(
                            value or ""
                        )
                    elif key == "inertia":
                        a_source_phys_collision_model.theInertia = _parse_single(value or "")
                        if a_source_phys_collision_model.theInertia in self.theInertiaToCountMap:
                            self.theInertiaToCountMap[
                                a_source_phys_collision_model.theInertia
                            ] += 1
                        else:
                            self.theInertiaToCountMap[
                                a_source_phys_collision_model.theInertia
                            ] = 1
                    elif key == "volume":
                        a_source_phys_collision_model.theVolume = _parse_single(value or "")
                    elif key == "massbias":
                        a_source_phys_collision_model.theMassBiasIsValid = True
                        a_source_phys_collision_model.theMassBias = _parse_single(value or "")

            # NOTE: Above while loop should return the ending brace.
            if key is None or key != "}":
                break
            self.thePhyFileData.theSourcePhyPhysCollisionModels.append(
                a_source_phys_collision_model
            )
            there_is_a_value = True

            if line is None:
                break

        # Most-used-values reduction.  ``max_value`` is a single shared local that
        # is NOT re-initialised between the three loops (VB default ``0.0``), and
        # the maps are iterated in ascending key order with a ``<=`` comparison.
        max_value = 0.0
        max_count = 0
        self.thePhyFileData.theSourcePhyPhysCollisionModelMostUsedValues = (
            SourcePhyPhysCollisionModel()
        )
        for damping in sorted(self.theDampingToCountMap):
            if max_count <= self.theDampingToCountMap[damping]:
                max_value = damping
                max_count = self.theDampingToCountMap[damping]
        self.thePhyFileData.theSourcePhyPhysCollisionModelMostUsedValues.theDamping = max_value

        max_count = 0
        for inertia in sorted(self.theInertiaToCountMap):
            if max_count <= self.theInertiaToCountMap[inertia]:
                max_value = inertia
                max_count = self.theInertiaToCountMap[inertia]
        self.thePhyFileData.theSourcePhyPhysCollisionModelMostUsedValues.theInertia = max_value

        max_count = 0
        for rot_damping in sorted(self.theRotDampingToCountMap):
            if max_count <= self.theRotDampingToCountMap[rot_damping]:
                max_value = rot_damping
                max_count = self.theRotDampingToCountMap[rot_damping]
        self.thePhyFileData.theSourcePhyPhysCollisionModelMostUsedValues.theRotDamping = max_value

        file_offset_end = reader.position - 1
        self.thePhyFileData.theFileSeekLog.add(file_offset_start, file_offset_end, "Properties")

    def read_source_phy_ragdoll_constraint_descs(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhyRagdollConstraintDescs``.

        The result is keyed by ``theChildIndex``; the VB ``SortedList.Add`` throws
        on a duplicate key, which is reproduced here with an explicit check because
        a Python ``dict`` would silently overwrite.
        """
        reader = self.theInputFileReader

        file_offset_start = reader.position

        self.thePhyFileData.theSourcePhyRagdollConstraintDescs = {}

        there_is_a_value = True
        key: Optional[str] = ""
        value: Optional[str] = ""
        line: Optional[str] = None

        while True:  # VB: Do ... Loop Until line Is Nothing
            a_source_ragdoll_constraint_desc = SourcePhyRagdollConstraint()
            temp_stream_offset = reader.position
            line = read_text_line(reader)
            if line is None or line != "ragdollconstraint {":
                reader.seek(temp_stream_offset, SEEK_BEGIN)
                break

            while there_is_a_value:
                key, value, there_is_a_value = read_key_value_line(reader)
                if there_is_a_value:
                    if key == "parent":
                        a_source_ragdoll_constraint_desc.theParentIndex = _parse_int(value or "")
                    elif key == "child":
                        a_source_ragdoll_constraint_desc.theChildIndex = _parse_int(value or "")
                    elif key == "xmin":
                        a_source_ragdoll_constraint_desc.theXMin = _parse_single(value or "")
                    elif key == "xmax":
                        a_source_ragdoll_constraint_desc.theXMax = _parse_single(value or "")
                    elif key == "xfriction":
                        a_source_ragdoll_constraint_desc.theXFriction = _parse_single(value or "")
                    elif key == "ymin":
                        a_source_ragdoll_constraint_desc.theYMin = _parse_single(value or "")
                    elif key == "ymax":
                        a_source_ragdoll_constraint_desc.theYMax = _parse_single(value or "")
                    elif key == "yfriction":
                        a_source_ragdoll_constraint_desc.theYFriction = _parse_single(value or "")
                    elif key == "zmin":
                        a_source_ragdoll_constraint_desc.theZMin = _parse_single(value or "")
                    elif key == "zmax":
                        a_source_ragdoll_constraint_desc.theZMax = _parse_single(value or "")
                    elif key == "zfriction":
                        a_source_ragdoll_constraint_desc.theZFriction = _parse_single(value or "")

            # NOTE: Above while loop should return the ending brace.
            if key is None or key != "}":
                break
            child_index = a_source_ragdoll_constraint_desc.theChildIndex
            if child_index in self.thePhyFileData.theSourcePhyRagdollConstraintDescs:
                # VB: SortedList.Add throws ArgumentException for a duplicate key.
                raise ValueError(
                    "duplicate ragdollconstraint child index: " + str(child_index)
                )
            self.thePhyFileData.theSourcePhyRagdollConstraintDescs[child_index] = (
                a_source_ragdoll_constraint_desc
            )
            there_is_a_value = True

            if line is None:
                break

        if file_offset_start < reader.position:
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(
                file_offset_start, file_offset_end, "Ragdoll constraints"
            )

    def read_source_phy_collision_rules(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhyCollisionRules``.

        ``selfcollisions`` ignores its value entirely -- its mere presence sets
        ``theSourcePhySelfCollides = False``.
        """
        reader = self.theInputFileReader

        file_offset_start = reader.position

        self.thePhyFileData.theSourcePhyCollisionPairs = []

        there_is_a_value = True
        key: Optional[str] = ""
        value: Optional[str] = ""
        line: Optional[str] = None

        while True:  # VB: Do ... Loop Until line Is Nothing
            temp_stream_offset = reader.position
            line = read_text_line(reader)
            if line is None or line != "collisionrules {":
                reader.seek(temp_stream_offset, SEEK_BEGIN)
                break

            while there_is_a_value:
                key, value, there_is_a_value = read_key_value_line(reader)
                if there_is_a_value:
                    if key == "collisionpair":
                        tokens = [token for token in (value or "").split(",") if token != ""]
                        if len(tokens) == 2:
                            a_source_phy_collision_pair = SourcePhyCollisionPair()
                            a_source_phy_collision_pair.obj0 = _parse_int(tokens[0])
                            a_source_phy_collision_pair.obj1 = _parse_int(tokens[1])
                            self.thePhyFileData.theSourcePhyCollisionPairs.append(
                                a_source_phy_collision_pair
                            )
                    elif key == "selfcollisions":
                        self.thePhyFileData.theSourcePhySelfCollides = False

            # NOTE: Above while loop should return the ending brace.
            if key is None or key != "}":
                break
            there_is_a_value = True

            if line is None:
                break

        if file_offset_start < reader.position:
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(
                file_offset_start, file_offset_end, "Collision rules"
            )

    def read_source_phy_animated_friction_section(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhyAnimatedFrictionSection``.

        The section object is allocated before the loop, so it is non-``None`` even
        when no block is present.  This method's ``Catch`` swallows exceptions.
        """
        reader = self.theInputFileReader

        file_offset_start = reader.position

        try:
            there_is_a_value = True
            key: Optional[str] = ""
            value: Optional[str] = ""
            line: Optional[str] = None
            self.thePhyFileData.theSourcePhyAnimatedFrictionSection = (
                SourcePhyAnimatedFrictionSection()
            )

            while True:  # VB: Do ... Loop Until line Is Nothing
                temp_stream_offset = reader.position
                line = read_text_line(reader)
                if line is None or line != "animatedfriction {":
                    reader.seek(temp_stream_offset, SEEK_BEGIN)
                    break

                while there_is_a_value:
                    key, value, there_is_a_value = read_key_value_line(reader)
                    if there_is_a_value:
                        section = self.thePhyFileData.theSourcePhyAnimatedFrictionSection
                        if key == "animfrictionmin":
                            section.animFrictionMin = _parse_single(value or "")
                        elif key == "animfrictionmax":
                            section.animFrictionMax = _parse_single(value or "")
                        elif key == "animfrictiontimein":
                            section.animFrictionTimeIn = _parse_single(value or "")
                        elif key == "animfrictiontimeout":
                            section.animFrictionTimeOut = _parse_single(value or "")
                        elif key == "animfrictiontimehold":
                            section.animFrictionTimeHold = _parse_single(value or "")

                # NOTE: Above while loop should return the ending brace.
                if key is None or key != "}":
                    break
                there_is_a_value = True

                if line is None:
                    break
        except MemoryError:
            raise
        except Exception:
            # VB: Catch ex As Exception : Dim debug As Integer = 4242
            pass

        if file_offset_start < reader.position:
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(
                file_offset_start, file_offset_end, "Animated Friction"
            )

    def read_source_phy_edit_params_section(self) -> None:
        """Implement ``SourcePhyFile.ReadSourcePhyEditParamsSection``.

        ``rootname`` and ``concave`` are stored as raw strings; ``totalmass`` is a
        float32.  This method's ``Catch`` swallows exceptions.
        """
        reader = self.theInputFileReader

        file_offset_start = reader.position

        try:
            there_is_a_value = True
            key: Optional[str] = ""
            value: Optional[str] = ""
            line: Optional[str] = None
            self.thePhyFileData.theSourcePhyEditParamsSection = SourcePhyEditParamsSection()

            while True:  # VB: Do ... Loop Until line Is Nothing
                temp_stream_offset = reader.position
                line = read_text_line(reader)
                if line is None or line != "editparams {":
                    reader.seek(temp_stream_offset, SEEK_BEGIN)
                    break

                while there_is_a_value:
                    key, value, there_is_a_value = read_key_value_line(reader)
                    if key == "rootname":
                        there_is_a_value = True
                        if key != value:
                            self.thePhyFileData.theSourcePhyEditParamsSection.rootName = (
                                value or ""
                            )
                            # Else: (the parser leaves rootName empty)
                    elif there_is_a_value:
                        section = self.thePhyFileData.theSourcePhyEditParamsSection
                        if key == "concave":
                            section.concave = value or ""
                        elif key == "totalmass":
                            section.totalMass = _parse_single(value or "")

                # NOTE: Above while loop should return the ending brace.
                if key is None or key != "}":
                    break
                there_is_a_value = True

                if line is None:
                    break
        except MemoryError:
            raise
        except Exception:
            # VB: Catch ex As Exception : Dim debug As Integer = 4242
            pass

        if file_offset_start < reader.position:
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(file_offset_start, file_offset_end, "Edit params")

    def read_collision_text_section(self) -> None:
        """Implement ``SourcePhyFile.ReadCollisionTextSection``."""
        reader = self.theInputFileReader

        file_offset_start = reader.position

        try:
            if self.thePhyEndOffset == 0:
                end_offset = reader.length - 1
            else:
                end_offset = self.thePhyEndOffset

            self.thePhyFileData.theSourcePhyCollisionText = read_phy_collision_text_section(
                reader, end_offset
            )
        except MemoryError:
            raise
        except Exception:
            # VB: Catch ex As Exception : Dim debug As Integer = 4242
            pass

        if file_offset_start < reader.position:
            file_offset_end = reader.position - 1
            self.thePhyFileData.theFileSeekLog.add(
                file_offset_start, file_offset_end, "Collision text"
            )

    def read_unread_bytes(self) -> None:
        """Implement ``SourcePhyFile.ReadUnreadBytes`` -- purely diagnostic."""
        self.thePhyFileData.theFileSeekLog.log_unread_bytes(self.theInputFileReader)


# ---------------------------------------------------------------------------
# Matrix / vector helpers (MathModule)
# ---------------------------------------------------------------------------

def cross_product(vector1: SourceVector, vector2: SourceVector) -> SourceVector:
    """Implement ``SourceVector.CrossProduct``."""
    return SourceVector(
        vector1.y * vector2.z - vector1.z * vector2.y,
        vector1.z * vector2.x - vector1.x * vector2.z,
        vector1.x * vector2.y - vector1.y * vector2.x,
    )


def dot_product(vector1: SourceVector, vector2: SourceVector) -> float:
    """Implement ``MathModule.DotProduct``."""
    return vector1.x * vector2.x + vector1.y * vector2.y + vector1.z * vector2.z


def vector_normalize(io_vector: SourceVector) -> float:
    """Implement ``MathModule.VectorNormalize`` -- in place, returns the length.

    A zero-length vector is left untouched (the original returns early).
    """
    length = 0.0
    length += io_vector.x * io_vector.x
    length += io_vector.y * io_vector.y
    length += io_vector.z * io_vector.z
    length = math.sqrt(length)
    if length == 0:
        return 0

    io_vector.x /= length
    io_vector.y /= length
    io_vector.z /= length

    return length


def angle_matrix(
    pitch_radians: float,
    yaw_radians: float,
    roll_radians: float,
    matrix_column0: SourceVector,
    matrix_column1: SourceVector,
    matrix_column2: SourceVector,
    matrix_column3: SourceVector,
) -> None:
    """Implement ``MathModule.AngleMatrix``.

    Inputs are **radians**.  The four ``ByRef`` columns are mutated in place; the
    translation column (``matrixColumn3``) is zeroed, exactly as the original does.
    """
    sy = math.sin(yaw_radians)
    cy = math.cos(yaw_radians)
    sp = math.sin(pitch_radians)
    cp = math.cos(pitch_radians)
    sr = math.sin(roll_radians)
    cr = math.cos(roll_radians)

    matrix_column0.x = cp * cy
    matrix_column0.y = cp * sy
    matrix_column0.z = -sp
    matrix_column1.x = sr * sp * cy + cr * -sy
    matrix_column1.y = sr * sp * sy + cr * cy
    matrix_column1.z = sr * cp
    matrix_column2.x = (cr * sp * cy + -sr * -sy)
    matrix_column2.y = (cr * sp * sy + -sr * cy)
    matrix_column2.z = cr * cp
    matrix_column3.x = 0
    matrix_column3.y = 0
    matrix_column3.z = 0


def matrix_invert(
    in_matrix_column0: SourceVector,
    in_matrix_column1: SourceVector,
    in_matrix_column2: SourceVector,
    in_matrix_column3: SourceVector,
    out_matrix_column0: SourceVector,
    out_matrix_column1: SourceVector,
    out_matrix_column2: SourceVector,
    out_matrix_column3: SourceVector,
) -> None:
    """Implement ``MathModule.MatrixInvert`` (transpose rotation, negate translation)."""
    out_matrix_column0.x = in_matrix_column0.x
    out_matrix_column1.x = in_matrix_column0.y
    out_matrix_column2.x = in_matrix_column0.z

    out_matrix_column0.y = in_matrix_column1.x
    out_matrix_column1.y = in_matrix_column1.y
    out_matrix_column2.y = in_matrix_column1.z

    out_matrix_column0.z = in_matrix_column2.x
    out_matrix_column1.z = in_matrix_column2.y
    out_matrix_column2.z = in_matrix_column2.z

    temp = SourceVector(
        in_matrix_column3.x, in_matrix_column3.y, in_matrix_column3.z
    )

    out_matrix_column3.x = -dot_product(temp, out_matrix_column0)
    out_matrix_column3.y = -dot_product(temp, out_matrix_column1)
    out_matrix_column3.z = -dot_product(temp, out_matrix_column2)


def vector_transform(
    input_vector: SourceVector,
    matrix_column0: SourceVector,
    matrix_column1: SourceVector,
    matrix_column2: SourceVector,
    matrix_column3: SourceVector,
) -> SourceVector:
    """Implement ``MathModule.VectorTransform``."""
    matrix_row0 = SourceVector(matrix_column0.x, matrix_column1.x, matrix_column2.x)
    matrix_row1 = SourceVector(matrix_column0.y, matrix_column1.y, matrix_column2.y)
    matrix_row2 = SourceVector(matrix_column0.z, matrix_column1.z, matrix_column2.z)

    output = SourceVector()
    output.x = dot_product(input_vector, matrix_row0) + matrix_column3.x
    output.y = dot_product(input_vector, matrix_row1) + matrix_column3.y
    output.z = dot_product(input_vector, matrix_row2) + matrix_column3.z

    return output


def vector_i_transform(
    input_vector: SourceVector,
    matrix_column0: SourceVector,
    matrix_column1: SourceVector,
    matrix_column2: SourceVector,
    matrix_column3: SourceVector,
) -> SourceVector:
    """Implement ``MathModule.VectorITransform`` (subtract translation, rotate by transpose)."""
    temp = SourceVector(
        input_vector.x - matrix_column3.x,
        input_vector.y - matrix_column3.y,
        input_vector.z - matrix_column3.z,
    )

    output = SourceVector()
    output.x = temp.x * matrix_column0.x + temp.y * matrix_column0.y + temp.z * matrix_column0.z
    output.y = temp.x * matrix_column1.x + temp.y * matrix_column1.y + temp.z * matrix_column1.z
    output.z = temp.x * matrix_column2.x + temp.y * matrix_column2.y + temp.z * matrix_column2.z

    return output


# ---------------------------------------------------------------------------
# Collision data -> SMD mesh
# ---------------------------------------------------------------------------

def process_transforms_for_physics(
    mdl_file_data,
    phy_file_data: SourcePhyFileData,
    a_first_animation_desc_frame_line=None,
) -> Optional[tuple[SourceVector, SourceVector, SourceVector, SourceVector]]:
    """Implement ``SourceSmdFile49.ProcessTransformsForPhysics``.

    Only runs when there is exactly one solid.  Returns the ``worldToPose`` matrix
    columns, or ``None`` when the guard fails (in which case the columns are never
    consulted by :func:`transform_phy_vertex`).

    ``a_first_animation_desc_frame_line`` is the ``AnimationFrameLine`` that
    ``CalculateFirstAnimDescFrameLinesForPhysics`` produced (the first entry of
    ``CalcAnimation(Nothing, theAnimationDescs(0), 0)``'s ``SortedList``).  It is an
    explicit parameter here because ``CalcAnimation`` belongs to the SMD writer
    module, not to the PHY parser.
    """
    collision_datas = phy_file_data.theSourcePhyCollisionDatas
    if collision_datas is None or len(collision_datas) != 1:
        return None

    position = a_first_animation_desc_frame_line.position
    rotation = a_first_animation_desc_frame_line.rotation

    pose_to_world_column0 = SourceVector()
    pose_to_world_column1 = SourceVector()
    pose_to_world_column2 = SourceVector()
    pose_to_world_column3 = SourceVector()

    angle_matrix(
        rotation.x,
        rotation.y,
        rotation.z + degrees_to_radians(-90),
        pose_to_world_column0,
        pose_to_world_column1,
        pose_to_world_column2,
        pose_to_world_column3,
    )
    pose_to_world_column3.x = position.y
    pose_to_world_column3.y = -position.x
    pose_to_world_column3.z = position.z

    world_to_pose_column0 = SourceVector()
    world_to_pose_column1 = SourceVector()
    world_to_pose_column2 = SourceVector()
    world_to_pose_column3 = SourceVector()
    matrix_invert(
        pose_to_world_column0,
        pose_to_world_column1,
        pose_to_world_column2,
        pose_to_world_column3,
        world_to_pose_column0,
        world_to_pose_column1,
        world_to_pose_column2,
        world_to_pose_column3,
    )

    return (
        world_to_pose_column0,
        world_to_pose_column1,
        world_to_pose_column2,
        world_to_pose_column3,
    )


def transform_phy_vertex(
    a_bone,
    vertex: SourceVector,
    a_source_phys_collision_model: Optional[SourcePhyPhysCollisionModel],
    mdl_file_data,
    phy_file_data: SourcePhyFileData,
    world_to_pose: Optional[tuple[SourceVector, SourceVector, SourceVector, SourceVector]] = None,
) -> SourceVector:
    """Implement ``SourceSmdFile49.TransformPhyVertex`` (lines 1844-2099).

    The live branch table (the VB source uses nested ``If``/``ElseIf``; there is no
    ``Select Case`` -- see the module docstring):

    ====================  ======  =======================  ==================================================
    MDL version           solids  STATIC_PROP              transform
    ====================  ======  =======================  ==================================================
    44..47                1       n/a                      ``(z, -x, -y) * (1 / 0.0254)``
    44..47                >1      n/a                      ``ITransform((x, z, -y) * scale, poseToBone)``
    else (>=48 or <44)    1       set                      ``Transform((z, -x, -y) * scale, worldToPose)`` -> permute -> ``ITransform(poseToBone)``
    else (>=48 or <44)    1       clear                    ``Transform((x, -y, -z) * scale, worldToPose)`` -> ``ITransform(poseToBone)``
    else (>=48 or <44)    >1      n/a                      ``ITransform((x, z, -y) * scale, poseToBone)``
    ====================  ======  =======================  ==================================================

    ``aBone`` must expose ``poseToBoneColumn0..3``; ``a_source_phys_collision_model``
    is accepted for signature parity but the live branches never read it.
    """
    del a_source_phys_collision_model  # unused by the live code paths
    a_vector_transformed = SourceVector()
    a_vector = SourceVector()

    version = mdl_file_data.version
    collision_datas = phy_file_data.theSourcePhyCollisionDatas
    collision_data_count = len(collision_datas) if collision_datas is not None else 0

    if 44 <= version <= 47:
        # This works for various weapons and vehicles in HL2.
        if collision_data_count == 1:
            a_vector_transformed.x = 1 / 0.0254 * vertex.z
            a_vector_transformed.y = 1 / 0.0254 * -vertex.x
            a_vector_transformed.z = 1 / 0.0254 * -vertex.y
        else:
            a_vector.x = 1 / 0.0254 * vertex.x
            a_vector.y = 1 / 0.0254 * vertex.z
            a_vector.z = 1 / 0.0254 * -vertex.y
            a_vector_transformed = vector_i_transform(
                a_vector,
                a_bone.poseToBoneColumn0,
                a_bone.poseToBoneColumn1,
                a_bone.poseToBoneColumn2,
                a_bone.poseToBoneColumn3,
            )
    else:
        if collision_data_count == 1:
            # FROM: collisionmodel.cpp ConvertToWorldSpace()
            if (mdl_file_data.flags & STUDIOHDR_FLAGS_STATIC_PROP) > 0:
                # TEST: Works for props_vehicles that use $staticprop.
                a_vector.x = 1 / 0.0254 * vertex.z
                a_vector.y = 1 / 0.0254 * -vertex.x
                a_vector.z = 1 / 0.0254 * -vertex.y
                a_vector_transformed = vector_transform(
                    a_vector,
                    world_to_pose[0],
                    world_to_pose[1],
                    world_to_pose[2],
                    world_to_pose[3],
                )
                a_vector.x = a_vector_transformed.x
                a_vector.y = a_vector_transformed.z
                a_vector.z = -a_vector_transformed.y
                a_vector_transformed = vector_i_transform(
                    a_vector,
                    a_bone.poseToBoneColumn0,
                    a_bone.poseToBoneColumn1,
                    a_bone.poseToBoneColumn2,
                    a_bone.poseToBoneColumn3,
                )
            else:
                # TEST: Works for w_minigun.
                a_vector.x = 1 / 0.0254 * vertex.x
                a_vector.y = 1 / 0.0254 * -vertex.y
                a_vector.z = 1 / 0.0254 * -vertex.z

                a_vector_transformed = vector_transform(
                    a_vector,
                    world_to_pose[0],
                    world_to_pose[1],
                    world_to_pose[2],
                    world_to_pose[3],
                )
                a_vector.x = a_vector_transformed.x
                a_vector.y = a_vector_transformed.y
                a_vector.z = a_vector_transformed.z
                a_vector_transformed = vector_i_transform(
                    a_vector,
                    a_bone.poseToBoneColumn0,
                    a_bone.poseToBoneColumn1,
                    a_bone.poseToBoneColumn2,
                    a_bone.poseToBoneColumn3,
                )
        else:
            # FROM: collisionmodel.cpp ConvertToBoneSpace()
            a_vector.x = 1 / 0.0254 * vertex.x
            a_vector.y = 1 / 0.0254 * vertex.z
            a_vector.z = 1 / 0.0254 * -vertex.y
            a_vector_transformed = vector_i_transform(
                a_vector,
                a_bone.poseToBoneColumn0,
                a_bone.poseToBoneColumn1,
                a_bone.poseToBoneColumn2,
                a_bone.poseToBoneColumn3,
            )

    return a_vector_transformed


class _FrameLine:
    """Minimal stand-in for the parser's ``AnimationFrameLine``.

    ``SourceSmdFile49.CalculateFirstAnimDescFrameLinesForPhysics`` fills one of these
    in by running ``CalcAnimation`` on the model's first animation description.  Only
    ``position`` and ``rotation`` are read by the physics transform.
    """

    __slots__ = ("position", "rotation")

    def __init__(self, position: SourceVector, rotation: SourceVector) -> None:
        self.position = position
        self.rotation = rotation


#: Zero pose -- used when the animation decoder is unavailable (see the note in
#: :func:`write_triangles_section_for_physics`).
_IDENTITY_FRAME_LINE = _FrameLine(SourceVector(0.0, 0.0, 0.0), SourceVector(0.0, 0.0, 0.0))


def write_triangles_section_for_physics(
    write_line: Callable[[str], None],
    mdl_file_data,
    phy_file_data: SourcePhyFileData,
    a_first_animation_desc_frame_line=None,
) -> None:
    """Implement ``SourceSmdFile49.WriteTrianglesSectionForPhysics``.

    ``write_line(text)`` writes one line **without** a trailing newline; the caller
    is responsible for line endings.  Output layout per face::

        "  phy"
        "    <boneIndex> <x> <y> <z> <nx> <ny> <nz> 0 0"   (three times)

    where every float uses ``"0.000000"`` (six decimals, ``en-US``) and the fields
    are separated by single spaces.  The whole body sits inside a ``Try``/``Catch``
    that swallows errors, so a failure part-way through leaves a truncated
    triangles section followed by ``end`` -- exactly like the original.

    The two extra keyword arguments are the port's seam: ``ProcessTransformsForPhysics``
    needs the animation frame line produced by the SMD writer's ``CalcAnimation``.
    When ``a_first_animation_desc_frame_line`` is omitted it is taken from
    ``mdl_file_data.theFirstAnimDescFrameLine`` if that attribute exists.
    """
    write_line("triangles")

    # NOTE: `a_first_animation_desc_frame_line` is produced by the parser's
    # `CalcAnimation`, which lives in the SMD writer and is not ported yet.  When it
    # is unavailable we substitute an identity world-to-pose transform so that the
    # mesh is still emitted; for single-solid models the result then differs from
    # the parser's, and `MdlDecompiler` reports that as a warning rather than silently.
    frame_line = a_first_animation_desc_frame_line
    if frame_line is None:
        frame_line = getattr(mdl_file_data, "theFirstAnimDescFrameLine", None)
    if frame_line is None:
        frame_line = _IDENTITY_FRAME_LINE

    try:
        if phy_file_data.theSourcePhyCollisionDatas is not None:
            world_to_pose = process_transforms_for_physics(
                mdl_file_data, phy_file_data, frame_line
            )

            for collision_data_index, collision_data in enumerate(
                phy_file_data.theSourcePhyCollisionDatas
            ):
                # NOTE: VB dereferences ``.Count`` here without a null check, so a
                #       missing properties section raises and truncates the mesh.
                phys_collision_models = phy_file_data.theSourcePhyPhysCollisionModels
                if collision_data_index < len(phys_collision_models):
                    a_source_phys_collision_model = phys_collision_models[collision_data_index]
                else:
                    a_source_phys_collision_model = None

                for convex_mesh in collision_data.theConvexMeshes:
                    # [12-Apr-2022] From RED_EYE:
                    #     flags: has_children: (self.flags >> 0) & 3  ' 0 = false; > 0 true
                    if (convex_mesh.flags & 3) > 0:
                        continue

                    if len(mdl_file_data.theBones) == 1:
                        bone_index = 0
                    else:
                        bone_index = convex_mesh.theBoneIndex
                        # MDL36 and MDL37 need this because their PHY does not store
                        # a bone index.  Model versions above MDL37 can have multiple
                        # bones with the same name, so this check needs to be last.
                        if bone_index < 0:
                            if (
                                a_source_phys_collision_model is not None
                                and a_source_phys_collision_model.theName
                                in mdl_file_data.theBoneNameToBoneIndexMap
                            ):
                                bone_index = mdl_file_data.theBoneNameToBoneIndexMap[
                                    a_source_phys_collision_model.theName
                                ]
                            else:
                                # Not expected to reach here, but just in case, write a
                                # mesh connected to the first bone instead of writing an
                                # empty mesh.
                                bone_index = 0
                    a_bone = mdl_file_data.theBones[bone_index]

                    for a_triangle in convex_mesh.theFaces:
                        write_line("  phy")

                        #  19 -0.000009 0.000001 0.999953 0.0 0.0 0.0 1 0
                        for vertex_index in range(len(a_triangle.vertexIndex)):
                            phy_vertex = convex_mesh.theVertices[a_triangle.vertexIndex[vertex_index]]

                            a_vector_transformed = transform_phy_vertex(
                                a_bone,
                                phy_vertex.vertex,
                                a_source_phys_collision_model,
                                mdl_file_data,
                                phy_file_data,
                                world_to_pose,
                            )

                            # NOTE: ``Normal`` normalizes lazily on first access, so it
                            #       is read once and reused for all three components.
                            normal = phy_vertex.normal

                            line = "    "
                            line += str(bone_index)
                            line += " "
                            line += _format_six_decimals(a_vector_transformed.x)
                            line += " "
                            line += _format_six_decimals(a_vector_transformed.y)
                            line += " "
                            line += _format_six_decimals(a_vector_transformed.z)

                            line += " "
                            line += _format_six_decimals(normal.x)
                            line += " "
                            line += _format_six_decimals(normal.y)
                            line += " "
                            line += _format_six_decimals(normal.z)

                            line += " 0 0"
                            # NOTE: The studiomdl.exe doesn't need the integer values at end.
                            write_line(line)
    except MemoryError:
        raise
    except Exception:
        # VB: Catch ex As Exception : Dim debug As Integer = 4242
        pass

    write_line("end")
