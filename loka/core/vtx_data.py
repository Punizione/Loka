"""Data classes for Source VTX geometry and material replacement tables."""

from __future__ import annotations

from typing import Optional

from .constants import MAX_NUM_BONES_PER_VERT

__all__ = [
    "SourceVtxFileData07",
    "SourceVtxBodyPart07",
    "SourceVtxBoneStateChange07",
    "SourceVtxMaterialReplacement07",
    "SourceVtxMaterialReplacementList07",
    "SourceVtxMesh07",
    "SourceVtxModel07",
    "SourceVtxModelLod07",
    "SourceVtxStrip07",
    "SourceVtxStripGroup07",
    "SourceVtxVertex07",
]


class SourceVtxBoneStateChange07:
    """``SourceVtxBoneStateChange07``."""

    def __init__(self) -> None:
        self.hardwareId: int = 0
        self.newBoneId: int = 0


class SourceVtxVertex07:
    """``SourceVtxVertex07`` -- one optimised VTX vertex."""

    def __init__(self) -> None:
        self.boneWeightIndex: list[int] = [0] * MAX_NUM_BONES_PER_VERT
        self.boneCount: int = 0
        self.originalMeshVertexIndex: int = 0
        self.boneId: list[int] = [0] * MAX_NUM_BONES_PER_VERT


class SourceVtxStrip07:
    """``SourceVtxStrip07`` -- one strip within a strip group."""

    SourceStripTriList: int = 0x1
    SourceStripTriStrip: int = 0x2

    def __init__(self) -> None:
        self.indexCount: int = 0
        self.indexMeshIndex: int = 0
        self.vertexCount: int = 0
        self.vertexMeshIndex: int = 0
        self.boneCount: int = 0
        self.flags: int = 0
        self.boneStateChangeCount: int = 0
        self.boneStateChangeOffset: int = 0
        self.unknownBytes01: int = 0
        self.unknownBytes02: int = 0
        self.theVtxBoneStateChanges: Optional[list[SourceVtxBoneStateChange07]] = None


class SourceVtxStripGroup07:
    """``SourceVtxStripGroup07`` -- one strip group within a mesh."""

    SourceStripGroupFlexed: int = 0x1
    SourceStripGroupHwSkinned: int = 0x2
    SourceStripGroupDeltaFixed: int = 0x4
    SourceStripGroupSuppressHwMorph: int = 0x8

    def __init__(self) -> None:
        self.vertexCount: int = 0
        self.vertexOffset: int = 0
        self.indexCount: int = 0
        self.indexOffset: int = 0
        self.stripCount: int = 0
        self.stripOffset: int = 0
        self.flags: int = 0
        self.topologyIndexCount: int = 0
        self.topologyIndexOffset: int = 0
        self.theVtxVertexes: Optional[list[SourceVtxVertex07]] = None
        self.theVtxIndexes: Optional[list[int]] = None
        self.theVtxStrips: Optional[list[SourceVtxStrip07]] = None
        self.theVtxTopologyIndexes: Optional[list[int]] = None


class SourceVtxMesh07:
    """``SourceVtxMesh07`` -- one mesh within a model LOD."""

    SourceMeshTeeth: int = 0x1
    SourceMeshEyes: int = 0x2

    def __init__(self) -> None:
        self.stripGroupCount: int = 0
        self.stripGroupOffset: int = 0
        self.flags: int = 0
        self.theVtxStripGroups: Optional[list[SourceVtxStripGroup07]] = None


class SourceVtxModelLod07:
    """``SourceVtxModelLod07`` -- one LOD of a VTX model."""

    def __init__(self) -> None:
        self.meshCount: int = 0
        self.meshOffset: int = 0
        self.switchPoint: float = 0.0
        self.theVtxMeshes: Optional[list[SourceVtxMesh07]] = None
        self.theVtxModelLodUsesFacial: bool = False


class SourceVtxModel07:
    """``SourceVtxModel07`` -- one VTX model."""

    def __init__(self) -> None:
        self.lodCount: int = 0
        self.lodOffset: int = 0
        self.theVtxModelLods: Optional[list[SourceVtxModelLod07]] = None


class SourceVtxBodyPart07:
    """``SourceVtxBodyPart07`` -- one VTX body part."""

    def __init__(self) -> None:
        self.modelCount: int = 0
        self.modelOffset: int = 0
        self.theVtxModels: Optional[list[SourceVtxModel07]] = None


class SourceVtxMaterialReplacement07:
    """``SourceVtxMaterialReplacement07``."""

    def __init__(self) -> None:
        self.materialIndex: int = 0
        self.nameOffset: int = 0
        self.theName: str = ""


class SourceVtxMaterialReplacementList07:
    """``SourceVtxMaterialReplacementList07``."""

    def __init__(self) -> None:
        self.replacementCount: int = 0
        self.replacementOffset: int = 0
        self.theVtxMaterialReplacements: Optional[list[SourceVtxMaterialReplacement07]] = None


class SourceVtxFileData07:
    """``SourceVtxFileData07`` -- the VTX file header plus parsed trees."""

    def __init__(self) -> None:
        self.version: int = 0
        self.vertexCacheSize: int = 0
        self.maxBonesPerStrip: int = 0
        self.maxBonesPerTri: int = 0
        self.maxBonesPerVertex: int = 0
        self.checksum: int = 0
        self.lodCount: int = 0
        self.materialReplacementListOffset: int = 0
        self.bodyPartCount: int = 0
        self.bodyPartOffset: int = 0
        self.theVtxBodyParts: Optional[list[SourceVtxBodyPart07]] = None
        self.theVtxMaterialReplacementLists: Optional[list[SourceVtxMaterialReplacementList07]] = None
