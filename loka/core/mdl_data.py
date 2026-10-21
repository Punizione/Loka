"""Data classes for Source model headers, bones, meshes and animations."""

from __future__ import annotations

from typing import Optional

from .constants import MAX_NUM_BONES_PER_VERT, MAX_NUM_LODS
from .vectors import SourceQuaternion, SourceVector, SourceVector4D

__all__ = [
    "SourceMdlFileData49",
    "SourceMdlAnimBlock",
    "SourceMdlAnimationDesc49",
    "SourceMdlAttachment",
    "SourceMdlBodyPart",
    "SourceMdlBodygroupPreset",
    "SourceMdlBone",
    "SourceMdlBoneController",
    "SourceMdlEyeball",
    "SourceMdlFlex",
    "SourceMdlFlexController",
    "SourceMdlFlexControllerUi",
    "SourceMdlFlexDesc",
    "SourceMdlHitbox",
    "SourceMdlHitboxSet",
    "SourceMdlIkChain",
    "SourceMdlIkLock",
    "SourceMdlMesh",
    "SourceMdlMeshVertexData",
    "SourceMdlModel",
    "SourceMdlModelGroup",
    "SourceMdlModelVertexData",
    "SourceMdlMouth",
    "SourceMdlPoseParamDesc",
    "SourceMdlSequenceDesc",
    "SourceMdlTexture",
    "SourceMdlVertAnim",
    "SourceMdlWeightList",
    "FlexFrame",
]


# ---------------------------------------------------------------------------
# Small helper structures
# ---------------------------------------------------------------------------

class SourceMdlAnimBlock:
    """``SourceMdlAnimBlock`` -- one entry of the MDL's animation block table."""

    def __init__(self) -> None:
        self.dataStart: int = 0
        self.dataEnd: int = 0


class SourceMdlModelVertexData:
    """``SourceMdlModelVertexData``."""

    def __init__(self) -> None:
        self.vertexDataP: int = 0
        self.tangentDataP: int = 0


class SourceMdlMeshVertexData:
    """``SourceMdlMeshVertexData``."""

    def __init__(self) -> None:
        self.modelVertexDataP: int = 0
        self.lodVertexCount: list[int] = [0] * MAX_NUM_LODS


class SourceMdlAxisInterpBone:
    """``SourceMdlAxisInterpBone`` (procedural rule type AXISINTERP)."""

    def __init__(self) -> None:
        self.control: int = 0
        self.axis: int = 0
        self.pos: list[SourceVector] = [SourceVector() for _ in range(6)]
        self.quat: list[SourceQuaternion] = [SourceQuaternion() for _ in range(6)]


class SourceMdlQuatInterpInfo:
    """``SourceMdlQuatInterpInfo`` -- one trigger of a quat-interp bone."""

    def __init__(self) -> None:
        self.inverseToleranceAngle: float = 0.0
        self.trigger: SourceQuaternion = SourceQuaternion()
        self.pos: SourceVector = SourceVector()
        self.quat: SourceQuaternion = SourceQuaternion()


class SourceMdlQuatInterpBone:
    """``SourceMdlQuatInterpBone`` (procedural rule type QUATINTERP)."""

    def __init__(self) -> None:
        self.controlBoneIndex: int = 0
        self.triggerCount: int = 0
        self.triggerOffset: int = 0
        self.theTriggers: list[SourceMdlQuatInterpInfo] = []


class SourceMdlAimAtBone:
    """``SourceMdlAimAtBone`` (procedural rule types AIMATBONE / AIMATATTACH)."""

    def __init__(self) -> None:
        self.parentBoneIndex: int = 0
        self.aimBoneOrAttachmentIndex: int = 0
        self.aim: SourceVector = SourceVector()
        self.up: SourceVector = SourceVector()
        self.basePos: SourceVector = SourceVector()


class SourceMdlJiggleBone:
    """``SourceMdlJiggleBone`` (procedural rule type JIGGLE)."""

    # JIGGLE_* flag constants
    JIGGLE_IS_FLEXIBLE: int = 0x1
    JIGGLE_IS_RIGID: int = 0x2
    JIGGLE_HAS_YAW_CONSTRAINT: int = 0x4
    JIGGLE_HAS_PITCH_CONSTRAINT: int = 0x8
    JIGGLE_HAS_ANGLE_CONSTRAINT: int = 0x10
    JIGGLE_HAS_LENGTH_CONSTRAINT: int = 0x20
    JIGGLE_HAS_BASE_SPRING: int = 0x40
    JIGGLE_IS_BOING: int = 0x80

    def __init__(self) -> None:
        self.flags: int = 0
        self.length: float = 0.0
        self.tipMass: float = 0.0
        self.yawStiffness: float = 0.0
        self.yawDamping: float = 0.0
        self.pitchStiffness: float = 0.0
        self.pitchDamping: float = 0.0
        self.alongStiffness: float = 0.0
        self.alongDamping: float = 0.0
        self.angleLimit: float = 0.0
        self.minYaw: float = 0.0
        self.maxYaw: float = 0.0
        self.yawFriction: float = 0.0
        self.yawBounce: float = 0.0
        self.minPitch: float = 0.0
        self.maxPitch: float = 0.0
        self.pitchFriction: float = 0.0
        self.pitchBounce: float = 0.0
        self.baseMass: float = 0.0
        self.baseStiffness: float = 0.0
        self.baseDamping: float = 0.0
        self.baseMinLeft: float = 0.0
        self.baseMaxLeft: float = 0.0
        self.baseLeftFriction: float = 0.0
        self.baseMinUp: float = 0.0
        self.baseMaxUp: float = 0.0
        self.baseUpFriction: float = 0.0
        self.baseMinForward: float = 0.0
        self.baseMaxForward: float = 0.0
        self.baseForwardFriction: float = 0.0
        self.boingImpactSpeed: float = 0.0
        self.boingImpactAngle: float = 0.0
        self.boingDampingRate: float = 0.0
        self.boingFrequency: float = 0.0
        self.boingAmplitude: float = 0.0


# ---------------------------------------------------------------------------
# Bones / attachments / hitboxes
# ---------------------------------------------------------------------------

class SourceMdlBone:
    """``SourceMdlBone`` -- one ``mstudiobone_t``."""

    # procedural rule types
    STUDIO_PROC_AXISINTERP: int = 1
    STUDIO_PROC_QUATINTERP: int = 2
    STUDIO_PROC_AIMATBONE: int = 3
    STUDIO_PROC_AIMATATTACH: int = 4
    STUDIO_PROC_JIGGLE: int = 5

    def __init__(self) -> None:
        self.name: str = ""
        self.nameOffset: int = 0
        self.parentBoneIndex: int = 0
        self.boneControllerIndex: list[int] = [0] * 6
        self.value: list[float] = [0.0] * 6
        self.scale: list[float] = [0.0] * 6
        self.position: SourceVector = SourceVector()
        self.quat: SourceQuaternion = SourceQuaternion()
        self.animChannels: list[float] = [0.0] * 6
        self.rotation: SourceVector = SourceVector()
        self.positionScale: SourceVector = SourceVector()
        self.rotationScale: SourceVector = SourceVector()
        self.poseToBoneColumn0: SourceVector = SourceVector()
        self.poseToBoneColumn1: SourceVector = SourceVector()
        self.poseToBoneColumn2: SourceVector = SourceVector()
        self.poseToBoneColumn3: SourceVector = SourceVector()
        self.qAlignment: SourceQuaternion = SourceQuaternion()
        self.flags: int = 0
        self.proceduralRuleType: int = 0
        self.proceduralRuleOffset: int = 0
        self.physicsBoneIndex: int = 0
        self.surfacePropNameOffset: int = 0
        self.contents: int = 0
        self.unused: list[int] = [0] * 8
        self.theName: str = ""
        self.theAxisInterpBone: Optional[SourceMdlAxisInterpBone] = None
        self.theQuatInterpBone: Optional[SourceMdlQuatInterpBone] = None
        self.theAimAtBone: Optional[SourceMdlAimAtBone] = None
        self.theJiggleBone: Optional[SourceMdlJiggleBone] = None
        self.theSurfacePropName: str = ""


class SourceMdlBoneController:
    """``SourceMdlBoneController`` -- one ``mstudiobonecontroller_t``."""

    STUDIO_X: int = 0
    STUDIO_Y: int = 1
    STUDIO_Z: int = 2
    STUDIO_XR: int = 3
    STUDIO_YR: int = 4
    STUDIO_ZR: int = 5
    STUDIO_LX: int = 6
    STUDIO_LY: int = 7
    STUDIO_LZ: int = 8
    STUDIO_LXR: int = 9
    STUDIO_LYR: int = 10
    STUDIO_LZR: int = 11
    STUDIO_LINEAR: int = 12
    STUDIO_QUADRATIC_MOTION: int = 13
    STUDIO_TYPES: int = 14
    STUDIO_RLOOP: int = 16

    def __init__(self) -> None:
        self.boneIndex: int = 0
        self.type: int = 0
        self.startBlah: float = 0.0
        self.endBlah: float = 0.0
        self.restIndex: int = 0
        self.inputField: int = 0
        self.unused: list[int] = [0] * 8


class SourceMdlAttachment:
    """``SourceMdlAttachment`` -- one ``mstudioattachment_t``."""

    def __init__(self) -> None:
        self.name: str = ""
        self.type: int = 0
        self.bone: int = 0
        self.attachmentPoint: SourceVector = SourceVector()
        self.vectors: list[SourceVector] = [SourceVector() for _ in range(3)]
        self.nameOffset: int = 0
        self.flags: int = 0
        self.localBoneIndex: int = 0
        self.localM11: float = 0.0
        self.localM12: float = 0.0
        self.localM13: float = 0.0
        self.localM14: float = 0.0
        self.localM21: float = 0.0
        self.localM22: float = 0.0
        self.localM23: float = 0.0
        self.localM24: float = 0.0
        self.localM31: float = 0.0
        self.localM32: float = 0.0
        self.localM33: float = 0.0
        self.localM34: float = 0.0
        self.unused: list[int] = [0] * 8
        self.theName: str = ""


class SourceMdlHitbox:
    """``SourceMdlHitbox`` -- one ``mstudiobbox_t``."""

    def __init__(self) -> None:
        self.boneIndex: int = 0
        self.groupIndex: int = 0
        self.boundingBoxMin: SourceVector = SourceVector()
        self.boundingBoxMax: SourceVector = SourceVector()
        self.nameOffset: int = 0
        self.unused: list[int] = [0] * 8
        self.boundingBoxPitchYawRoll: SourceVector = SourceVector()
        self.unknown: float = 0.0
        self.unused_VERSION49: list[int] = [0] * 4
        self.theName: str = ""


class SourceMdlHitboxSet:
    """``SourceMdlHitboxSet`` -- one ``mstudiohitboxset_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.hitboxCount: int = 0
        self.hitboxOffset: int = 0
        self.theName: str = ""
        self.theHitboxes: Optional[list[SourceMdlHitbox]] = None


# ---------------------------------------------------------------------------
# Sequences / events
# ---------------------------------------------------------------------------

class SourceMdlEvent:
    """``SourceMdlEvent`` -- one ``mstudioevent_t``."""

    NEW_EVENT_STYLE: int = 1 << 10

    def __init__(self) -> None:
        self.cycle: float = 0.0
        self.eventIndex: int = 0
        self.eventType: int = 0
        self.options: str = ""
        self.nameOffset: int = 0
        self.theName: str = ""


class SourceMdlAutoLayer:
    """``SourceMdlAutoLayer`` -- one ``mstudioautolayer_t``."""

    def __init__(self) -> None:
        self.iSequence: int = 0
        self.iPose: int = 0
        self.flags: int = 0
        self.start: float = 0.0
        self.peak: float = 0.0
        self.tail: float = 0.0
        self.end: float = 0.0


class SourceMdlIkLock:
    """``SourceMdlIkLock`` -- one ``mstudioiklock_t``."""

    def __init__(self) -> None:
        self.chainIndex: int = 0
        self.posLock: float = 0.0
        self.rotLock: float = 0.0


class SourceMdlActivityModifier:
    """``SourceMdlActivityModifier``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.theName: str = ""


class SourceMdlAnimTag:
    """``SourceMdlAnimTag``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.theName: str = ""
        self.cycle: float = 0.0


class SourceMdlSequenceDesc:
    """``SourceMdlSequenceDesc`` -- the shared base for sequence descriptions."""

    def __init__(self) -> None:
        self.baseHeaderOffset: int = 0
        self.nameOffset: int = 0
        self.activityNameOffset: int = 0
        self.flags: int = 0
        self.activity: int = 0
        self.activityWeight: int = 0
        self.eventCount: int = 0
        self.eventOffset: int = 0
        self.bbMin: SourceVector = SourceVector()
        self.bbMax: SourceVector = SourceVector()
        self.blendCount: int = 0
        self.animIndexOffset: int = 0
        self.movementIndex: int = 0
        self.groupSize: list[int] = [0, 0]
        self.paramIndex: list[int] = [0, 0]
        self.paramStart: list[float] = [0.0, 0.0]
        self.paramEnd: list[float] = [0.0, 0.0]
        self.paramParent: int = 0
        self.fadeInTime: float = 0.0
        self.fadeOutTime: float = 0.0
        self.localEntryNodeIndex: int = 0
        self.localExitNodeIndex: int = 0
        self.nodeFlags: int = 0
        self.entryPhase: float = 0.0
        self.exitPhase: float = 0.0
        self.lastFrame: float = 0.0
        self.nextSeq: int = 0
        self.pose: int = 0
        self.ikRuleCount: int = 0
        self.autoLayerCount: int = 0
        self.autoLayerOffset: int = 0
        self.weightOffset: int = 0
        self.poseKeyOffset: int = 0
        self.ikLockCount: int = 0
        self.ikLockOffset: int = 0
        self.keyValueOffset: int = 0
        self.keyValueSize: int = 0
        self.cyclePoseIndex: int = 0
        self.unused: list[int] = [0] * 7
        self.activityModifierOffset: int = 0
        self.activityModifierCount: int = 0
        self.animTagOffset: int = 0
        self.animTagCount: int = 0
        self.rootDriverBoneIndex: int = 0
        self.theName: str = ""
        self.theActivityName: str = ""
        self.thePoseKeys: Optional[list[float]] = None
        self.theEvents: Optional[list[SourceMdlEvent]] = None
        self.theAutoLayers: Optional[list[SourceMdlAutoLayer]] = None
        self.theIkLocks: Optional[list[SourceMdlIkLock]] = None
        self.theBoneWeights: Optional[list[float]] = None
        self.theWeightListIndex: int = -1
        self.theAnimDescIndexes: Optional[list[int]] = None
        self.theKeyValues: str = ""
        self.theActivityModifiers: Optional[list[SourceMdlActivityModifier]] = None
        self.theAnimTags: Optional[list[SourceMdlAnimTag]] = None
        self.theBoneWeightsAreDefault: bool = False
        self.theCorrectiveAnimationName: str = ""


# ---------------------------------------------------------------------------
# Body parts / models / meshes
# ---------------------------------------------------------------------------

class SourceMdlVertAnim:
    """``SourceMdlVertAnim`` -- one ``mstudiovertanim_t``.

    ``theDelta``/``theNDelta`` hold three ``SourceFloat16bits`` each; in Python we
    store the decoded floats directly because that is all the writers use.
    """

    STUDIO_VERT_ANIM_NORMAL: int = 0
    STUDIO_VERT_ANIM_WRINKLE: int = 1

    def __init__(self) -> None:
        self.index: int = 0
        self.speed: int = 0
        self.side: int = 0
        self.theDelta: list[float] = [0.0, 0.0, 0.0]
        self.theNDelta: list[float] = [0.0, 0.0, 0.0]


class SourceMdlFlex:
    """``SourceMdlFlex`` -- one ``mstudioflex_t``."""

    def __init__(self) -> None:
        self.flexDescIndex: int = 0
        self.target0: float = 0.0
        self.target1: float = 0.0
        self.target2: float = 0.0
        self.target3: float = 0.0
        self.vertCount: int = 0
        self.vertOffset: int = 0
        self.flexDescPartnerIndex: int = 0
        self.vertAnimType: int = 0
        self.unusedChar: str = ""
        self.unused: list[int] = [0] * 6
        self.theVertAnims: Optional[list[SourceMdlVertAnim]] = None


class SourceMdlMesh:
    """``SourceMdlMesh`` -- one ``mstudiomesh_t``."""

    def __init__(self) -> None:
        self.materialIndex: int = 0
        self.modelOffset: int = 0
        self.vertexCount: int = 0
        self.vertexIndexStart: int = 0
        self.flexCount: int = 0
        self.flexOffset: int = 0
        self.materialType: int = 0
        self.materialParam: int = 0
        self.id: int = 0
        self.centerX: float = 0.0
        self.centerY: float = 0.0
        self.centerZ: float = 0.0
        self.vertexData: SourceMdlMeshVertexData = SourceMdlMeshVertexData()
        self.unused: list[int] = [0] * 8
        self.theFlexes: Optional[list[SourceMdlFlex]] = None


class SourceMdlEyeball:
    """``SourceMdlEyeball`` -- one ``mstudioeyeball_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.boneIndex: int = 0
        self.org: SourceVector = SourceVector()
        self.zOffset: float = 0.0
        self.radius: float = 0.0
        self.up: SourceVector = SourceVector()
        self.forward: SourceVector = SourceVector()
        self.texture: int = 0
        self.unused1: int = 0
        self.irisScale: float = 0.0
        self.unused2: int = 0
        # NOTE: the VB struct declares these as int[3] / float[3]; sizing them to 2
        # would desynchronise the read stream.
        self.upperFlexDesc: list[int] = [0, 0, 0]
        self.lowerFlexDesc: list[int] = [0, 0, 0]
        self.upperTarget: list[float] = [0.0, 0.0, 0.0]
        self.lowerTarget: list[float] = [0.0, 0.0, 0.0]
        self.upperLidFlexDesc: int = 0
        self.lowerLidFlexDesc: int = 0
        self.unused: list[int] = [0] * 4
        self.eyeballIsNonFacs: int = 0
        self.unused3: str = ""
        self.unused4: list[int] = [0] * 7
        self.theName: str = ""
        self.theTextureIndex: int = 0


class SourceMdlModel:
    """``SourceMdlModel`` -- one ``mstudiomodel_t``."""

    def __init__(self) -> None:
        self.name: str = ""
        self.type: int = 0
        self.boundingRadius: float = 0.0
        self.meshCount: int = 0
        self.meshOffset: int = 0
        self.vertexCount: int = 0
        self.vertexOffset: int = 0
        self.tangentOffset: int = 0
        self.attachmentCount: int = 0
        self.attachmentOffset: int = 0
        self.eyeballCount: int = 0
        self.eyeballOffset: int = 0
        self.vertexData: SourceMdlModelVertexData = SourceMdlModelVertexData()
        self.unused: list[int] = [0] * 8
        self.theSmdFileNames: list[str] = [""] * MAX_NUM_LODS
        self.theMeshes: Optional[list[SourceMdlMesh]] = None
        self.theEyeballs: Optional[list[SourceMdlEyeball]] = None


class SourceMdlBodyPart:
    """``SourceMdlBodyPart`` -- one ``mstudiobodyparts_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.modelCount: int = 0
        self.base: int = 0
        self.modelOffset: int = 0
        self.theName: str = ""
        self.theModels: Optional[list[SourceMdlModel]] = None
        self.theModelCommandIsUsed: bool = False
        self.theEyeballOptionIsUsed: bool = False
        self.theFlexFrames: Optional[list["FlexFrame"]] = None


# ---------------------------------------------------------------------------
# Flexes / IK / mouths / pose parameters
# ---------------------------------------------------------------------------

class SourceMdlFlexDesc:
    """``SourceMdlFlexDesc`` -- one ``mstudioflexdesc_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.theName: str = ""
        self.theDescIsUsedByFlex: bool = False
        self.theDescIsUsedByFlexRule: bool = False
        self.theDescIsUsedByEyelid: bool = False


class SourceMdlFlexController:
    """``SourceMdlFlexController`` -- one ``mstudioflexcontroller_t``."""

    def __init__(self) -> None:
        self.typeOffset: int = 0
        self.nameOffset: int = 0
        self.localToGlobal: int = 0
        self.min: float = 0.0
        self.max: float = 0.0
        self.theName: str = ""
        self.theType: str = ""


class SourceMdlFlexControllerUi:
    """``SourceMdlFlexControllerUi`` -- one ``mstudioflexcontrollerui_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.config0: int = 0
        self.config1: int = 0
        self.config2: int = 0
        self.remapType: int = 0
        self.controlIsStereo: int = 0
        self.unused: list[int] = [0] * 2
        self.theName: str = ""


class SourceMdlFlexRule:
    """``SourceMdlFlexRule`` -- one ``mstudioflexrule_t``."""

    def __init__(self) -> None:
        self.flexIndex: int = 0
        self.flexOpCount: int = 0
        self.flexOpOffset: int = 0
        self.theFlexOps: Optional[list[int]] = None


class SourceMdlIkLink:
    """``SourceMdlIkLink`` -- one ``mstudioiklink_t``."""

    def __init__(self) -> None:
        self.boneIndex: int = 0
        self.kneeDirX: float = 0.0
        self.kneeDirY: float = 0.0
        self.kneeDirZ: float = 0.0
        self.kneeDir: SourceVector = SourceVector()


class SourceMdlIkChain:
    """``SourceMdlIkChain`` -- one ``mstudioikchain_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.linkType: int = 0
        self.linkCount: int = 0
        self.linkOffset: int = 0
        self.theName: str = ""
        self.theLinks: Optional[list[SourceMdlIkLink]] = None


class SourceMdlMouth:
    """``SourceMdlMouth`` -- one ``mstudiomouth_t``."""

    def __init__(self) -> None:
        self.boneIndex: int = 0
        self.forward: SourceVector = SourceVector()
        self.flexDescIndex: int = 0


class SourceMdlPoseParamDesc:
    """``SourceMdlPoseParamDesc`` -- one ``mstudioposeparamdesc_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.flags: int = 0
        self.startingValue: float = 0.0
        self.endingValue: float = 0.0
        self.loopingRange: float = 0.0
        self.theName: str = ""


class SourceMdlModelGroup:
    """``SourceMdlModelGroup`` -- one ``mstudiomodelgroup_t``."""

    def __init__(self) -> None:
        self.labelOffset: int = 0
        self.fileNameOffset: int = 0
        self.theLabel: str = ""
        self.theFileName: str = ""


class SourceMdlBodygroupPreset:
    """``SourceMdlBodygroupPreset``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.value: int = 0
        self.mask: int = 0
        self.theName: str = ""


class SourceMdlWeightList:
    """``SourceMdlWeightList``."""

    def __init__(self) -> None:
        self.theName: str = ""
        self.theWeights: Optional[list[float]] = None


class SourceMdlTexture:
    """``SourceMdlTexture`` -- one ``mstudiotexture_t``."""

    def __init__(self) -> None:
        self.nameOffset: int = 0
        self.flags: int = 0
        self.used: int = 0
        self.unused1: int = 0
        self.materialP: int = 0
        self.clientMaterialP: int = 0
        self.unused: list[int] = [0] * 10
        self.thePathFileName: str = ""


class SourceMdlAnimationDesc49:
    """``SourceMdlAnimationDesc49`` -- one ``mstudioanimdesc_t`` for v44..v59."""

    STUDIO_LOOPING: int = 0x1
    STUDIO_SNAP: int = 0x2
    STUDIO_DELTA: int = 0x4
    STUDIO_AUTOPLAY: int = 0x8
    STUDIO_POST: int = 0x10
    STUDIO_ALLZEROS: int = 0x20
    STUDIO_FRAMEANIM: int = 0x40
    STUDIO_CYCLEPOSE: int = 0x80
    STUDIO_REALTIME: int = 0x100
    STUDIO_LOCAL: int = 0x200
    STUDIO_HIDDEN: int = 0x400
    STUDIO_OVERRIDE: int = 0x800
    STUDIO_ACTIVITY: int = 0x1000
    STUDIO_EVENT: int = 0x2000
    STUDIO_WORLD: int = 0x4000
    STUDIO_NOFORCELOOP: int = 0x8000
    STUDIO_EVENT_CLIENT: int = 0x10000

    def __init__(self) -> None:
        self.baseHeaderOffset: int = 0
        self.nameOffset: int = 0
        self.fps: float = 0.0
        self.flags: int = 0
        self.frameCount: int = 0
        self.movementCount: int = 0
        self.movementOffset: int = 0
        self.ikRuleZeroFrameOffset_VERSION49: int = 0
        self.unused1: list[int] = [0] * 4
        self.animBlock: int = 0
        self.animOffset: int = 0
        self.ikRuleCount: int = 0
        self.ikRuleOffset: int = 0
        self.animblockIkRuleOffset: int = 0
        self.localHierarchyCount: int = 0
        self.localHierarchyOffset: int = 0
        self.sectionOffset: int = 0
        self.sectionFrameCount: int = 0
        self.spanFrameCount: int = 0
        self.spanCount: int = 0
        self.spanOffset: int = 0
        self.spanStallTime: float = 0.0
        self.theSectionsOfAnimations: Optional[list[list[object]]] = None
        self.theSectionsOfFrameAnim: Optional[list[object]] = None
        self.theIkRules: Optional[list[object]] = None
        self.theSections: Optional[list[object]] = None
        self.theMovements: Optional[list[object]] = None
        self.theLocalHierarchies: Optional[list[object]] = None
        self.theAnimIsLinkedToSequence: bool = False
        self.theLinkedSequences: Optional[list[SourceMdlSequenceDesc]] = None
        self.theOffsetStart: int = 0
        self.theName: str = ""
        self.theSmdRelativePathFileName: str = ""
        self.theCorrectiveAnimationName: str = ""


class FlexFrame:
    """``FlexFrame`` -- one entry of a body part's vertex-animation frame list."""

    def __init__(self) -> None:
        self.flexName: str = ""
        self.flexDescription: str = ""
        self.flexHasPartner: bool = False
        self.flexPartnerName: str = ""
        self.flexSplit: float = 0.0
        self.bodyAndMeshVertexIndexStarts: list[int] = []
        self.flexes: list[SourceMdlFlex] = []
        self.meshIndexes: list[int] = []


# ---------------------------------------------------------------------------
# The MDL file data root
# ---------------------------------------------------------------------------

class SourceMdlFileData49:
    """``SourceMdlFileData49`` -- everything parsed out of one MDL.

    The header field order matches ``SourceMdlFile49.ReadMdlHeader00`` /
    ``ReadMdlHeader01`` / ``ReadMdlHeader02`` exactly.
    """

    def __init__(self) -> None:
        # --- ReadMdlHeader00 -------------------------------------------------
        self.id: str = ""
        self.version: int = 0
        self.checksum: int = 0
        self.name: str = ""
        self.fileSize: int = 0
        self.theActualFileSize: int = 0

        # --- ReadMdlHeader01 -------------------------------------------------
        self.eyePosition: SourceVector = SourceVector()
        self.illuminationPosition: SourceVector = SourceVector()
        self.hullMinPosition: SourceVector = SourceVector()
        self.hullMaxPosition: SourceVector = SourceVector()
        self.viewBoundingBoxMinPosition: SourceVector = SourceVector()
        self.viewBoundingBoxMaxPosition: SourceVector = SourceVector()
        self.flags: int = 0
        self.boneCount: int = 0
        self.boneOffset: int = 0
        self.boneControllerCount: int = 0
        self.boneControllerOffset: int = 0
        self.hitboxSetCount: int = 0
        self.hitboxSetOffset: int = 0
        self.localAnimationCount: int = 0
        self.localAnimationOffset: int = 0
        self.localSequenceCount: int = 0
        self.localSequenceOffset: int = 0
        self.activityListVersion: int = 0
        self.eventsIndexed: int = 0
        self.textureCount: int = 0
        self.textureOffset: int = 0
        self.texturePathCount: int = 0
        self.texturePathOffset: int = 0
        self.skinReferenceCount: int = 0
        self.skinFamilyCount: int = 0
        self.skinFamilyOffset: int = 0
        self.bodyPartCount: int = 0
        self.bodyPartOffset: int = 0
        self.localAttachmentCount: int = 0
        self.localAttachmentOffset: int = 0
        self.localNodeCount: int = 0
        self.localNodeOffset: int = 0
        self.localNodeNameOffset: int = 0
        self.flexDescCount: int = 0
        self.flexDescOffset: int = 0
        self.flexControllerCount: int = 0
        self.flexControllerOffset: int = 0
        self.flexRuleCount: int = 0
        self.flexRuleOffset: int = 0
        self.ikChainCount: int = 0
        self.ikChainOffset: int = 0
        self.mouthCount: int = 0
        self.mouthOffset: int = 0
        self.localPoseParamaterCount: int = 0
        self.localPoseParameterOffset: int = 0
        self.surfacePropOffset: int = 0
        self.keyValueOffset: int = 0
        self.keyValueSize: int = 0
        self.localIkAutoPlayLockCount: int = 0
        self.localIkAutoPlayLockOffset: int = 0
        self.mass: float = 0.0
        self.contents: int = 0
        self.includeModelCount: int = 0
        self.includeModelOffset: int = 0
        self.virtualModelP: int = 0
        self.animBlockNameOffset: int = 0
        self.animBlockCount: int = 0
        self.animBlockOffset: int = 0
        self.animBlockModelP: int = 0
        self.boneTableByNameOffset: int = 0
        self.vertexBaseP: int = 0
        self.indexBaseP: int = 0
        self.directionalLightDot: int = 0
        self.rootLod: int = 0
        self.allowedRootLodCount_VERSION48: int = 0
        self.unused: int = 0
        self.zeroframecacheindex_VERSION44to47: int = 0
        self.flexControllerUiCount: int = 0
        self.flexControllerUiOffset: int = 0
        self.vertAnimFixedPointScale: float = 0.0
        self.surfacePropLookup: int = 0
        self.studioHeader2Offset: int = 0
        self.unused2: int = 0

        # --- ReadMdlHeader02 -------------------------------------------------
        self.sourceBoneTransformCount: int = 0
        self.sourceBoneTransformOffset: int = 0
        self.illumPositionAttachmentNumber: int = 0
        self.maxEyeDeflection: float = 0.0
        self.linearBoneOffset: int = 0
        self.nameCopyOffset: int = 0
        self.boneFlexDriverCount: int = 0
        self.boneFlexDriverOffset: int = 0
        self.unknownValue: int = 0
        self.bodygroupPresetCount: int = 0
        self.bodygroupPresetOffset: int = 0
        self.reserved: list[int] = [0] * 53

        # --- derived / referenced data --------------------------------------
        self.theModelName: str = ""
        self.theNameCopy: str = ""
        self.theSurfacePropName: str = ""
        self.theAnimBlockRelativePathFileName: str = ""
        self.theAnimBlocks: Optional[list[SourceMdlAnimBlock]] = None
        self.theAnimationDescs: Optional[list[SourceMdlAnimationDesc49]] = None
        self.theAttachments: Optional[list[SourceMdlAttachment]] = None
        self.theBodygroupPresets: Optional[list[SourceMdlBodygroupPreset]] = None
        self.theBodyParts: Optional[list[SourceMdlBodyPart]] = None
        self.theBoneControllers: Optional[list[SourceMdlBoneController]] = None
        self.theBones: Optional[list[SourceMdlBone]] = None
        self.theBoneTableByName: Optional[list[int]] = None
        self.theBoneTransforms: Optional[list[object]] = None
        self.theFlexControllers: Optional[list[SourceMdlFlexController]] = None
        self.theFlexControllerUis: Optional[list[SourceMdlFlexControllerUi]] = None
        self.theFlexDescs: Optional[list[SourceMdlFlexDesc]] = None
        self.theFlexRules: Optional[list[SourceMdlFlexRule]] = None
        self.theHitboxSets: Optional[list[SourceMdlHitboxSet]] = None
        self.theIkChains: Optional[list[SourceMdlIkChain]] = None
        self.theIkLocks: Optional[list[SourceMdlIkLock]] = None
        self.theKeyValuesText: str = ""
        self.theLinearBoneTable: Optional[object] = None
        self.theLocalNodeNames: Optional[list[str]] = None
        self.theLocalNodes: Optional[list[list[int]]] = None
        self.theModelGroups: Optional[list[SourceMdlModelGroup]] = None
        self.theMouths: Optional[list[SourceMdlMouth]] = None
        self.thePoseParamDescs: Optional[list[SourceMdlPoseParamDesc]] = None
        self.theSequenceDescs: Optional[list[SourceMdlSequenceDesc]] = None
        self.theSkinFamilies: Optional[list[list[int]]] = None
        self.theTexturePaths: Optional[list[str]] = None
        self.theTextures: Optional[list[SourceMdlTexture]] = None
        self.theWeightLists: list[SourceMdlWeightList] = []
        self.theModifiedTexturePaths: Optional[list[str]] = None
        self.theModifiedTextureFileNames: Optional[list[str]] = None

        # --- state flags -----------------------------------------------------
        self.theMdlFileOnlyHasAnimations: bool = False
        self.theProceduralBonesCommandIsUsed: bool = False
        self.theAnimBlockSizeNoStallOptionIsUsed: bool = False
        self.theBoneNameToBoneIndexMap: dict[str, int] = {}
        self.theOriginalBoneNames: Optional[tuple[str, ...]] = None
        self.theBoneNameRemap: dict[int, str] = {}
        self.theCorrectiveAnimationDescs: Optional[list[SourceMdlAnimationDesc49]] = None
        self.theSectionFrameCount: int = 0
        self.theSectionFrameMinFrameCount: int = 2000000
