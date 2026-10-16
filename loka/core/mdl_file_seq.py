"""Read Source sequence descriptions, events, blend weights, pose keys and IK locks."""

from __future__ import annotations

import math

from .mdl_data import (
    SourceMdlActivityModifier,
    SourceMdlAnimTag,
    SourceMdlAutoLayer,
    SourceMdlEvent,
    SourceMdlIkLock,
    SourceMdlSequenceDesc,
)
from .reader import SEEK_BEGIN

__all__ = [
    "SourceMdlAutoLayerVB",
    "SourceMdlIkLockVB",
    "SourceMdlAnimTagVB",
    "SourceMdlFile49SequenceMixin",
    "EVENT_OPTIONS_LENGTH",
]

#: ``mstudioevent_t.options`` -- ``char options[64]``.
EVENT_OPTIONS_LENGTH = 64


# ---------------------------------------------------------------------------
# Data-class shims: VB field names + the fields mdl_data.py is missing
# ---------------------------------------------------------------------------

class SourceMdlAutoLayerVB(SourceMdlAutoLayer):
    """``SourceMdlAutoLayer`` with the parser's VB field names as aliases.

    ``mdl_data.SourceMdlAutoLayer`` names its fields after ``studio.h``
    (``iSequence``/``iPose``/``start``/``peak``/``tail``/``end``) whereas
    ``ReadAutoLayers`` and the QC writer use ``sequenceIndex``/``poseIndex``/
    ``influenceStart``/``influencePeak``/``influenceTail``/``influenceEnd``.
    Both spellings refer to the same storage.
    """

    @property
    def sequenceIndex(self) -> int:
        return self.iSequence

    @sequenceIndex.setter
    def sequenceIndex(self, value: int) -> None:
        self.iSequence = value

    @property
    def poseIndex(self) -> int:
        return self.iPose

    @poseIndex.setter
    def poseIndex(self, value: int) -> None:
        self.iPose = value

    @property
    def influenceStart(self) -> float:
        return self.start

    @influenceStart.setter
    def influenceStart(self, value: float) -> None:
        self.start = value

    @property
    def influencePeak(self) -> float:
        return self.peak

    @influencePeak.setter
    def influencePeak(self, value: float) -> None:
        self.peak = value

    @property
    def influenceTail(self) -> float:
        return self.tail

    @influenceTail.setter
    def influenceTail(self, value: float) -> None:
        self.tail = value

    @property
    def influenceEnd(self) -> float:
        return self.end

    @influenceEnd.setter
    def influenceEnd(self, value: float) -> None:
        self.end = value


class SourceMdlIkLockVB(SourceMdlIkLock):
    """``SourceMdlIkLock`` plus the VB names and the two fields Python lacks.

    ``SourceMdlIkLock`` is ``{chainIndex, posWeight, localQWeight, flags,
    unused[4]}``; ``mdl_data.SourceMdlIkLock`` calls the weights ``posLock`` and
    ``rotLock`` and has no ``flags``/``unused``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.flags: int = 0
        self.unused: list[int] = [0] * 4

    @property
    def posWeight(self) -> float:
        return self.posLock

    @posWeight.setter
    def posWeight(self, value: float) -> None:
        self.posLock = value

    @property
    def localQWeight(self) -> float:
        return self.rotLock

    @localQWeight.setter
    def localQWeight(self, value: float) -> None:
        self.rotLock = value


class SourceMdlAnimTagVB(SourceMdlAnimTag):
    """``SourceMdlAnimTag`` plus ``tagIndex``, which ``mdl_data.py`` omits.

    ``mstudioanimtag_t`` is ``{int tag; float cycle; int sztagindex;}`` -- note the
    fields are read in that order, i.e. ``tagIndex`` comes *before* ``cycle``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tagIndex: int = 0


class SourceMdlFile49SequenceMixin:
    """Implement ``SourceMdlFile49.ReadSequenceDescs`` and its child readers.

    Mixed into :class:`loka.core.mdl_file.SourceMdlFile49`; it relies only on
    ``self.theInputFileReader`` (a :class:`~loka.core.reader.BinaryReader`)
    and ``self.theMdlFileData`` (a :class:`~loka.core.mdl_data.SourceMdlFileData49`).
    """

    # ------------------------------------------------------------------
    # ReadSequenceDescs
    # ------------------------------------------------------------------
    def read_sequence_descs(self) -> None:
        """Implement ``ReadSequenceDescs``."""
        data = self.theMdlFileData
        if data.localSequenceCount <= 0:
            return

        reader = self.theInputFileReader
        try:
            reader.seek(data.localSequenceOffset, SEEK_BEGIN)

            data.theSequenceDescs = []
            for _ in range(data.localSequenceCount):
                seq_pos = reader.position
                a_seq_desc = SourceMdlSequenceDesc()

                a_seq_desc.baseHeaderOffset = reader.read_int32()
                a_seq_desc.nameOffset = reader.read_int32()
                a_seq_desc.activityNameOffset = reader.read_int32()
                a_seq_desc.flags = reader.read_int32()
                a_seq_desc.activity = reader.read_int32()
                a_seq_desc.activityWeight = reader.read_int32()
                a_seq_desc.eventCount = reader.read_int32()
                a_seq_desc.eventOffset = reader.read_int32()

                a_seq_desc.bbMin.x = reader.read_single()
                a_seq_desc.bbMin.y = reader.read_single()
                a_seq_desc.bbMin.z = reader.read_single()
                a_seq_desc.bbMax.x = reader.read_single()
                a_seq_desc.bbMax.y = reader.read_single()
                a_seq_desc.bbMax.z = reader.read_single()

                a_seq_desc.blendCount = reader.read_int32()
                a_seq_desc.animIndexOffset = reader.read_int32()
                a_seq_desc.movementIndex = reader.read_int32()
                a_seq_desc.groupSize[0] = reader.read_int32()
                a_seq_desc.groupSize[1] = reader.read_int32()

                a_seq_desc.paramIndex[0] = reader.read_int32()
                a_seq_desc.paramIndex[1] = reader.read_int32()
                a_seq_desc.paramStart[0] = reader.read_single()
                a_seq_desc.paramStart[1] = reader.read_single()
                a_seq_desc.paramEnd[0] = reader.read_single()
                a_seq_desc.paramEnd[1] = reader.read_single()
                a_seq_desc.paramParent = reader.read_int32()

                a_seq_desc.fadeInTime = reader.read_single()
                a_seq_desc.fadeOutTime = reader.read_single()

                a_seq_desc.localEntryNodeIndex = reader.read_int32()
                a_seq_desc.localExitNodeIndex = reader.read_int32()
                a_seq_desc.nodeFlags = reader.read_int32()

                a_seq_desc.entryPhase = reader.read_single()
                a_seq_desc.exitPhase = reader.read_single()
                a_seq_desc.lastFrame = reader.read_single()

                a_seq_desc.nextSeq = reader.read_int32()
                a_seq_desc.pose = reader.read_int32()

                a_seq_desc.ikRuleCount = reader.read_int32()
                a_seq_desc.autoLayerCount = reader.read_int32()
                a_seq_desc.autoLayerOffset = reader.read_int32()
                a_seq_desc.weightOffset = reader.read_int32()
                a_seq_desc.poseKeyOffset = reader.read_int32()

                a_seq_desc.ikLockCount = reader.read_int32()
                a_seq_desc.ikLockOffset = reader.read_int32()
                a_seq_desc.keyValueOffset = reader.read_int32()
                a_seq_desc.keyValueSize = reader.read_int32()
                a_seq_desc.cyclePoseIndex = reader.read_int32()

                # The tail of the struct differs by version, but both layouts are
                # 212 bytes: v48/v49 spend 5 ints on the activity-modifier/animtag
                # block and leave 2 "unused", older versions have 7 "unused".
                a_seq_desc.activityModifierOffset = 0
                a_seq_desc.activityModifierCount = 0
                if data.version == 48 or data.version == 49:
                    a_seq_desc.activityModifierOffset = reader.read_int32()
                    a_seq_desc.activityModifierCount = reader.read_int32()
                    a_seq_desc.animTagOffset = reader.read_int32()
                    a_seq_desc.animTagCount = reader.read_int32()
                    a_seq_desc.rootDriverBoneIndex = reader.read_int32()
                    for x in range(2):
                        a_seq_desc.unused[x] = reader.read_int32()
                else:
                    for x in range(7):
                        a_seq_desc.unused[x] = reader.read_int32()

                data.theSequenceDescs.append(a_seq_desc)

                input_file_stream_position = reader.position

                # NOTE: names are at `seqPos + offset`, i.e. relative to this struct.
                if a_seq_desc.nameOffset != 0:
                    reader.seek(seq_pos + a_seq_desc.nameOffset, SEEK_BEGIN)
                    a_seq_desc.theName = reader.read_null_terminated_string()
                else:
                    a_seq_desc.theName = ""

                if a_seq_desc.activityNameOffset != 0:
                    reader.seek(seq_pos + a_seq_desc.activityNameOffset, SEEK_BEGIN)
                    a_seq_desc.theActivityName = reader.read_null_terminated_string()
                else:
                    a_seq_desc.theActivityName = ""

                if (
                    a_seq_desc.groupSize[0] > 1 or a_seq_desc.groupSize[1] > 1
                ) and a_seq_desc.poseKeyOffset != 0:
                    self.read_pose_keys(seq_pos, a_seq_desc)
                if a_seq_desc.eventCount > 0 and a_seq_desc.eventOffset != 0:
                    self.read_events(seq_pos, a_seq_desc)
                if a_seq_desc.autoLayerCount > 0 and a_seq_desc.autoLayerOffset != 0:
                    self.read_auto_layers(seq_pos, a_seq_desc)
                if data.boneCount > 0 and a_seq_desc.weightOffset > 0:
                    self.read_mdl_anim_bone_weights(seq_pos, a_seq_desc)
                if a_seq_desc.ikLockCount > 0 and a_seq_desc.ikLockOffset != 0:
                    self.read_sequence_ik_locks(seq_pos, a_seq_desc)
                if (
                    a_seq_desc.groupSize[0] * a_seq_desc.groupSize[1]
                ) > 0 and a_seq_desc.animIndexOffset != 0:
                    self.read_mdl_anim_indexes(seq_pos, a_seq_desc)
                if a_seq_desc.keyValueSize > 0 and a_seq_desc.keyValueOffset != 0:
                    self.read_sequence_key_values(seq_pos, a_seq_desc)
                if (
                    a_seq_desc.activityModifierCount != 0
                    and a_seq_desc.activityModifierOffset != 0
                ):
                    self.read_activity_modifiers(seq_pos, a_seq_desc)
                if a_seq_desc.animTagCount != 0 and a_seq_desc.animTagOffset != 0:
                    self.read_anim_tags(seq_pos, a_seq_desc)

                reader.seek(input_file_stream_position, SEEK_BEGIN)
        except MemoryError:
            raise
        except Exception:
            # the parser's Catch only sets a debug local: keep whatever was parsed.
            pass

    # ------------------------------------------------------------------
    # Private child readers (same order as the VB source)
    # ------------------------------------------------------------------
    def read_pose_keys(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadPoseKeys``."""
        reader = self.theInputFileReader

        pose_key_count = a_seq_desc.groupSize[0] + a_seq_desc.groupSize[1]

        reader.seek(seq_pos + a_seq_desc.poseKeyOffset, SEEK_BEGIN)

        a_seq_desc.thePoseKeys = []
        for _ in range(pose_key_count):
            # A pose key is a 32-bit Single widened to Double, as in the VB.
            a_seq_desc.thePoseKeys.append(reader.read_single())

    def read_events(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadEvents``."""
        reader = self.theInputFileReader

        event_count = a_seq_desc.eventCount

        reader.seek(seq_pos + a_seq_desc.eventOffset, SEEK_BEGIN)

        a_seq_desc.theEvents = []
        for _ in range(event_count):
            event_pos = reader.position
            an_event = SourceMdlEvent()

            an_event.cycle = reader.read_single()
            an_event.eventIndex = reader.read_int32()
            an_event.eventType = reader.read_int32()
            # `char options[64]`, read one char at a time in the VB; ReadChars(64)
            # is byte-for-byte identical under .NET's ASCII encoding.  NUL padding
            # is kept: the QC writer trims it itself.
            an_event.options = reader.read_chars(EVENT_OPTIONS_LENGTH)
            an_event.nameOffset = reader.read_int32()
            a_seq_desc.theEvents.append(an_event)

            input_file_stream_position = reader.position

            if an_event.nameOffset != 0:
                reader.seek(event_pos + an_event.nameOffset, SEEK_BEGIN)
                an_event.theName = reader.read_null_terminated_string()
            else:
                # NOTE: the parser falls back to the numeric event index, not "".
                an_event.theName = str(an_event.eventIndex)

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_auto_layers(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadAutoLayers``."""
        reader = self.theInputFileReader

        auto_layer_count = a_seq_desc.autoLayerCount

        reader.seek(seq_pos + a_seq_desc.autoLayerOffset, SEEK_BEGIN)

        a_seq_desc.theAutoLayers = []
        for _ in range(auto_layer_count):
            an_auto_layer = SourceMdlAutoLayerVB()

            # ReadInt16: both indices are signed shorts, and the QC writer indexes
            # theSequenceDescs/thePoseParamDescs with the sign-extended value.
            an_auto_layer.sequenceIndex = reader.read_int16()
            an_auto_layer.poseIndex = reader.read_int16()
            an_auto_layer.flags = reader.read_int32()
            an_auto_layer.influenceStart = reader.read_single()
            an_auto_layer.influencePeak = reader.read_single()
            an_auto_layer.influenceTail = reader.read_single()
            an_auto_layer.influenceEnd = reader.read_single()
            a_seq_desc.theAutoLayers.append(an_auto_layer)

            # NOTE: NaN influences are changed to 0.  Needed for
            # HL2DM\HL2\hl2_misc_dir.vpk\models\combine_soldier_anims.mdl "Man_Gun".
            if math.isnan(an_auto_layer.influenceStart):
                an_auto_layer.influenceStart = 0.0
            if math.isnan(an_auto_layer.influencePeak):
                an_auto_layer.influencePeak = 0.0
            if math.isnan(an_auto_layer.influenceTail):
                an_auto_layer.influenceTail = 0.0
            if math.isnan(an_auto_layer.influenceEnd):
                an_auto_layer.influenceEnd = 0.0

    def read_mdl_anim_bone_weights(
        self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc
    ) -> None:
        """Implement ``ReadMdlAnimBoneWeights``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        reader.seek(seq_pos + a_seq_desc.weightOffset, SEEK_BEGIN)

        a_seq_desc.theBoneWeightsAreDefault = True
        a_seq_desc.theBoneWeights = []
        for _ in range(data.boneCount):
            an_anim_bone_weight = reader.read_single()
            a_seq_desc.theBoneWeights.append(an_anim_bone_weight)

            if an_anim_bone_weight != 1:
                a_seq_desc.theBoneWeightsAreDefault = False

    def read_sequence_ik_locks(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadSequenceIkLocks``."""
        reader = self.theInputFileReader

        lock_count = a_seq_desc.ikLockCount

        reader.seek(seq_pos + a_seq_desc.ikLockOffset, SEEK_BEGIN)

        a_seq_desc.theIkLocks = []
        for _ in range(lock_count):
            an_ik_lock = SourceMdlIkLockVB()

            an_ik_lock.chainIndex = reader.read_int32()
            an_ik_lock.posWeight = reader.read_single()
            an_ik_lock.localQWeight = reader.read_single()
            an_ik_lock.flags = reader.read_int32()
            for x in range(len(an_ik_lock.unused)):
                an_ik_lock.unused[x] = reader.read_int32()
            a_seq_desc.theIkLocks.append(an_ik_lock)

    def read_mdl_anim_indexes(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadMdlAnimIndexes``."""
        reader = self.theInputFileReader
        data = self.theMdlFileData

        anim_index_count = a_seq_desc.groupSize[0] * a_seq_desc.groupSize[1]

        reader.seek(seq_pos + a_seq_desc.animIndexOffset, SEEK_BEGIN)

        a_seq_desc.theAnimDescIndexes = []
        for _ in range(anim_index_count):
            an_anim_index = reader.read_int16()
            a_seq_desc.theAnimDescIndexes.append(an_anim_index)

            if (
                data.theAnimationDescs is not None
                and len(data.theAnimationDescs) > an_anim_index
            ):
                # NOTE: .NET's List indexer rejects a negative index and the VB test
                # above does not exclude one, so a negative index throws there too --
                # which the caller's Try/Catch swallows.  Mirror that instead of
                # silently wrapping around to the end of the list, as Python would.
                if an_anim_index < 0:
                    raise IndexError(
                        f"animation index {an_anim_index} is negative "
                        f"(VB: theAnimationDescs({an_anim_index}))"
                    )
                an_anim_desc = data.theAnimationDescs[an_anim_index]
                # NOTE: Set this boolean for use in writing lines in the QC file.
                an_anim_desc.theAnimIsLinkedToSequence = True
                # The VB constructor pre-creates this list; mdl_data.py leaves it None.
                if an_anim_desc.theLinkedSequences is None:
                    an_anim_desc.theLinkedSequences = []
                an_anim_desc.theLinkedSequences.append(a_seq_desc)

    def read_sequence_key_values(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadSequenceKeyValues``."""
        reader = self.theInputFileReader

        reader.seek(seq_pos + a_seq_desc.keyValueOffset, SEEK_BEGIN)

        a_seq_desc.theKeyValues = reader.read_null_terminated_string()

    def read_activity_modifiers(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadActivityModifiers``."""
        reader = self.theInputFileReader

        reader.seek(seq_pos + a_seq_desc.activityModifierOffset, SEEK_BEGIN)

        activity_modifier_count = a_seq_desc.activityModifierCount
        a_seq_desc.theActivityModifiers = []
        for _ in range(activity_modifier_count):
            activity_modifier_pos = reader.position
            an_activity_modifier = SourceMdlActivityModifier()

            an_activity_modifier.nameOffset = reader.read_int32()
            a_seq_desc.theActivityModifiers.append(an_activity_modifier)

            input_file_stream_position = reader.position

            if an_activity_modifier.nameOffset != 0:
                reader.seek(activity_modifier_pos + an_activity_modifier.nameOffset, SEEK_BEGIN)
                an_activity_modifier.theName = reader.read_null_terminated_string()
            else:
                an_activity_modifier.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)

    def read_anim_tags(self, seq_pos: int, a_seq_desc: SourceMdlSequenceDesc) -> None:
        """Implement ``ReadAnimTags``."""
        reader = self.theInputFileReader

        reader.seek(seq_pos + a_seq_desc.animTagOffset, SEEK_BEGIN)

        anim_tag_count = a_seq_desc.animTagCount
        a_seq_desc.theAnimTags = []
        for _ in range(anim_tag_count):
            anim_tag_pos = reader.position

            an_anim_tag = SourceMdlAnimTagVB()
            an_anim_tag.tagIndex = reader.read_int32()
            an_anim_tag.cycle = reader.read_single()
            an_anim_tag.nameOffset = reader.read_int32()
            a_seq_desc.theAnimTags.append(an_anim_tag)

            input_file_stream_position = reader.position

            if an_anim_tag.nameOffset != 0:
                reader.seek(anim_tag_pos + an_anim_tag.nameOffset, SEEK_BEGIN)
                an_anim_tag.theName = reader.read_null_terminated_string()
            else:
                an_anim_tag.theName = ""

            reader.seek(input_file_stream_position, SEEK_BEGIN)
