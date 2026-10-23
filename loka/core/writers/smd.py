"""Write UTF-8 SMD geometry and skeleton sections with CRLF line endings."""

from __future__ import annotations

from typing import Callable
from ... import __version__

from ..constants import STUDIOHDR_FLAGS_STATIC_PROP
from ..mdl_data import SourceMdlFileData49, SourceMdlModel
from ..numfmt import fmt_fixed6, fmt_int, fmt_single_fixed6
from ..vtx_data import SourceVtxModel07, SourceVtxStripGroup07
from ..vvd import SourceVvdFileData04

__all__ = ["SmdFile49", "DEFAULT_PRODUCT_NAME"]

#: Application identity written to exported SMD headers.
DEFAULT_PRODUCT_NAME = f"Loka {__version__}"


class SmdFile49:
    """Implement ``SourceSmdFile49``.

    ``write_line(text)`` receives one line *without* a line terminator.
    """

    def __init__(
        self,
        write_line: Callable[[str], None],
        mdl_file_data: SourceMdlFileData49,
        vvd_file_data: SourceVvdFileData04 | None = None,
        *,
        product_name: str = DEFAULT_PRODUCT_NAME,
        stricter_format: bool = False,
    ) -> None:
        self.write_line = write_line
        self.theMdlFileData = mdl_file_data
        self.theVvdFileData = vvd_file_data
        self.product_name = product_name
        self.stricter_format = stricter_format

    # ------------------------------------------------------------------
    # Common sections
    # ------------------------------------------------------------------
    def write_header_comment(self) -> None:
        """Implement ``Common.WriteHeaderComment``."""
        if not self.stricter_format:
            self.write_line("// Created by " + self.product_name)

    def write_header_section(self) -> None:
        """Implement ``WriteHeaderSection``."""
        self.write_line("version 1")

    def write_nodes_section(self) -> None:
        """Implement ``WriteNodesSection``."""
        self.write_line("nodes")

        bones = self.theMdlFileData.theBones
        bone_count = 0 if bones is None else len(bones)
        for bone_index in range(bone_count):
            bone = bones[bone_index]
            self.write_line(
                "  "
                + fmt_int(bone_index)
                + ' "'
                + bone.theName
                + '" '
                + fmt_int(bone.parentBoneIndex)
            )

        self.write_line("end")

    def write_skeleton_section(self, lod_index: int) -> None:
        """Implement ``WriteSkeletonSection``.

        ``lod_index`` is unused by the v49 writer but kept for signature parity
        (the physics writer passes ``-1``).
        """
        self.write_line("skeleton")

        if self.stricter_format:
            self.write_line("time 0")
        else:
            self.write_line("  time 0")

        for bone_index, bone in enumerate(self.theMdlFileData.theBones):
            self.write_line(
                "    "
                + fmt_int(bone_index)
                + " "
                + fmt_fixed6(bone.position.x)
                + " "
                + fmt_fixed6(bone.position.y)
                + " "
                + fmt_fixed6(bone.position.z)
                + " "
                + fmt_fixed6(bone.rotation.x)
                + " "
                + fmt_fixed6(bone.rotation.y)
                + " "
                + fmt_fixed6(bone.rotation.z)
            )

        self.write_line("end")

    # ------------------------------------------------------------------
    # Triangles (reference / LOD mesh)
    # ------------------------------------------------------------------
    def write_triangles_section(
        self,
        a_vtx_model: SourceVtxModel07,
        lod_index: int,
        a_model: SourceMdlModel,
        body_part_vertex_index_start: int,
        extra_uv_channel_index: int,
    ) -> None:
        """Implement ``WriteTrianglesSection``."""
        self.write_line("triangles")

        try:
            a_vtx_lod = a_vtx_model.theVtxModelLods[lod_index]

            if a_vtx_lod.theVtxMeshes is not None:
                for mesh_index in range(len(a_vtx_lod.theVtxMeshes)):
                    a_vtx_mesh = a_vtx_lod.theVtxMeshes[mesh_index]
                    material_index = a_model.theMeshes[mesh_index].materialIndex
                    material_file_name = self.theMdlFileData.theModifiedTextureFileNames[
                        material_index
                    ]

                    mesh_vertex_index_start = a_model.theMeshes[mesh_index].vertexIndexStart

                    if a_vtx_mesh.theVtxStripGroups is not None:
                        for strip_group in a_vtx_mesh.theVtxStripGroups:
                            if (
                                strip_group.theVtxStrips is None
                                or strip_group.theVtxIndexes is None
                                or strip_group.theVtxVertexes is None
                            ):
                                continue

                            for vtx_index_index in range(0, len(strip_group.theVtxIndexes) - 2, 3):
                                # NOTE: studiomdl.exe complains if the texture name is not at
                                # the start of the line, hence material first on its own line.
                                material_line = material_file_name
                                vertex1 = self._write_vertex_line(
                                    strip_group,
                                    vtx_index_index,
                                    lod_index,
                                    mesh_vertex_index_start,
                                    body_part_vertex_index_start,
                                    extra_uv_channel_index,
                                )
                                vertex2 = self._write_vertex_line(
                                    strip_group,
                                    vtx_index_index + 2,
                                    lod_index,
                                    mesh_vertex_index_start,
                                    body_part_vertex_index_start,
                                    extra_uv_channel_index,
                                )
                                vertex3 = self._write_vertex_line(
                                    strip_group,
                                    vtx_index_index + 1,
                                    lod_index,
                                    mesh_vertex_index_start,
                                    body_part_vertex_index_start,
                                    extra_uv_channel_index,
                                )
                                # If any vertex of the triangle failed, comment out all of it.
                                if (
                                    vertex1.startswith("// ")
                                    or vertex2.startswith("// ")
                                    or vertex3.startswith("// ")
                                ):
                                    material_line = "// " + material_line
                                    if not vertex1.startswith("// "):
                                        vertex1 = "// " + vertex1
                                    if not vertex2.startswith("// "):
                                        vertex2 = "// " + vertex2
                                    if not vertex3.startswith("// "):
                                        vertex3 = "// " + vertex3

                                self.write_line(material_line)
                                self.write_line(vertex1)
                                self.write_line(vertex2)
                                self.write_line(vertex3)
        except MemoryError:
            raise
        except Exception:
            # the parser swallows this and emits a truncated (but closed) section.
            pass

        self.write_line("end")

    def _write_vertex_line(
        self,
        a_strip_group: SourceVtxStripGroup07,
        a_vtx_index_index: int,
        lod_index: int,
        mesh_vertex_index_start: int,
        body_part_vertex_index_start: int,
        extra_uv_channel_index: int,
    ) -> str:
        """Implement ``WriteVertexLine``.  Returns the line (possibly ``// ``-prefixed)."""
        line = ""
        try:
            a_vtx_vertex_index = a_strip_group.theVtxIndexes[a_vtx_index_index]
            a_vtx_vertex = a_strip_group.theVtxVertexes[a_vtx_vertex_index]
            vertex_index = (
                a_vtx_vertex.originalMeshVertexIndex
                + body_part_vertex_index_start
                + mesh_vertex_index_start
            )

            if self.theVvdFileData.fixupCount == 0:
                a_vertex = self.theVvdFileData.theVertexes[vertex_index]
            else:
                # NOTE: the parser always indexes LOD 0 here -- using lodIndex does not
                # match what MDL Decompiler produces.
                a_vertex = self.theVvdFileData.theFixedVertexesByLod[0][vertex_index]

            line = "  "
            line += fmt_int(a_vertex.boneWeight.bone[0])

            line += " "
            if (self.theMdlFileData.flags & STUDIOHDR_FLAGS_STATIC_PROP) > 0:
                line += fmt_fixed6(a_vertex.positionY)
                line += " "
                line += fmt_fixed6(-a_vertex.positionX)
            else:
                line += fmt_fixed6(a_vertex.positionX)
                line += " "
                line += fmt_fixed6(a_vertex.positionY)
            line += " "
            line += fmt_fixed6(a_vertex.positionZ)

            line += " "
            if (self.theMdlFileData.flags & STUDIOHDR_FLAGS_STATIC_PROP) > 0:
                line += fmt_fixed6(a_vertex.normalY)
                line += " "
                line += fmt_fixed6(-a_vertex.normalX)
            else:
                line += fmt_fixed6(a_vertex.normalX)
                line += " "
                line += fmt_fixed6(a_vertex.normalY)
            line += " "
            line += fmt_fixed6(a_vertex.normalZ)

            if extra_uv_channel_index == -1:
                line += " "
                line += fmt_fixed6(a_vertex.texCoordX)
                line += " "
                line += fmt_fixed6(1 - a_vertex.texCoordY)
            else:
                extra = self.theVvdFileData.theExtraDatas[extra_uv_channel_index]
                u = extra.theTextureCoordinates[vertex_index].X
                v = extra.theTextureCoordinates[vertex_index].Y
                line += " "
                line += fmt_fixed6(u)
                line += " "
                line += fmt_fixed6(1 - v)

            line += " "
            line += fmt_int(a_vertex.boneWeight.boneCount)
            for bone_weight_bone_index in range(a_vertex.boneWeight.boneCount):
                line += " "
                line += fmt_int(a_vertex.boneWeight.bone[bone_weight_bone_index])
                line += " "
                # NOTE: bone weights are VB `Single`, so they need .NET's
                # 7-significant-digit Single formatting, unlike the Double
                # position/normal/UV fields above.
                line += fmt_single_fixed6(a_vertex.boneWeight.weight[bone_weight_bone_index])
        except MemoryError:
            raise
        except Exception:
            line = "// " + line

        return line
