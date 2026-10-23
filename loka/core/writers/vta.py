"""Write VTA flex frames and vertex deltas."""

from __future__ import annotations

from typing import Optional

from ..mdl_data import FlexFrame, SourceMdlBodyPart, SourceMdlFileData49, SourceMdlFlex
from ..vvd import SourceVertex, SourceVvdFileData04
from .text import (
    DEFAULT_PRODUCT_NAME,
    DEFAULT_VERSION,
    WriteLine,
    fmt_fixed6,
    fmt_int,
    write_header_comment as _write_header_comment,
)

__all__ = [
    "SourceVtaFile49",
    "create_flex_frame_list",
    "get_split",
    "get_vta_file_name",
]


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def get_vta_file_name(model_name: str, body_part_index: int) -> str:
    """Implement ``SourceFileNamesModule.GetVtaFileName``.

    ``<model>_<bodyPartIndex + 1 zero padded to 2>.vta`` -- one-based body part
    number, e.g. ``gman_01.vta``.  The ``"00"`` custom format is emitted without
    the internal ``NumberFormatInfo`` in VB, but ``"00"`` is culture independent.
    """
    return f"{model_name}_{body_part_index + 1:02d}.vta"


# ---------------------------------------------------------------------------
# Flex frames
# ---------------------------------------------------------------------------

def get_split(a_flex: SourceMdlFlex, mesh_vertex_index_start: int) -> float:  # noqa: ARG001
    """Implement ``SourceMdlFile49.GetSplit`` -- **always returns ``1``**.

    The real split calculation is commented out in the VB original and both
    parameters are ignored.  ``flexSplit`` is not written to a ``.vta`` at all;
    it only feeds the QC ``flexpair`` line, so this constant is reproduced purely
    for QC compatibility.
    """
    return 1.0


def _build_flex_frame(
    a_flex: SourceMdlFlex,
    mesh_vertex_index_start: int,
    mdl_file_data: SourceMdlFileData49,
) -> FlexFrame:
    """Build a new :class:`FlexFrame` exactly as ``CreateFlexFrameList`` does."""
    a_flex_frame = FlexFrame()

    # NOTE: ``aMesh.theFlexes(flexIndex).flexDescPartnerIndex`` in the VB original.
    a_flex_desc_partner_index = a_flex.flexDescPartnerIndex

    flex_descs = mdl_file_data.theFlexDescs
    assert flex_descs is not None  # guarded by create_flex_frame_list

    a_flex_frame.flexName = flex_descs[a_flex.flexDescIndex].theName
    if a_flex_desc_partner_index > 0:
        a_flex_frame.flexDescription = a_flex_frame.flexName
        a_flex_frame.flexDescription += "+"
        a_flex_frame.flexDescription += flex_descs[a_flex_desc_partner_index].theName
        a_flex_frame.flexHasPartner = True
        a_flex_frame.flexPartnerName = flex_descs[a_flex_desc_partner_index].theName
        a_flex_frame.flexSplit = get_split(a_flex, mesh_vertex_index_start)
        flex_descs[a_flex_desc_partner_index].theDescIsUsedByFlex = True
    else:
        a_flex_frame.flexDescription = a_flex_frame.flexName
        a_flex_frame.flexHasPartner = False
    flex_descs[a_flex.flexDescIndex].theDescIsUsedByFlex = True

    return a_flex_frame


def create_flex_frame_list(mdl_file_data: SourceMdlFileData49) -> None:
    """Implement ``SourceMdlFile49.CreateFlexFrameList`` (4363-4473).

    Fills ``mdl_file_data.theBodyParts[bp].theFlexFrames`` -- the list the ``.vta``
    writer iterates.  Index 0 is always the implicit "default flex" frame (the
    basis shape key); ``create_flex_frame_list`` appends one frame per distinct
    (flexDescIndex, target0..target3) combination, in the MDL's
    body-part/model/mesh/flex file order.

    The dedup map ``flexDescToFlexFrames`` is rebuilt **per body part**, so the
    same flex descriptor in a different body part yields a different frame.

    ``cumulativebodyPartVertexIndexStart`` deliberately is *not* reset per body
    part: it is the running count of every model vertex seen so far, i.e. the
    global VVD base offset of the current body part.  Each frame records
    ``meshVertexIndexStart + cumulativebodyPartVertexIndexStart`` for every flex it
    collects, in lockstep with ``frame.flexes``.

    Reproduces the VB loop guards: a body part/model/mesh/flex list that is empty
    or ``None`` is skipped, and a flex whose ``theVertAnims`` is empty contributes
    no vertex rows (the writer iterates ``theVertAnims``, not ``vertCount``).
    """
    if not mdl_file_data.theFlexDescs:
        # ``If theFlexDescs IsNot Nothing AndAlso Count > 0`` -- otherwise no frame
        # list is built at all and therefore no .vta files are produced.
        return

    body_parts = mdl_file_data.theBodyParts
    if not body_parts:
        return

    cumulative_body_part_vertex_index_start = 0

    for body_part_index in range(len(body_parts)):
        a_body_part = body_parts[body_part_index]

        flex_desc_count = len(mdl_file_data.theFlexDescs)
        flex_desc_to_flex_frames: list[list[FlexFrame]] = [[] for _ in range(flex_desc_count)]

        a_body_part.theFlexFrames = []
        # NOTE: Create the default flex.
        a_body_part.theFlexFrames.append(FlexFrame())

        if not a_body_part.theModels:
            continue

        for model_index in range(len(a_body_part.theModels)):
            a_model = a_body_part.theModels[model_index]

            if a_model.theMeshes:
                for mesh_index in range(len(a_model.theMeshes)):
                    a_mesh = a_model.theMeshes[mesh_index]

                    # NOTE: re-indexed from the MDL in the VB original, not read
                    # from the aMesh variable.
                    mesh_vertex_index_start = (
                        mdl_file_data.theBodyParts[body_part_index]
                        .theModels[model_index]
                        .theMeshes[mesh_index]
                        .vertexIndexStart
                    )

                    if a_mesh.theFlexes:
                        for flex_index in range(len(a_mesh.theFlexes)):
                            a_flex = a_mesh.theFlexes[flex_index]

                            a_flex_frame: Optional[FlexFrame] = None
                            for searched_flex_frame in flex_desc_to_flex_frames[a_flex.flexDescIndex]:
                                # Exact Double equality on all four targets.
                                if (
                                    searched_flex_frame.flexes[0].target0 == a_flex.target0
                                    and searched_flex_frame.flexes[0].target1 == a_flex.target1
                                    and searched_flex_frame.flexes[0].target2 == a_flex.target2
                                    and searched_flex_frame.flexes[0].target3 == a_flex.target3
                                ):
                                    # Add to an existing flexFrame.
                                    a_flex_frame = searched_flex_frame
                                    break

                            if a_flex_frame is None:
                                a_flex_frame = _build_flex_frame(
                                    a_flex, mesh_vertex_index_start, mdl_file_data
                                )
                                a_body_part.theFlexFrames.append(a_flex_frame)
                                flex_desc_to_flex_frames[a_flex.flexDescIndex].append(a_flex_frame)

                            # Always appended, in lockstep, for new and reused frames.
                            a_flex_frame.bodyAndMeshVertexIndexStarts.append(
                                mesh_vertex_index_start + cumulative_body_part_vertex_index_start
                            )
                            a_flex_frame.flexes.append(a_flex)

            cumulative_body_part_vertex_index_start += a_model.vertexCount


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

class SourceVtaFile49:
    """Implement ``SourceVtaFile49``.

    ``write_line`` is the parser's ``StreamWriter.WriteLine`` -- pass a
    :class:`~loka.core.writers.text.TextWriter` (or any object with a
    ``write_line(str)`` method / any ``str -> None`` callable).

    ``stricter_format`` mirrors ``TheApp.Settings.DecompileStricterFormatIsChecked``:
    it suppresses the header comment and removes the two leading spaces on every
    ``time`` line.  Nothing else changes.
    """

    def __init__(
        self,
        write_line: WriteLine,
        mdl_file_data: SourceMdlFileData49,
        vvd_file_data: SourceVvdFileData04,
        body_part: SourceMdlBodyPart,
        *,
        stricter_format: bool = False,
        product_name: Optional[str] = None,
        version: Optional[tuple[int, ...]] = None,
    ) -> None:
        if not callable(write_line):
            # Accept a TextWriter-like object as a convenience.
            write_line = write_line.write_line
        self.theOutputFileStreamWriter: WriteLine = write_line
        self.theMdlFileData = mdl_file_data
        self.theVvdFileData = vvd_file_data
        self.theBodyPart = body_part
        self.theStricterFormatIsChecked = stricter_format
        self.theProductName = product_name
        self.theVersion = version

    # -- indentation ---------------------------------------------------------
    @property
    def _time_indent(self) -> str:
        """``"  "`` normally, ``""`` in stricter-format mode (lines 60-64, 78-82...)."""
        return "" if self.theStricterFormatIsChecked else "  "

    # -- sections ------------------------------------------------------------
    def write_header_comment(self) -> None:
        """Implement ``SourceVtaFile49.WriteHeaderComment`` (lines 18-20)."""
        _write_header_comment(
            self.theOutputFileStreamWriter,
            self.theStricterFormatIsChecked,
            self.theProductName if self.theProductName is not None else DEFAULT_PRODUCT_NAME,
            self.theVersion if self.theVersion is not None else DEFAULT_VERSION,
        )

    def write_header_section(self) -> None:
        """Implement ``SourceVtaFile49.WriteHeaderSection`` (lines 22-28) -- ``version 1``."""
        # ``version 1`` is emitted unconditionally; the stricter-format switch does
        # not touch it.
        self.theOutputFileStreamWriter("version 1")

    def write_nodes_section(self) -> None:
        """Implement ``SourceVtaFile49.WriteNodesSection`` (lines 30-52).

        Grammar: ``'  ' + str(boneIndex) + ' "' + boneName + '" ' + str(parentBoneIndex)``.

        Unlike the SMD writer (lines 49-53) this does **not** guard against
        ``theBones`` being nothing; the parser lets the resulting exception escape
        into the caller's ``Try`` and the file ends up truncated.
        """
        self.theOutputFileStreamWriter("nodes")

        bones = self.theMdlFileData.theBones
        if bones is None:
            raise AttributeError(
                "theMdlFileData.theBones is None; SourceVtaFile49.WriteNodesSection "
                "is required to write the VTA nodes section"
            )

        for bone_index in range(len(bones)):
            a_bone = bones[bone_index]
            self.theOutputFileStreamWriter(
                "  "
                + fmt_int(bone_index)
                + ' "'
                + a_bone.theName
                + '" '
                + fmt_int(a_bone.parentBoneIndex)
            )

        self.theOutputFileStreamWriter("end")

    def write_skeleton_section_for_vertex_animation(self) -> None:
        """Implement ``SourceVtaFile49.WriteSkeletonSectionForVertexAnimation`` (54-93).

        Emits ``time`` headers only -- no bone rows (see the module docstring).
        The number is the loop counter ``timeIndex`` (1, 2, 3, ...), not
        ``flexTimeIndex``; the two are equal in practice but the VB code keeps its
        own counter, which is reproduced here.
        """
        self.theOutputFileStreamWriter("skeleton")
        self.theOutputFileStreamWriter(self._time_indent + "time 0 # basis shape key")

        a_flex_frames = self.theBodyPart.theFlexFrames
        if not a_flex_frames:
            self.theOutputFileStreamWriter("end")
            return

        time_index = 1
        # NOTE: The first frame was written in code above.
        for flex_time_index in range(1, len(a_flex_frames)):
            a_flex_frame = a_flex_frames[flex_time_index]

            self.theOutputFileStreamWriter(
                self._time_indent + "time " + fmt_int(time_index) + " # " + a_flex_frame.flexDescription
            )

            time_index += 1

        self.theOutputFileStreamWriter("end")

    def write_vertex_animation_section(self) -> None:
        """Implement ``SourceVtaFile49.WriteVertexAnimationSection`` (lines 95-199)."""
        self.theOutputFileStreamWriter("vertexanimation")
        self.theOutputFileStreamWriter(self._time_indent + "time 0 # basis shape key")

        # Write all vertexes for every basis shape key.  The commented-out
        # per-body-part vertex range computation (lines 108-136) is deliberately
        # not reproduced: "[07-Jan-2023] This prevents flexes from working on
        # recompile in a model where the only bodypart that had flexes did not
        # start at the first vertex of the global vertex list."
        try:
            vertexes = self._vertex_list()
            for vertex_index in range(len(vertexes)):
                a_vertex = vertexes[vertex_index]
                # Four leading spaces; seven "0.000000" fields.
                self.theOutputFileStreamWriter(
                    "    "
                    + fmt_int(vertex_index)
                    + " "
                    + fmt_fixed6(a_vertex.positionX)
                    + " "
                    + fmt_fixed6(a_vertex.positionY)
                    + " "
                    + fmt_fixed6(a_vertex.positionZ)
                    + " "
                    + fmt_fixed6(a_vertex.normalX)
                    + " "
                    + fmt_fixed6(a_vertex.normalY)
                    + " "
                    + fmt_fixed6(a_vertex.normalZ)
                )
        except AttributeError:
            # The VB original wraps this loop in a bare ``Try ... Catch ex As
            # Exception : End Try`` (lines 138, 166-167) so an out-of-range index
            # silently truncates the basis block.  Only the equivalent failure is
            # swallowed here -- a bug in this port still raises.
            pass

        a_flex_frames = self.theBodyPart.theFlexFrames
        if not a_flex_frames:
            self.theOutputFileStreamWriter("end")
            return

        time_index = 1
        # NOTE: The first frame was written in code above.
        for flex_time_index in range(1, len(a_flex_frames)):
            a_flex_frame = a_flex_frames[flex_time_index]

            self.theOutputFileStreamWriter(
                self._time_indent + "time " + fmt_int(time_index) + " # " + a_flex_frame.flexDescription
            )

            for x in range(len(a_flex_frame.flexes)):
                self._write_vertex_anim_lines(
                    a_flex_frame.flexes[x], a_flex_frame.bodyAndMeshVertexIndexStarts[x]
                )

            time_index += 1

        self.theOutputFileStreamWriter("end")

    # -- private methods -----------------------------------------------------
    def _vertex_list(self) -> list[SourceVertex]:
        """The vertex array the basis block iterates.

        ``theVertexes`` when ``fixupCount == 0``, else the pre-fixed LOD-0 list.
        NOTE (from the VB original, lines 145-146): *"I don't know why lodIndex is
        not needed here, but using only lodIndex=0 matches what MDL Decompiler
        produces."*
        """
        vvd_file_data = self.theVvdFileData
        if vvd_file_data.fixupCount == 0:
            return vvd_file_data.theVertexes
        return vvd_file_data.theFixedVertexesByLod[0]

    def _vertex_at(self, vertex_index: int) -> SourceVertex:
        """One vertex, using the same fixup rule as :meth:`_vertex_list`."""
        vvd_file_data = self.theVvdFileData
        if vvd_file_data.fixupCount == 0:
            return vvd_file_data.theVertexes[vertex_index]
        return vvd_file_data.theFixedVertexesByLod[0][vertex_index]

    def _write_vertex_anim_lines(
        self, a_flex: SourceMdlFlex, body_and_mesh_vertex_index_start: int
    ) -> None:
        """Implement ``SourceVtaFile49.WriteVertexAnimLines`` (lines 209-270).

        ``vertexIndex = aVertAnim.index + bodyAndMeshVertexIndexStart``; the emitted
        position/normal is the bind-pose value plus the vert-anim delta.  The
        ``theVertAnims`` list is iterated in raw on-disk order -- the VB sort call
        is commented out, so sorting here would be a
        bug.
        """
        the_vert_anims = a_flex.theVertAnims
        if not the_vert_anims:
            return

        for i in range(len(the_vert_anims)):
            a_vert_anim = the_vert_anims[i]

            vertex_index = a_vert_anim.index + body_and_mesh_vertex_index_start
            a_vertex = self._vertex_at(vertex_index)

            # ``flDelta(n).TheFloatValue`` in VB; mdl_data.py stores the decoded
            # floats directly on theDelta / theNDelta.
            position_x = a_vertex.positionX + a_vert_anim.theDelta[0]
            position_y = a_vertex.positionY + a_vert_anim.theDelta[1]
            position_z = a_vertex.positionZ + a_vert_anim.theDelta[2]
            normal_x = a_vertex.normalX + a_vert_anim.theNDelta[0]
            normal_y = a_vertex.normalY + a_vert_anim.theNDelta[1]
            normal_z = a_vertex.normalZ + a_vert_anim.theNDelta[2]

            self.theOutputFileStreamWriter(
                "    "
                + fmt_int(vertex_index)
                + " "
                + fmt_fixed6(position_x)
                + " "
                + fmt_fixed6(position_y)
                + " "
                + fmt_fixed6(position_z)
                + " "
                + fmt_fixed6(normal_x)
                + " "
                + fmt_fixed6(normal_y)
                + " "
                + fmt_fixed6(normal_z)
            )
