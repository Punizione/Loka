"""Read Source model headers and shared model data."""

from __future__ import annotations

from typing import Optional

from .constants import (
    MAX_LOCAL_NODES,
    MAX_STUDIO_HITBOX_SETS,
    STUDIO_PROC_AIMATATTACH,
    STUDIO_PROC_AIMATBONE,
    STUDIO_PROC_AXISINTERP,
    STUDIO_PROC_JIGGLE,
    STUDIO_PROC_QUATINTERP,
    TEXT_SCAL_VERSION44_VINDICTUS,
)
from .mdl_data import (
    SourceMdlAimAtBone,
    SourceMdlAnimBlock,
    SourceMdlAttachment,
    SourceMdlAxisInterpBone,
    SourceMdlBodyPart,
    SourceMdlBone,
    SourceMdlBoneController,
    SourceMdlFileData49,
    SourceMdlHitbox,
    SourceMdlHitboxSet,
    SourceMdlJiggleBone,
    SourceMdlMesh,
    SourceMdlMeshVertexData,
    SourceMdlModel,
    SourceMdlModelVertexData,
    SourceMdlQuatInterpBone,
    SourceMdlQuatInterpInfo,
    SourceMdlTexture,
)
from .reader import BinaryReader, SEEK_BEGIN

# The parser is split across mixins so that each area can be ported and reviewed
# independently, mirroring the section comments in SourceMdlFile49.  Each import
# degrades gracefully so the parser stays usable while an area is still in flight.
try:
    from .mdl_file_seq import SourceMdlFile49SequenceMixin
except ImportError:  # pragma: no cover - keeps the parser usable without it

    class SourceMdlFile49SequenceMixin:  # type: ignore[no-redef]
        """Fallback base when :mod:`loka.core.mdl_file_seq` is unavailable."""

        def read_sequence_descs(self) -> None:
            return


try:
    from .mdl_file_model import SourceMdlFile49ModelMixin
except ImportError:  # pragma: no cover - keeps the parser usable without it

    class SourceMdlFile49ModelMixin:  # type: ignore[no-redef]
        """Fallback base when :mod:`loka.core.mdl_file_model` is unavailable."""

        def read_eyeballs(self, model_pos: int, a_model) -> None:
            return

        def read_flexes(self, mesh_pos: int, a_mesh) -> None:
            return


try:  # The animation decoder lives in its own module (ported separately).
    from .ani import SourceMdlFile49AnimationMixin
except ImportError:  # pragma: no cover - keeps the parser usable without it

    class SourceMdlFile49AnimationMixin:  # type: ignore[no-redef]
        """Fallback base when :mod:`loka.core.ani` is unavailable."""


__all__ = ["SourceMdlFile49"]


class SourceMdlFile49(
    SourceMdlFile49SequenceMixin,
    SourceMdlFile49ModelMixin,
    SourceMdlFile49AnimationMixin,
):
    """Implement ``SourceMdlFile49``.

    The image is a single VB class; here it is assembled from mixins that each cover
    one section of the original file, so that a missing area cannot break the rest.
    """

    def __init__(self, reader: BinaryReader, mdl_file_data: SourceMdlFileData49) -> None:
        self.theInputFileReader = reader
        self.theMdlFileData = mdl_file_data

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    def read_mdl_header00(self, log_description: str) -> None:
        """Implement ``ReadMdlHeader00`` -- the ``IDST``/``MDLZ`` prefix + name."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        data.id = reader.read_chars(4)
        data.version = reader.read_int32()
        data.checksum = reader.read_int32()
        data.name = reader.read_chars(64)
        data.theModelName = data.name.strip("\x00")
        data.fileSize = reader.read_int32()
        data.theActualFileSize = reader.length

    def read_mdl_header01(self, log_description: str) -> None:
        """Implement ``ReadMdlHeader01`` -- the bulk of ``studiohdr_t``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        for vector, field in (
            (data.eyePosition, "eyePosition"),
            (data.illuminationPosition, "illuminationPosition"),
            (data.hullMinPosition, "hullMinPosition"),
            (data.hullMaxPosition, "hullMaxPosition"),
            (data.viewBoundingBoxMinPosition, "viewBoundingBoxMinPosition"),
            (data.viewBoundingBoxMaxPosition, "viewBoundingBoxMaxPosition"),
        ):
            vector.x = reader.read_single()
            vector.y = reader.read_single()
            vector.z = reader.read_single()

        data.flags = reader.read_int32()

        data.boneCount = reader.read_int32()
        data.boneOffset = reader.read_int32()

        data.boneControllerCount = reader.read_int32()
        data.boneControllerOffset = reader.read_int32()

        data.hitboxSetCount = reader.read_int32()
        data.hitboxSetOffset = reader.read_int32()
        # FROM studiomdl: MAXSTUDIOHITBOXSETNAME limits the usable count.
        if data.hitboxSetCount > MAX_STUDIO_HITBOX_SETS:
            data.hitboxSetCount = MAX_STUDIO_HITBOX_SETS

        data.localAnimationCount = reader.read_int32()
        data.localAnimationOffset = reader.read_int32()

        data.localSequenceCount = reader.read_int32()
        data.localSequenceOffset = reader.read_int32()

        data.activityListVersion = reader.read_int32()
        data.eventsIndexed = reader.read_int32()

        data.textureCount = reader.read_int32()
        data.textureOffset = reader.read_int32()

        data.texturePathCount = reader.read_int32()
        data.texturePathOffset = reader.read_int32()

        data.skinReferenceCount = reader.read_int32()
        data.skinFamilyCount = reader.read_int32()
        data.skinFamilyOffset = reader.read_int32()

        data.bodyPartCount = reader.read_int32()
        data.bodyPartOffset = reader.read_int32()

        data.localAttachmentCount = reader.read_int32()
        data.localAttachmentOffset = reader.read_int32()

        data.localNodeCount = reader.read_int32()
        data.localNodeOffset = reader.read_int32()
        data.localNodeNameOffset = reader.read_int32()
        # FROM studiomdl: EXTERN char *g_xnodename[100].
        if data.localNodeCount > MAX_LOCAL_NODES:
            data.localNodeCount = MAX_LOCAL_NODES

        data.flexDescCount = reader.read_int32()
        data.flexDescOffset = reader.read_int32()
        data.flexControllerCount = reader.read_int32()
        data.flexControllerOffset = reader.read_int32()
        data.flexRuleCount = reader.read_int32()
        data.flexRuleOffset = reader.read_int32()

        data.ikChainCount = reader.read_int32()
        data.ikChainOffset = reader.read_int32()

        data.mouthCount = reader.read_int32()
        data.mouthOffset = reader.read_int32()

        data.localPoseParamaterCount = reader.read_int32()
        data.localPoseParameterOffset = reader.read_int32()

        data.surfacePropOffset = reader.read_int32()
        if data.surfacePropOffset > 0:
            saved = reader.position
            reader.seek(data.surfacePropOffset, SEEK_BEGIN)
            data.theSurfacePropName = reader.read_null_terminated_string()
            reader.seek(saved, SEEK_BEGIN)
        else:
            data.theSurfacePropName = ""

        data.keyValueOffset = reader.read_int32()
        data.keyValueSize = reader.read_int32()

        data.localIkAutoPlayLockCount = reader.read_int32()
        data.localIkAutoPlayLockOffset = reader.read_int32()

        data.mass = reader.read_single()
        data.contents = reader.read_int32()

        data.includeModelCount = reader.read_int32()
        data.includeModelOffset = reader.read_int32()

        data.virtualModelP = reader.read_int32()

        data.animBlockNameOffset = reader.read_int32()
        data.animBlockCount = reader.read_int32()
        data.animBlockOffset = reader.read_int32()
        data.animBlockModelP = reader.read_int32()
        if data.animBlockCount > 0:
            if data.animBlockNameOffset > 0:
                saved = reader.position
                reader.seek(data.animBlockNameOffset, SEEK_BEGIN)
                data.theAnimBlockRelativePathFileName = reader.read_null_terminated_string()
                reader.seek(saved, SEEK_BEGIN)
            if data.animBlockOffset > 0:
                saved = reader.position
                reader.seek(data.animBlockOffset, SEEK_BEGIN)
                data.theAnimBlocks = []
                for _ in range(data.animBlockCount):
                    an_anim_block = SourceMdlAnimBlock()
                    an_anim_block.dataStart = reader.read_int32()
                    an_anim_block.dataEnd = reader.read_int32()
                    data.theAnimBlocks.append(an_anim_block)
                reader.seek(saved, SEEK_BEGIN)

        data.boneTableByNameOffset = reader.read_int32()

        data.vertexBaseP = reader.read_int32()
        data.indexBaseP = reader.read_int32()

        data.directionalLightDot = reader.read_byte()
        data.rootLod = reader.read_byte()
        data.allowedRootLodCount_VERSION48 = reader.read_byte()
        data.unused = reader.read_byte()

        data.zeroframecacheindex_VERSION44to47 = reader.read_int32()

        data.flexControllerUiCount = reader.read_int32()
        data.flexControllerUiOffset = reader.read_int32()

        data.vertAnimFixedPointScale = reader.read_single()
        data.surfacePropLookup = reader.read_int32()

        data.studioHeader2Offset = reader.read_int32()

        data.unused2 = reader.read_int32()

        if data.bodyPartCount == 0 and data.localSequenceCount > 0:
            data.theMdlFileOnlyHasAnimations = True

    def read_mdl_header02(self, log_description: str) -> None:
        """Implement ``ReadMdlHeader02`` -- the ``studiohdr2_t`` block.

        NOTE: ``bodygroupPresetOffset`` is stored *relative to the start of this
        structure* (i.e. to ``studioHeader2Offset``), which is why the VB adds
        ``fileOffsetStart``.
        """
        reader = self.theInputFileReader
        data = self.theMdlFileData

        file_offset_start = reader.position

        data.sourceBoneTransformCount = reader.read_int32()
        data.sourceBoneTransformOffset = reader.read_int32()
        data.illumPositionAttachmentNumber = reader.read_int32()
        data.maxEyeDeflection = reader.read_single()
        data.linearBoneOffset = reader.read_int32()

        data.nameCopyOffset = reader.read_int32()
        if data.nameCopyOffset > 0:
            saved = reader.position
            reader.seek(file_offset_start + data.nameCopyOffset, SEEK_BEGIN)
            data.theNameCopy = reader.read_null_terminated_string()
            data.theModelName = data.theNameCopy
            reader.seek(saved, SEEK_BEGIN)
        else:
            data.theNameCopy = ""

        data.boneFlexDriverCount = reader.read_int32()
        data.boneFlexDriverOffset = reader.read_int32()

        data.unknownValue = reader.read_int32()

        data.bodygroupPresetCount = reader.read_int32()
        data.bodygroupPresetOffset = file_offset_start + reader.read_int32()

        for i in range(len(data.reserved)):
            data.reserved[i] = reader.read_int32()

    # ------------------------------------------------------------------
    # Bones
    # ------------------------------------------------------------------
    def read_bones(self) -> None:
        """Implement ``ReadBones``."""
        data = self.theMdlFileData
        if data.boneCount <= 0:
            return

        reader = self.theInputFileReader
        try:
            reader.seek(data.boneOffset, SEEK_BEGIN)

            data.theBones = []
            for bone_index in range(data.boneCount):
                bone_pos = reader.position
                a_bone = SourceMdlBone()

                a_bone.nameOffset = reader.read_int32()
                a_bone.parentBoneIndex = reader.read_int32()

                for j in range(len(a_bone.boneControllerIndex)):
                    a_bone.boneControllerIndex[j] = reader.read_int32()

                a_bone.position.x = reader.read_single()
                a_bone.position.y = reader.read_single()
                a_bone.position.z = reader.read_single()

                a_bone.quat.x = reader.read_single()
                a_bone.quat.y = reader.read_single()
                a_bone.quat.z = reader.read_single()
                a_bone.quat.w = reader.read_single()

                a_bone.rotation.x = reader.read_single()
                a_bone.rotation.y = reader.read_single()
                a_bone.rotation.z = reader.read_single()

                a_bone.positionScale.x = reader.read_single()
                a_bone.positionScale.y = reader.read_single()
                a_bone.positionScale.z = reader.read_single()

                a_bone.rotationScale.x = reader.read_single()
                a_bone.rotationScale.y = reader.read_single()
                a_bone.rotationScale.z = reader.read_single()

                # NOTE: stored column-major as .x of each column, then .y, then .z.
                a_bone.poseToBoneColumn0.x = reader.read_single()
                a_bone.poseToBoneColumn1.x = reader.read_single()
                a_bone.poseToBoneColumn2.x = reader.read_single()
                a_bone.poseToBoneColumn3.x = reader.read_single()
                a_bone.poseToBoneColumn0.y = reader.read_single()
                a_bone.poseToBoneColumn1.y = reader.read_single()
                a_bone.poseToBoneColumn2.y = reader.read_single()
                a_bone.poseToBoneColumn3.y = reader.read_single()
                a_bone.poseToBoneColumn0.z = reader.read_single()
                a_bone.poseToBoneColumn1.z = reader.read_single()
                a_bone.poseToBoneColumn2.z = reader.read_single()
                a_bone.poseToBoneColumn3.z = reader.read_single()

                a_bone.qAlignment.x = reader.read_single()
                a_bone.qAlignment.y = reader.read_single()
                a_bone.qAlignment.z = reader.read_single()
                a_bone.qAlignment.w = reader.read_single()

                a_bone.flags = reader.read_int32()

                a_bone.proceduralRuleType = reader.read_int32()
                a_bone.proceduralRuleOffset = reader.read_int32()
                a_bone.physicsBoneIndex = reader.read_int32()
                a_bone.surfacePropNameOffset = reader.read_int32()
                a_bone.contents = reader.read_int32()

                for k in range(8):
                    a_bone.unused[k] = reader.read_int32()

                data.theBones.append(a_bone)

                input_pos = reader.position

                if a_bone.nameOffset != 0:
                    reader.seek(bone_pos + a_bone.nameOffset, SEEK_BEGIN)
                    a_bone.theName = reader.read_null_terminated_string()
                else:
                    a_bone.theName = ""

                # MDL > 37 can have several bones sharing a name, so only the first wins.
                if a_bone.theName not in data.theBoneNameToBoneIndexMap:
                    data.theBoneNameToBoneIndexMap[a_bone.theName] = bone_index

                if a_bone.proceduralRuleOffset != 0:
                    if a_bone.proceduralRuleType == STUDIO_PROC_AXISINTERP:
                        self.read_axis_interp_bone(bone_pos, a_bone)
                    elif a_bone.proceduralRuleType == STUDIO_PROC_QUATINTERP:
                        data.theProceduralBonesCommandIsUsed = True
                        self.read_quat_interp_bone(bone_pos, a_bone)
                    elif a_bone.proceduralRuleType in (
                        STUDIO_PROC_AIMATBONE,
                        STUDIO_PROC_AIMATATTACH,
                    ):
                        data.theProceduralBonesCommandIsUsed = True
                        self.read_aim_at_bone(bone_pos, a_bone)
                    elif a_bone.proceduralRuleType == STUDIO_PROC_JIGGLE:
                        self.read_jiggle_bone(bone_pos, a_bone)

                if a_bone.surfacePropNameOffset != 0:
                    reader.seek(bone_pos + a_bone.surfacePropNameOffset, SEEK_BEGIN)
                    a_bone.theSurfacePropName = reader.read_null_terminated_string()
                else:
                    a_bone.theSurfacePropName = ""

                reader.seek(input_pos, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_axis_interp_bone(self, bone_pos: int, a_bone: SourceMdlBone) -> None:
        """Implement ``ReadAxisInterpBone``.

        NOTE: the VB reads ``quat(x).z`` twice (a bug in the original) -- that is
        reproduced here so the file position after the block matches exactly.
        """
        reader = self.theInputFileReader
        try:
            reader.seek(bone_pos + a_bone.proceduralRuleOffset, SEEK_BEGIN)

            a_bone.theAxisInterpBone = SourceMdlAxisInterpBone()
            a_bone.theAxisInterpBone.control = reader.read_int32()
            for x in range(len(a_bone.theAxisInterpBone.pos)):
                a_bone.theAxisInterpBone.pos[x].x = reader.read_single()
                a_bone.theAxisInterpBone.pos[x].y = reader.read_single()
                a_bone.theAxisInterpBone.pos[x].z = reader.read_single()
            for x in range(len(a_bone.theAxisInterpBone.quat)):
                a_bone.theAxisInterpBone.quat[x].x = reader.read_single()
                a_bone.theAxisInterpBone.quat[x].y = reader.read_single()
                a_bone.theAxisInterpBone.quat[x].z = reader.read_single()
                a_bone.theAxisInterpBone.quat[x].z = reader.read_single()
        except MemoryError:
            raise
        except Exception:
            pass

    def read_quat_interp_bone(self, bone_pos: int, a_bone: SourceMdlBone) -> None:
        """Implement ``ReadQuatInterpBone``."""
        reader = self.theInputFileReader
        try:
            reader.seek(bone_pos + a_bone.proceduralRuleOffset, SEEK_BEGIN)

            quat_pos = reader.position
            a_bone.theQuatInterpBone = SourceMdlQuatInterpBone()
            a_bone.theQuatInterpBone.controlBoneIndex = reader.read_int32()
            a_bone.theQuatInterpBone.triggerCount = reader.read_int32()
            a_bone.theQuatInterpBone.triggerOffset = reader.read_int32()

            input_pos = reader.position
            if a_bone.theQuatInterpBone.triggerCount > 0 and a_bone.theQuatInterpBone.triggerOffset != 0:
                self.read_triggers(quat_pos, a_bone.theQuatInterpBone)
            reader.seek(input_pos, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_triggers(self, quat_interp_bone_pos: int, a_quat_interp_bone: SourceMdlQuatInterpBone) -> None:
        """Implement ``ReadTriggers``."""
        reader = self.theInputFileReader
        try:
            reader.seek(quat_interp_bone_pos + a_quat_interp_bone.triggerOffset, SEEK_BEGIN)

            a_quat_interp_bone.theTriggers = []
            for _ in range(a_quat_interp_bone.triggerCount):
                a_trigger = SourceMdlQuatInterpInfo()

                a_trigger.inverseToleranceAngle = reader.read_single()

                a_trigger.trigger.x = reader.read_single()
                a_trigger.trigger.y = reader.read_single()
                a_trigger.trigger.z = reader.read_single()
                a_trigger.trigger.w = reader.read_single()

                a_trigger.pos.x = reader.read_single()
                a_trigger.pos.y = reader.read_single()
                a_trigger.pos.z = reader.read_single()

                a_trigger.quat.x = reader.read_single()
                a_trigger.quat.y = reader.read_single()
                a_trigger.quat.z = reader.read_single()
                a_trigger.quat.w = reader.read_single()

                a_quat_interp_bone.theTriggers.append(a_trigger)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_aim_at_bone(self, bone_pos: int, a_bone: SourceMdlBone) -> None:
        """Implement ``ReadAimAtBone``."""
        reader = self.theInputFileReader
        try:
            reader.seek(bone_pos + a_bone.proceduralRuleOffset, SEEK_BEGIN)

            a_bone.theAimAtBone = SourceMdlAimAtBone()
            a_bone.theAimAtBone.parentBoneIndex = reader.read_int32()
            a_bone.theAimAtBone.aimBoneOrAttachmentIndex = reader.read_int32()

            a_bone.theAimAtBone.aim.x = reader.read_single()
            a_bone.theAimAtBone.aim.y = reader.read_single()
            a_bone.theAimAtBone.aim.z = reader.read_single()

            a_bone.theAimAtBone.up.x = reader.read_single()
            a_bone.theAimAtBone.up.y = reader.read_single()
            a_bone.theAimAtBone.up.z = reader.read_single()

            a_bone.theAimAtBone.basePos.x = reader.read_single()
            a_bone.theAimAtBone.basePos.y = reader.read_single()
            a_bone.theAimAtBone.basePos.z = reader.read_single()
        except MemoryError:
            raise
        except Exception:
            pass

    def read_jiggle_bone(self, bone_pos: int, a_bone: SourceMdlBone) -> None:
        """Implement ``ReadJiggleBone``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(bone_pos + a_bone.proceduralRuleOffset, SEEK_BEGIN)

        jiggle = SourceMdlJiggleBone()
        a_bone.theJiggleBone = jiggle

        jiggle.flags = reader.read_int32()
        jiggle.length = reader.read_single()
        jiggle.tipMass = reader.read_single()

        jiggle.yawStiffness = reader.read_single()
        jiggle.yawDamping = reader.read_single()
        jiggle.pitchStiffness = reader.read_single()
        jiggle.pitchDamping = reader.read_single()
        jiggle.alongStiffness = reader.read_single()
        jiggle.alongDamping = reader.read_single()

        jiggle.angleLimit = reader.read_single()

        jiggle.minYaw = reader.read_single()
        jiggle.maxYaw = reader.read_single()
        jiggle.yawFriction = reader.read_single()
        jiggle.yawBounce = reader.read_single()

        jiggle.minPitch = reader.read_single()
        jiggle.maxPitch = reader.read_single()
        jiggle.pitchFriction = reader.read_single()
        jiggle.pitchBounce = reader.read_single()

        jiggle.baseMass = reader.read_single()
        jiggle.baseStiffness = reader.read_single()
        jiggle.baseDamping = reader.read_single()
        jiggle.baseMinLeft = reader.read_single()
        jiggle.baseMaxLeft = reader.read_single()
        jiggle.baseLeftFriction = reader.read_single()
        jiggle.baseMinUp = reader.read_single()
        jiggle.baseMaxUp = reader.read_single()
        jiggle.baseUpFriction = reader.read_single()
        jiggle.baseMinForward = reader.read_single()
        jiggle.baseMaxForward = reader.read_single()
        jiggle.baseForwardFriction = reader.read_single()

        # The boing block is only present for Source SDK Base 2013-era models.
        if (jiggle.flags & SourceMdlJiggleBone.JIGGLE_IS_BOING) > 0 and data.version in (48, 49):
            jiggle.boingImpactSpeed = reader.read_single()
            jiggle.boingImpactAngle = reader.read_single()
            jiggle.boingDampingRate = reader.read_single()
            jiggle.boingFrequency = reader.read_single()
            jiggle.boingAmplitude = reader.read_single()

    # ------------------------------------------------------------------
    # Bone controllers / attachments / hitboxes
    # ------------------------------------------------------------------
    def read_bone_controllers(self) -> None:
        """Implement ``ReadBoneControllers``."""
        data = self.theMdlFileData
        if data.boneControllerCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.boneControllerOffset, SEEK_BEGIN)

        data.theBoneControllers = []
        for _ in range(data.boneControllerCount):
            a_bone_controller = SourceMdlBoneController()

            a_bone_controller.boneIndex = reader.read_int32()
            a_bone_controller.type = reader.read_int32()
            a_bone_controller.startBlah = reader.read_single()
            a_bone_controller.endBlah = reader.read_single()
            a_bone_controller.restIndex = reader.read_int32()
            a_bone_controller.inputField = reader.read_int32()
            if data.version > 10:
                for x in range(len(a_bone_controller.unused)):
                    a_bone_controller.unused[x] = reader.read_int32()

            data.theBoneControllers.append(a_bone_controller)

    def read_attachments(self) -> None:
        """Implement ``ReadAttachments`` (the non-v10 branch)."""
        data = self.theMdlFileData
        if data.localAttachmentCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.localAttachmentOffset, SEEK_BEGIN)

        data.theAttachments = []
        for _ in range(data.localAttachmentCount):
            attachment_pos = reader.position
            an_attachment = SourceMdlAttachment()

            if data.version == 10:
                an_attachment.name = reader.read_chars(32)
                an_attachment.theName = an_attachment.name.strip("\x00")
                an_attachment.type = reader.read_int32()
                an_attachment.bone = reader.read_int32()
                an_attachment.attachmentPoint.x = reader.read_single()
                an_attachment.attachmentPoint.y = reader.read_single()
                an_attachment.attachmentPoint.z = reader.read_single()
                for x in range(3):
                    an_attachment.vectors[x].x = reader.read_single()
                    an_attachment.vectors[x].y = reader.read_single()
                    an_attachment.vectors[x].z = reader.read_single()
            else:
                an_attachment.nameOffset = reader.read_int32()
                an_attachment.flags = reader.read_int32()
                an_attachment.localBoneIndex = reader.read_int32()
                an_attachment.localM11 = reader.read_single()
                an_attachment.localM12 = reader.read_single()
                an_attachment.localM13 = reader.read_single()
                an_attachment.localM14 = reader.read_single()
                an_attachment.localM21 = reader.read_single()
                an_attachment.localM22 = reader.read_single()
                an_attachment.localM23 = reader.read_single()
                an_attachment.localM24 = reader.read_single()
                an_attachment.localM31 = reader.read_single()
                an_attachment.localM32 = reader.read_single()
                an_attachment.localM33 = reader.read_single()
                an_attachment.localM34 = reader.read_single()
                for x in range(8):
                    an_attachment.unused[x] = reader.read_int32()

            data.theAttachments.append(an_attachment)

            input_pos = reader.position
            if an_attachment.nameOffset != 0:
                reader.seek(attachment_pos + an_attachment.nameOffset, SEEK_BEGIN)
                an_attachment.theName = reader.read_null_terminated_string()
            else:
                an_attachment.theName = ""
            reader.seek(input_pos, SEEK_BEGIN)

    def read_hitbox_sets(self) -> None:
        """Implement ``ReadHitboxSets``."""
        data = self.theMdlFileData
        if data.hitboxSetCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.hitboxSetOffset, SEEK_BEGIN)

        data.theHitboxSets = []
        try:
            for _ in range(data.hitboxSetCount):
                hitbox_set_pos = reader.position
                a_hitbox_set = SourceMdlHitboxSet()
                a_hitbox_set.nameOffset = reader.read_int32()
                a_hitbox_set.hitboxCount = reader.read_int32()
                a_hitbox_set.hitboxOffset = reader.read_int32()
                data.theHitboxSets.append(a_hitbox_set)

                input_pos = reader.position

                if a_hitbox_set.nameOffset > 0:
                    reader.seek(hitbox_set_pos + a_hitbox_set.nameOffset, SEEK_BEGIN)
                    a_hitbox_set.theName = reader.read_null_terminated_string()
                else:
                    a_hitbox_set.theName = ""

                self.read_hitboxes(hitbox_set_pos + a_hitbox_set.hitboxOffset, a_hitbox_set)
                reader.seek(input_pos, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            # the parser drops a trailing empty hitbox set if the read ran off the end.
            if data.theHitboxSets:
                last = data.theHitboxSets[-1]
                if last.theName == "" and last.theHitboxes is not None and len(last.theHitboxes) == 0:
                    data.theHitboxSets.remove(last)

    def read_hitboxes(self, hitbox_offset_pos: int, a_hitbox_set: SourceMdlHitboxSet) -> None:
        """Implement ``ReadHitboxes``."""
        if a_hitbox_set.hitboxCount <= 0:
            return

        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(hitbox_offset_pos, SEEK_BEGIN)

        a_hitbox_set.theHitboxes = []
        for _ in range(a_hitbox_set.hitboxCount):
            hitbox_pos = reader.position
            a_hitbox = SourceMdlHitbox()

            a_hitbox.boneIndex = reader.read_int32()
            a_hitbox.groupIndex = reader.read_int32()
            a_hitbox.boundingBoxMin.x = reader.read_single()
            a_hitbox.boundingBoxMin.y = reader.read_single()
            a_hitbox.boundingBoxMin.z = reader.read_single()
            a_hitbox.boundingBoxMax.x = reader.read_single()
            a_hitbox.boundingBoxMax.y = reader.read_single()
            a_hitbox.boundingBoxMax.z = reader.read_single()
            a_hitbox.nameOffset = reader.read_int32()
            # NOTE: Roll (z) is stored first.
            a_hitbox.boundingBoxPitchYawRoll.z = reader.read_single()
            a_hitbox.boundingBoxPitchYawRoll.x = reader.read_single()
            a_hitbox.boundingBoxPitchYawRoll.y = reader.read_single()
            a_hitbox.unknown = reader.read_single()
            for x in range(len(a_hitbox.unused_VERSION49)):
                a_hitbox.unused_VERSION49[x] = reader.read_int32()

            a_hitbox_set.theHitboxes.append(a_hitbox)

            input_pos = reader.position

            # Only v49 (and Vindictus-style v44) use a relative name offset here.
            if a_hitbox.nameOffset != 0 and (
                data.flexControllerUiOffset == TEXT_SCAL_VERSION44_VINDICTUS or data.version == 49
            ):
                reader.seek(hitbox_pos + a_hitbox.nameOffset, SEEK_BEGIN)
                a_hitbox.theName = reader.read_null_terminated_string()
            else:
                a_hitbox.theName = ""

            reader.seek(input_pos, SEEK_BEGIN)

    def read_bone_table_by_name(self) -> None:
        """Implement ``ReadBoneTableByName``."""
        data = self.theMdlFileData
        if data.boneTableByNameOffset == 0 or data.theBones is None:
            return

        reader = self.theInputFileReader
        reader.seek(data.boneTableByNameOffset, SEEK_BEGIN)

        data.theBoneTableByName = []
        for _ in range(len(data.theBones)):
            data.theBoneTableByName.append(reader.read_byte())

    # ------------------------------------------------------------------
    # Animation transition nodes
    # ------------------------------------------------------------------
    def read_local_node_names(self) -> None:
        """Implement ``ReadLocalNodeNames``."""
        data = self.theMdlFileData
        if data.localNodeCount <= 0 or data.localNodeNameOffset <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.localNodeNameOffset, SEEK_BEGIN)

        data.theLocalNodeNames = []
        try:
            for _ in range(data.localNodeCount):
                input_pos_holder = reader.position
                local_node_name_offset = reader.read_int32()

                input_pos = reader.position
                if local_node_name_offset > 0:
                    # NOTE: these offsets are absolute file offsets.
                    reader.seek(local_node_name_offset, SEEK_BEGIN)
                    a_local_node_name = reader.read_null_terminated_string()
                else:
                    a_local_node_name = ""
                data.theLocalNodeNames.append(a_local_node_name)
                reader.seek(input_pos, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            if data.theLocalNodeNames and data.theLocalNodeNames[-1] == "":
                data.theLocalNodeNames.pop()

    def read_local_nodes(self) -> None:
        """Implement ``ReadLocalNodes``."""
        data = self.theMdlFileData
        if data.localNodeCount <= 0 or data.localNodeOffset <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.localNodeOffset, SEEK_BEGIN)

        data.theLocalNodes = []
        try:
            for _ in range(data.localNodeCount):
                exit_nodes: list[int] = []
                for _ in range(data.localNodeCount):
                    exit_nodes.append(reader.read_byte())
                data.theLocalNodes.append(exit_nodes)
        except MemoryError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Body parts / models / meshes
    # ------------------------------------------------------------------
    def read_body_parts(self) -> None:
        """Implement ``ReadBodyParts``."""
        data = self.theMdlFileData
        if data.bodyPartCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.bodyPartOffset, SEEK_BEGIN)

        data.theBodyParts = []
        for body_part_index in range(data.bodyPartCount):
            body_part_pos = reader.position
            a_body_part = SourceMdlBodyPart()
            a_body_part.nameOffset = reader.read_int32()
            a_body_part.modelCount = reader.read_int32()
            a_body_part.base = reader.read_int32()
            a_body_part.modelOffset = reader.read_int32()
            data.theBodyParts.append(a_body_part)

            input_pos = reader.position

            if a_body_part.nameOffset != 0:
                reader.seek(body_part_pos + a_body_part.nameOffset, SEEK_BEGIN)
                a_body_part.theName = reader.read_null_terminated_string()
            else:
                a_body_part.theName = ""

            self.read_models(body_part_pos, a_body_part, body_part_index)
            reader.seek(input_pos, SEEK_BEGIN)

    def read_models(self, body_part_pos: int, a_body_part: SourceMdlBodyPart, body_part_index: int) -> None:
        """Implement ``ReadModels``."""
        if a_body_part.modelCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(body_part_pos + a_body_part.modelOffset, SEEK_BEGIN)

        a_body_part.theModels = []
        for _ in range(a_body_part.modelCount):
            model_pos = reader.position
            a_model = SourceMdlModel()

            a_model.name = reader.read_chars(64)
            a_model.type = reader.read_int32()
            a_model.boundingRadius = reader.read_single()
            a_model.meshCount = reader.read_int32()
            a_model.meshOffset = reader.read_int32()
            a_model.vertexCount = reader.read_int32()
            a_model.vertexOffset = reader.read_int32()
            a_model.tangentOffset = reader.read_int32()
            a_model.attachmentCount = reader.read_int32()
            a_model.attachmentOffset = reader.read_int32()
            a_model.eyeballCount = reader.read_int32()
            a_model.eyeballOffset = reader.read_int32()
            model_vertex_data = SourceMdlModelVertexData()
            model_vertex_data.vertexDataP = reader.read_int32()
            model_vertex_data.tangentDataP = reader.read_int32()
            a_model.vertexData = model_vertex_data
            for x in range(8):
                a_model.unused[x] = reader.read_int32()

            a_body_part.theModels.append(a_model)

            input_pos = reader.position

            # NOTE: eyeballs before meshes, so meshes can fill in the texture index.
            self.read_eyeballs(model_pos, a_model)
            self.read_meshes(model_pos, a_model)

            reader.seek(input_pos, SEEK_BEGIN)

    def read_meshes(self, model_pos: int, a_model: SourceMdlModel) -> None:
        """Implement ``ReadMeshes``."""
        if a_model.meshCount <= 0 or a_model.meshOffset == 0:
            return

        reader = self.theInputFileReader
        reader.seek(model_pos + a_model.meshOffset, SEEK_BEGIN)

        a_model.theMeshes = []
        for _ in range(a_model.meshCount):
            mesh_pos = reader.position
            a_mesh = SourceMdlMesh()

            a_mesh.materialIndex = reader.read_int32()
            a_mesh.modelOffset = reader.read_int32()
            a_mesh.vertexCount = reader.read_int32()
            a_mesh.vertexIndexStart = reader.read_int32()
            a_mesh.flexCount = reader.read_int32()
            a_mesh.flexOffset = reader.read_int32()
            a_mesh.materialType = reader.read_int32()
            a_mesh.materialParam = reader.read_int32()
            a_mesh.id = reader.read_int32()
            a_mesh.centerX = reader.read_single()
            a_mesh.centerY = reader.read_single()
            a_mesh.centerZ = reader.read_single()
            mesh_vertex_data = SourceMdlMeshVertexData()
            mesh_vertex_data.modelVertexDataP = reader.read_int32()
            for x in range(len(mesh_vertex_data.lodVertexCount)):
                mesh_vertex_data.lodVertexCount[x] = reader.read_int32()
            a_mesh.vertexData = mesh_vertex_data
            for x in range(8):
                a_mesh.unused[x] = reader.read_int32()

            a_model.theMeshes.append(a_mesh)

            # Fill in eyeball texture index info.
            if a_mesh.materialType == 1 and a_model.theEyeballs is not None:
                a_model.theEyeballs[a_mesh.materialParam].theTextureIndex = a_mesh.materialIndex

            input_pos = reader.position

            if a_mesh.flexCount > 0 and a_mesh.flexOffset != 0:
                self.read_flexes(mesh_pos, a_mesh)

            reader.seek(input_pos, SEEK_BEGIN)

    # ------------------------------------------------------------------
    # Textures / skins
    # ------------------------------------------------------------------
    def read_textures(self) -> None:
        """Implement ``ReadTextures``."""
        data = self.theMdlFileData
        if data.textureCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.textureOffset, SEEK_BEGIN)

        data.theTextures = []
        for _ in range(data.textureCount):
            texture_pos = reader.position
            a_texture = SourceMdlTexture()

            a_texture.nameOffset = reader.read_int32()
            a_texture.flags = reader.read_int32()
            a_texture.used = reader.read_int32()
            a_texture.unused1 = reader.read_int32()
            a_texture.materialP = reader.read_int32()
            a_texture.clientMaterialP = reader.read_int32()
            for x in range(10):
                a_texture.unused[x] = reader.read_int32()
            data.theTextures.append(a_texture)

            input_pos = reader.position

            if a_texture.nameOffset != 0:
                reader.seek(texture_pos + a_texture.nameOffset, SEEK_BEGIN)
                a_texture.thePathFileName = reader.read_null_terminated_string()
                # the parser normalises forward slashes to backslashes.
                a_texture.thePathFileName = a_texture.thePathFileName.replace("/", "\\")
            else:
                a_texture.thePathFileName = ""

            reader.seek(input_pos, SEEK_BEGIN)

    def read_texture_paths(self) -> None:
        """Implement ``ReadTexturePaths``."""
        data = self.theMdlFileData
        if data.texturePathCount <= 0:
            return

        reader = self.theInputFileReader
        try:
            reader.seek(data.texturePathOffset, SEEK_BEGIN)

            data.theTexturePaths = []
            for _ in range(data.texturePathCount):
                input_pos_holder = reader.position
                texture_path_offset = reader.read_int32()

                input_pos = reader.position
                if texture_path_offset != 0:
                    # NOTE: absolute file offset.
                    reader.seek(texture_path_offset, SEEK_BEGIN)
                    a_texture_path = reader.read_null_terminated_string()
                    a_texture_path = a_texture_path.replace("/", "\\")
                else:
                    a_texture_path = ""
                data.theTexturePaths.append(a_texture_path)
                reader.seek(input_pos, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            pass

    def read_skin_families(self) -> None:
        """Implement ``ReadSkinFamilies``."""
        data = self.theMdlFileData
        if data.skinFamilyCount <= 0 or data.skinReferenceCount <= 0:
            return

        reader = self.theInputFileReader
        reader.seek(data.skinFamilyOffset, SEEK_BEGIN)

        data.theSkinFamilies = []
        for _ in range(data.skinFamilyCount):
            a_skin_family: list[int] = []
            for _ in range(data.skinReferenceCount):
                a_skin_family.append(reader.read_int16())
            data.theSkinFamilies.append(a_skin_family)

    # ------------------------------------------------------------------
    # Ported in :mod:`loka.core.mdl_file_model`
    # ------------------------------------------------------------------
    # ``read_eyeballs`` and ``read_flexes`` are supplied by
    # ``SourceMdlFile49ModelMixin`` (called from ``read_models`` / ``read_meshes``
    # above).  They are deliberately NOT defined here, so the mixin's real
    # implementations win over any placeholder.
