"""Read a Source model and its companion files, then write decompiled assets."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import SUPPORTED_MDL_VERSIONS
from .core.constants import MDL_VERSIONS_USING_SOURCE_MODEL_49
from .core.mdl_data import SourceMdlFileData49
from .core.mdl_file import SourceMdlFile49
from .core.naming import (
    DIRECTORY_SEPARATOR,
    SmdFileNameRegistry,
    create_animation_smd_relative_path_file_name,
    create_body_group_smd_file_name,
    create_corrective_animation_smd_relative_path_file_name,
    create_physics_smd_file_name,
    get_animation_smd_relative_path,
    get_declare_sequence_qci_file_name,
    get_vrd_file_name,
    get_vta_file_name,
    move_paths_from_texture_file_names_to_texture_paths,
)
from .core.reader import BinaryReader
from .core.bodygroup_events import apply_bodygroup_event_setup
from .core.subtract_correction import apply_corrective_subtract_setup
from .core.vtx import SourceVtxFile07, VtxValidationError
from .core.vtx_data import SourceVtxFileData07
from .core.vvd import SourceVvdFile04, SourceVvdFileData04, SourceVvdFixup04
from .core.writers.smd import DEFAULT_PRODUCT_NAME, SmdFile49

__all__ = ["DecompileOptions", "DecompileResult", "MdlDecompiler", "SmdTextWriter", "detect_mdl_version"]


class SmdTextWriter:
    """Accumulates lines and writes them as UTF-8 (no BOM) with CRLF endings.

    Equivalent to the parser's ``File.CreateText`` + ``StreamWriter.WriteLine``.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def write_line(self, text: str) -> None:
        self._lines.append(text)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            for line in self._lines:
                handle.write(line)
                handle.write("\r\n")

    @property
    def lines(self) -> list[str]:
        return self._lines


@dataclass
class DecompileOptions:
    """Mirror of the ``TheApp.Settings.Decompile*IsChecked`` flags the parser uses.

    Defaults reproduce ``AppSettings.SetDefaultDecompileReCreateFilesOptions``.
    """

    write_qc_file: bool = True
    write_reference_mesh_smd_file: bool = True
    write_lod_mesh_smd_files: bool = True
    write_physics_mesh_smd_file: bool = True
    write_vertex_animation_vta_file: bool = True
    write_bone_animation_smd_files: bool = True
    write_procedural_bones_vrd_file: bool = True
    write_declare_sequence_qci_file: bool = False
    write_texture_files: bool = False

    remove_path_from_smd_material_file_names: bool = True
    use_non_valve_uv_conversion: bool = False
    stricter_format: bool = False
    prefix_file_names_with_model_name: bool = False
    bone_animation_place_in_subfolder: bool = True

    #: How ``ikrule`` lines inside ``$animation``/``$sequence`` blocks are written:
    #: "default" keeps the parser's output, "delete" drops every ikrule line, and
    #: "usesource" rewrites touch ikrules to ``ikrule "<chain>" usesource``.
    ik_rule_handling: str = "default"

    #: Subtract correction: reuse a real looping idle animation as the delta
    #: sequences' subtract base instead of the parser's empty *_corrective_animation
    #: targets, adding the idle's frame-0 pose back into the animation SMDs.
    subtract_correction: bool = False
    #: A specific idle animation name chosen by the user ("" = automatic
    #: name matching: items containing "ads" match an "ads" idle, others match a
    #: plain idle).
    subtract_correction_idle: str = ""

    #: Per-animation axis adjustment for the subtract correction blend, keyed by
    #: animation/sequence name ("@" prefix optional): a ``(dx, dy, dz)`` rotation
    #: in degrees added to the stored delta euler before the base pose is
    #: composed back.  ``(0, 0, 0)`` (the default) means no adjustment.
    subtract_axis_offsets: dict = field(default_factory=dict)

    #: Per-animation subtract base override, keyed by animation/sequence name:
    #: the idle animation name to use as this item's subtract base instead of
    #: the global ``subtract_correction_idle`` / automatic name matching.
    subtract_bases: dict = field(default_factory=dict)

    #: BodyGroup 兼容: add ``AE_CL_BODYGROUP_SET_VALUE`` events to the sequences
    #: carrying the ACT_VM/ACT_PRIMARY VM MIXED_ON/MIXED_OFF activities, setting
    #: the ``mixed_*`` bodygroup entries (MIXED_ON -> last frame, MIXED_OFF ->
    #: frame 0).
    bodygroup_events: bool = False
    #: Optional per-activity override of the bodygroup entry to set
    #: (activity name -> bodygroup entry/smd name; empty = automatic matching).
    bodygroup_event_map: dict = field(default_factory=dict)

    #: 一键处理·处理淡入: force ``fadein 0.2`` on every sequence whose activity
    #: is ACT_VM_MELEE_LAYER (a QC may contain several such sequences).
    melee_layer_fadein_fix: bool = False

    #: Overrides the MDL version instead of reading it from the file.
    override_mdl_version: Optional[int] = None

    #: Printed as "// Created by <product_name>".
    product_name: str = DEFAULT_PRODUCT_NAME

    #: L4D2 viewmodel libraries and unchanged include dependency packaging.
    handle_vmodel_includes: bool = True
    resolve_world_physics_includes: bool = False
    game_path: str = ""

    #: Preserve compiled reference kinds and default touch-IK pose semantics.
    #: Opt-in for low-level callers; mandatory for weapon conversion.
    preserve_animation_semantics: bool = False


@dataclass
class DecompileResult:
    """What a decompile produced (paths are absolute)."""

    mdl_path: str = ""
    output_path: str = ""
    model_name: str = ""
    version: int = 0
    success: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)
    model_role: str = "other"
    included_files: list[str] = field(default_factory=list)
    vtx_recoveries: list[dict] = field(default_factory=list)

    @property
    def qc_file(self) -> Optional[str]:
        for path in self.written_files:
            if path.lower().endswith(".qc"):
                return path
        return None


def detect_mdl_version(mdl_path: str) -> int:
    """Implement ``SourceModel.GetVersion`` -- reads the id and version fields."""
    with open(mdl_path, "rb") as handle:
        header = handle.read(8)

    if len(header) < 8:
        raise ValueError(f"'{mdl_path}' is too short to be an MDL file.")

    identifier = header[:4].decode("ascii", errors="replace")
    version = int.from_bytes(header[4:8], "little", signed=True)

    if identifier == "MDLZ":
        if version != 14:
            raise ValueError(
                "File with header ID 'MDLZ' does not have the expected MDL version of 14. "
                "MDL file is not a GoldSource- or Source-engine MDL file."
            )
    elif identifier != "IDST":
        raise ValueError(
            "File does not have the expected MDL header ID of 'IDST' or 'MDLZ'. "
            "MDL file is not a GoldSource- or Source-engine MDL file."
        )

    return version


class MdlDecompiler:
    """Decompiles one ``.mdl`` (plus its companion files) into QC/SMD/VTA/VRD."""

    def __init__(self, options: Optional[DecompileOptions] = None) -> None:
        self.options = options or DecompileOptions()
        self.registry = SmdFileNameRegistry()

        self.mdl_file_data: Optional[SourceMdlFileData49] = None
        self.vtx_file_data: Optional[SourceVtxFileData07] = None
        self.vvd_file_data: Optional[SourceVvdFileData04] = None
        self.phy_file_data = None

        self.result = DecompileResult()

    # ------------------------------------------------------------------
    def decompile(
        self,
        mdl_path: str,
        output_path: Optional[str] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> DecompileResult:
        """Decompile a model; automatic v_model bundles default to its ``dec`` folder."""
        self.result = DecompileResult(mdl_path=os.path.abspath(mdl_path))
        self.registry.clear()
        self.vtx_file_data = None
        self.vvd_file_data = None

        def report(message: str) -> None:
            if progress is not None:
                progress(message)

        if not os.path.isfile(mdl_path):
            self.result.errors.append(f"未找到 MDL 文件: {mdl_path}")
            return self.result

        default_output = output_path is None
        if default_output:
            output_path = os.path.dirname(os.path.abspath(mdl_path))
        output_path = os.path.abspath(output_path)
        os.makedirs(output_path, exist_ok=True)
        self.result.output_path = output_path

        try:
            version = (
                self.options.override_mdl_version
                if self.options.override_mdl_version is not None
                else detect_mdl_version(mdl_path)
            )
        except ValueError as error:
            self.result.errors.append(str(error))
            return self.result

        self.result.version = version
        report(f"Model version {version} detected.")

        if version not in MDL_VERSIONS_USING_SOURCE_MODEL_49:
            if version in SUPPORTED_MDL_VERSIONS:
                self.result.errors.append(
                    f"MDL version {version} is not implemented by this port yet."
                )
            else:
                self.result.errors.append(
                    f"MDL version {version} is not currently supported."
                )
            return self.result

        model_name = os.path.splitext(os.path.basename(mdl_path))[0]
        self.result.model_name = model_name

        self.mdl_file_data = SourceMdlFileData49()

        report("正在读取 MDL 文件 ...")
        try:
            self._read_mdl_file(mdl_path)
        except Exception as error:  # noqa: BLE001 - mirror the parser's catch-all
            from .process_limits import exception_text
            self.result.errors.append('Failed to read MDL file: '+exception_text(error))
            return self.result

        if self.options.handle_vmodel_includes:
            from .vmodel import classify_model, prepare_animation_library, prepare_viewmodel

            self.result.model_role = classify_model(mdl_path, self.mdl_file_data)
            if self.result.model_role in ("viewmodel", "animation_library"):
                if default_output:
                    output_path = os.path.join(os.path.dirname(os.path.abspath(mdl_path)), "dec")
                    os.makedirs(output_path, exist_ok=True)
                    self.result.output_path = output_path
                elif os.path.normcase(os.path.realpath(output_path)) == os.path.normcase(os.path.realpath(os.path.dirname(mdl_path))):
                    self.result.errors.append("v_model 输出不能使用原始模型目录；请使用 dec 或其他独立输出目录。")
                    return self.result
            if self.result.model_role == "viewmodel":
                prepare_viewmodel(mdl_path, self.mdl_file_data)
            if self.result.model_role == "animation_library":
                prepare_animation_library(mdl_path, self.mdl_file_data)
                report("识别为 v_model 动画库，按实际文件名生成可重编译源码。")

        if self.options.remove_path_from_smd_material_file_names:
            self._process_texture_paths()

        report("正在读取附属文件 ...")
        self._read_phy_file(mdl_path)
        try:
            self._read_vtx_file(mdl_path)
        except VtxValidationError as error:
            self.vtx_file_data = None
            self.result.errors.append(str(error))
            return self.result
        self._read_ani_file(mdl_path)
        self._read_vvd_file(mdl_path)

        if self.options.resolve_world_physics_includes:
            try:
                from .world_physics import prepare_include_world_physics
                prepare_include_world_physics(self,mdl_path,output_path)
            except Exception as error:
                from .process_limits import exception_text
                self.result.errors.append('W 模 include 物理兼容失败: '+exception_text(error))
                return self.result

        report("正在写入反编译文件 ...")
        if self.options.write_qc_file:
            self._write_qc_file(output_path)
        if self.options.write_reference_mesh_smd_file:
            self._write_mesh_smd_files(output_path, 0, 0)
        if self.options.write_lod_mesh_smd_files and self._has_lod_mesh_data():
            lod_count = self.vtx_file_data.lodCount if self.vtx_file_data else 0
            self._write_mesh_smd_files(output_path, 1, lod_count - 1)
        if self.options.write_physics_mesh_smd_file and self._has_physics_mesh_data():
            self._write_physics_mesh_smd_file(output_path)
        if self.options.write_procedural_bones_vrd_file:
            self._write_vrd_file(output_path)
        if self.options.write_vertex_animation_vta_file:
            self._write_vta_files(output_path)
        if self.options.write_bone_animation_smd_files:
            self._write_bone_animation_smd_files(output_path)
        if self.options.write_declare_sequence_qci_file:
            self._write_declare_sequence_qci_file(output_path)

        if self.result.model_role in ("viewmodel", "animation_library"):
            from .vmodel import copy_include_dependencies

            try:
                self.result.included_files = copy_include_dependencies(
                    mdl_path, self.mdl_file_data, output_path, self.options.game_path)
                self.result.written_files.extend(self.result.included_files)
                if self.result.included_files:
                    report(f"已原样复制 {len(self.result.included_files)} 个 include 依赖文件。")
            except (OSError, ValueError) as error:
                self.result.errors.append(str(error))

        self.result.success = not self.result.errors
        report("反编译完成。" if self.result.success else "反编译完成（存在错误）。")
        return self.result

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def _read_mdl_file(self, mdl_path: str) -> None:
        reader = BinaryReader.from_file(mdl_path)
        mdl_file = SourceMdlFile49(reader, self.mdl_file_data)
        data = self.mdl_file_data

        data.theSectionFrameCount = 0
        data.theProceduralBonesCommandIsUsed = False
        data.theAnimBlockSizeNoStallOptionIsUsed = False

        mdl_file.read_mdl_header00("MDL File Header 00")
        mdl_file.read_mdl_header01("MDL File Header 01")
        if data.studioHeader2Offset > 0:
            mdl_file.read_mdl_header02("MDL File Header 02")

        # WriteBoneInfo()
        mdl_file.read_bones()
        mdl_file.read_bone_controllers()
        mdl_file.read_attachments()
        mdl_file.read_hitbox_sets()
        mdl_file.read_bone_table_by_name()

        # WriteAnimations()
        if data.localAnimationCount > 0:
            try:
                mdl_file.read_local_animation_descs()
                mdl_file.read_animation_sections()
                mdl_file.read_animation_mdl_blocks()
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - the parser swallows this too
                pass

        # WriteSequenceInfo()
        self._call(mdl_file, "read_sequence_descs")
        mdl_file.read_local_node_names()
        mdl_file.read_local_nodes()

        # WriteModel()
        # NOTE: flex descs must be read BEFORE body parts so that flexes can register
        # themselves on the flex descs, and flex rules must come AFTER body parts.
        self._call(mdl_file, "read_flex_descs")
        mdl_file.read_body_parts()
        self._call(mdl_file, "read_flex_controllers")
        self._call(mdl_file, "read_flex_rules")
        self._call(mdl_file, "read_ik_chains")
        self._call(mdl_file, "read_ik_locks")
        self._call(mdl_file, "read_mouths")
        self._call(mdl_file, "read_pose_param_descs")
        self._call(mdl_file, "read_model_groups")

        # WriteTextures()
        mdl_file.read_texture_paths()
        # NOTE: textures must be read after texture paths, so they can be compared.
        mdl_file.read_textures()
        mdl_file.read_skin_families()

        # WriteKeyValues() / WriteBoneTransforms()
        self._call(mdl_file, "read_key_values")
        self._call(mdl_file, "read_bone_transforms")
        self._call(mdl_file, "read_linear_bone_table")
        self._call(mdl_file, "read_flex_controller_uis")
        self._call(mdl_file, "read_bodygroup_presets")
        self._call(mdl_file, "post_process")
        # the parser ends ReadMdlFile_Internal with CreateFlexFrameList().  It has to run
        # during the READ phase, not lazily from the VTA writer: the QC writer resolves
        # each eyelid's `lowerer`/`raiser` frame index out of `bodyPart.theFlexFrames`,
        # and the QC file is produced before anything else.
        self._create_flex_frame_list()

    def _create_flex_frame_list(self) -> None:
        """Implement the ``CreateFlexFrameList()`` call at the end of the MDL read."""
        try:
            from .core.writers.vta import create_flex_frame_list
        except ImportError:
            return
        try:
            create_flex_frame_list(self.mdl_file_data)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - the parser's Try/Catch discipline
            pass

    @staticmethod
    def _call(mdl_file, method_name: str) -> None:
        """Invoke an optional parser section if its mixin has been ported.

        The parser is assembled from mixins so that an area which has not been ported
        yet simply contributes nothing instead of failing the whole decompile.
        """
        method = getattr(mdl_file, method_name, None)
        if method is None:
            return
        try:
            method()
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - the parser's Try/Catch discipline
            pass

    def _process_texture_paths(self) -> None:
        """Implement ``Common.ProcessTexturePaths`` (with the "remove path" option on)."""
        data = self.mdl_file_data
        modified_paths: list[str] = []
        modified_file_names: list[str] = []

        if data.theTexturePaths is not None:
            modified_paths.extend(data.theTexturePaths)
        if data.theTextures is not None:
            for texture in data.theTextures:
                modified_file_names.append(texture.thePathFileName)

        move_paths_from_texture_file_names_to_texture_paths(modified_paths, modified_file_names)

        data.theModifiedTexturePaths = modified_paths
        data.theModifiedTextureFileNames = modified_file_names

    def _read_vtx_file(self, mdl_path: str) -> None:
        """Implement ``SourceModel49.CheckForRequiredFiles`` VTX resolution + ``ReadVtxFile``.

        the parser tries ``.dx11.vtx``, ``.dx90.vtx``, ``.dx80.vtx``, ``.sw.vtx`` and
        then plain ``.vtx``, accepting the first one whose header checksum matches
        the MDL's checksum.
        """
        if self.mdl_file_data.theMdlFileOnlyHasAnimations:
            return

        vtx_path = None
        checksum = self.mdl_file_data.checksum
        base = os.path.splitext(mdl_path)[0]
        for suffix in (".dx11.vtx", ".dx90.vtx", ".dx80.vtx", ".sw.vtx", ".vtx"):
            candidate = base + suffix
            if not os.path.isfile(candidate):
                continue
            if self._vtx_file_has_same_checksum(candidate, checksum):
                vtx_path = candidate
                break

        if vtx_path is None:
            self.result.warnings.append(
                "VTX file not found or the VTX file checksum does not match the MDL file checksum."
            )
            self.vtx_file_data = None
            return

        self.vtx_file_data = SourceVtxFileData07()
        reader = BinaryReader.from_file(vtx_path)
        vtx_file = SourceVtxFile07(reader, self.vtx_file_data)
        vvd_data = self.vvd_file_data
        vvd_path = base + ".vvd"
        if vvd_data is None and os.path.isfile(vvd_path):
            # Only the fixed VVD header is needed for the VTX association gate.
            with open(vvd_path, "rb") as handle:
                header = handle.read(64)
            if len(header) < 64:
                raise VtxValidationError(f"VTX 关联 VVD 无效: {vvd_path}: header（字段偏移 0）不足 64 字节")
            vvd_data = SourceVvdFileData04()
            SourceVvdFile04(BinaryReader(header, vvd_path), vvd_data).read_source_vvd_header()
            count=vvd_data.lodVertexCount[0];size=os.path.getsize(vvd_path)
            if (count<0 or (count and (vvd_data.vertexDataOffset<64 or vvd_data.tangentDataOffset<64))
                    or vvd_data.vertexDataOffset+count*48>size or vvd_data.tangentDataOffset+count*16>size):
                raise VtxValidationError(f'VTX 关联 VVD 无效: {vvd_path}: 顶点/切线数据越过文件边界')
        if (reader.length>=36 and vvd_data is not None and
                int.from_bytes(reader.slice(28,4),'little',signed=True)!=self.mdl_file_data.bodyPartCount):
            if not vtx_file._vvd_fixup_count_allowed(vvd_data):
                raise VtxValidationError(f'VTX 关联 VVD 无效: {vvd_path}: fixupCount 为负数或超过解析预算')
            if vvd_data.fixupCount:
                start=vvd_data.fixupTableOffset;end=start+vvd_data.fixupCount*12
                size=os.path.getsize(vvd_path);count=vvd_data.lodVertexCount[0]
                stride=64 if 54<=self.mdl_file_data.version<=59 else 48
                ranges=((vvd_data.vertexDataOffset,vvd_data.vertexDataOffset+count*stride),
                        (vvd_data.tangentDataOffset,vvd_data.tangentDataOffset+count*16))
                if start<64 or end>size or any(start<b and a<end for a,b in ranges):
                    raise VtxValidationError(f'VTX 关联 VVD 无效: {vvd_path}: fixup 表越界或与顶点/切线数据重叠')
                vvd_data.theFixups=[]
                with open(vvd_path,'rb') as handle:
                    handle.seek(start)
                    for _ in range(vvd_data.fixupCount):
                        record=handle.read(12)
                        if len(record)!=12:raise VtxValidationError('VVD fixup 表读取不完整: '+vvd_path)
                        fixup=SourceVvdFixup04()
                        fixup.lodIndex=int.from_bytes(record[0:4],'little',signed=True)
                        fixup.vertexIndex=int.from_bytes(record[4:8],'little',signed=True)
                        fixup.vertexCount=int.from_bytes(record[8:12],'little',signed=True)
                        vvd_data.theFixups.append(fixup)
            if not vtx_file._vvd_mapping_valid(vvd_data):
                raise VtxValidationError(f'VTX 关联 VVD 无效: {vvd_path}: fixup/LOD 顶点映射不完整或越界')
        vtx_file.read_source_vtx_header(mdl_data=self.mdl_file_data,vvd_data=vvd_data)
        self.result.vtx_recoveries.extend(vtx_file.recovery_records)
        for record in vtx_file.recovery_records:
            self.result.warnings.append(f"VTX MDL 引导恢复: {vtx_path}: {record['field']} "
                f"{record['original']} → {record['recovered']}；完整结构与关联校验通过，原文件未修改。")
        if self.vtx_file_data.lodCount > 0:
            vtx_file.read_source_vtx_body_parts()
        vtx_file.read_source_vtx_material_replacement_lists()

    def _vtx_file_has_same_checksum(self, vtx_path: str, mdl_checksum: int) -> bool:
        """Implement ``SourceModel49.VtxFileHasSameChecksum`` (reads only the header)."""
        with open(vtx_path, "rb") as handle:
            header = handle.read(36)
        if len(header) < 36:
            raise VtxValidationError(f"VTX 结构无效: {vtx_path}: header（字段偏移 0）不足 36 字节")
        # Probe checksum without suppressing structural errors in the selected
        # companion as a missing optional file.
        return int.from_bytes(header[16:20], "little", signed=True) == mdl_checksum

    def _read_ani_file(self, mdl_path: str) -> None:
        """Implement ``SourceModel49.ReadAniFile``.

        NOTE: real ``.ani`` files start with the id ``IDAG`` (not ``IDST``) and carry
        zeroed offset fields, so every animation lookup must go through the *MDL's*
        ``theAnimBlocks`` table -- which the ANI reader does by taking the MDL data
        as a second argument.
        """
        data = self.mdl_file_data
        if data.animBlockCount <= 0:
            return
        ani_path = os.path.splitext(mdl_path)[0] + ".ani"
        if not os.path.isfile(ani_path):
            self.result.warnings.append("未找到 ANI 文件。")
            return
        try:
            from .core.ani import SourceAniFile49
        except ImportError:
            self.result.warnings.append("当前构建不支持 ANI。")
            return

        ani_file_data = SourceMdlFileData49()
        reader = BinaryReader.from_file(ani_path)
        try:
            ani_file = SourceAniFile49(reader, ani_file_data, data)
            ani_file.read_mdl_header00("ANI File Header 00")
            ani_file.read_mdl_header01("ANI File Header 01")
            ani_file.read_animation_ani_blocks()
            ani_file.read_unread_bytes()
        except MemoryError:
            raise
        except Exception as error:  # noqa: BLE001
            self.result.warnings.append(f"Failed to read ANI file: {error}")

    def _read_vvd_file(self, mdl_path: str) -> None:
        if self.mdl_file_data.theMdlFileOnlyHasAnimations:
            return
        vvd_path = os.path.splitext(mdl_path)[0] + ".vvd"
        if not os.path.isfile(vvd_path):
            self.result.warnings.append("未找到 VVD 文件。")
            return
        self.vvd_file_data = SourceVvdFileData04()
        reader = BinaryReader.from_file(vvd_path)
        vvd_file = SourceVvdFile04(reader, self.vvd_file_data)
        vvd_file.read_source_vvd_header()
        vvd_file.read_vertexes(self.mdl_file_data.version)
        vvd_file.read_tangents()
        vvd_file.read_fixups()
        vvd_file.read_extra_data()

    def _read_phy_file(self, mdl_path: str) -> None:
        if self.mdl_file_data.theMdlFileOnlyHasAnimations:
            return
        phy_path = os.path.splitext(mdl_path)[0] + ".phy"
        if not os.path.isfile(phy_path):
            return
        try:
            from .core.phy import SourcePhyFile, SourcePhyFileData
        except ImportError:
            self.result.warnings.append("当前构建不支持 PHY。")
            return

        self.phy_file_data = SourcePhyFileData()
        reader = BinaryReader.from_file(phy_path)
        phy_file = SourcePhyFile(reader, self.phy_file_data)

        try:
            phy_file.read_source_phy_header()
            if self.phy_file_data.solidCount > 0:
                phy_file.read_source_collision_data()
                phy_file.calculate_vertex_normals()
                phy_file.read_source_phys_collision_models()
                phy_file.read_source_phy_ragdoll_constraint_descs()
                phy_file.read_source_phy_collision_rules()
                phy_file.read_source_phy_animated_friction_section()
                phy_file.read_source_phy_edit_params_section()
                phy_file.read_collision_text_section()
            phy_file.read_unread_bytes()
        except MemoryError:
            raise
        except Exception as error:  # noqa: BLE001
            self.result.warnings.append(f"Failed to read PHY file: {error}")
            self.phy_file_data = None
            return

        checksum = getattr(self.phy_file_data, "checksum", None)
        if checksum is not None and checksum != self.mdl_file_data.checksum:
            self.result.warnings.append(
                "PHY file checksum does not match MDL file checksum."
            )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def _write_text_file(self, path: str, write_body: Callable[[Callable[[str], None]], None]) -> None:
        writer = SmdTextWriter()
        write_body(writer.write_line)
        writer.save(path)
        self.result.written_files.append(path)

    def _write_qc_file(self, output_path: str) -> None:
        try:
            from .core.writers.qc import QcFile49
        except ImportError:
            self.result.warnings.append("当前构建不支持 QC 写入。")
            return

        qc_path = os.path.join(output_path, self.result.model_name + ".qc")

        # the parser does this inside `SourceModel49.WriteQcFile`, just before it emits the
        # `$animation` group, so the writer can reference the corrective (subtract)
        # animation names.
        self._set_up_corrective_subtract_animation_blocks()
        self._set_up_bodygroup_events()

        def body(write_line: Callable[[str], None]) -> None:
            qc_file = QcFile49(
                write_line,
                self.mdl_file_data,
                self.vtx_file_data,
                self.phy_file_data,
                self.result.model_name,
                registry=self.registry,
                options=self.options,
            )
            if self.result.model_role == "animation_library":
                qc_file.write_animation_library()
            else:
                qc_file.write_all()

        self._write_text_file(qc_path, body)

        # Necola mode B: supplement the MIXED transition sequences after the
        # bodygroup event wiring, so the fallback paths hit the mixed state.
        if getattr(self.options, "bodygroup_events", False):
            from .core.bodygroup_events import append_mixed_transition_sequences

            try:
                with open(qc_path, encoding="utf-8", newline="") as handle:
                    text = handle.read()
            except OSError:
                return
            new_text, notes = append_mixed_transition_sequences(text)
            self.result.warnings.extend(notes)
            if new_text != text:
                with open(qc_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(new_text)

    def _write_mesh_smd_files(self, output_path: str, lod_start_index: int, lod_stop_index: int) -> None:
        """Implement ``SourceModel49.WriteMeshSmdFiles``."""
        data = self.mdl_file_data
        vtx_data = self.vtx_file_data

        if vtx_data is None or vtx_data.theVtxBodyParts is None or data.theBodyParts is None:
            return
        if lod_stop_index < lod_start_index:
            return

        body_part_vertex_index_start = 0

        for body_part_index in range(len(vtx_data.theVtxBodyParts)):
            a_vtx_body_part = vtx_data.theVtxBodyParts[body_part_index]
            if a_vtx_body_part.theVtxModels is None:
                continue

            for model_index in range(len(a_vtx_body_part.theVtxModels)):
                a_vtx_body_model = a_vtx_body_part.theVtxModels[model_index]
                if a_vtx_body_model.theVtxModelLods is None:
                    continue

                a_body_model = data.theBodyParts[body_part_index].theModels[model_index]

                if a_body_model.name[:1] == "\x00" and a_vtx_body_model.theVtxModelLods[0].theVtxMeshes is None:
                    continue

                for lod_index in range(lod_start_index, lod_stop_index + 1):
                    if lod_index >= len(a_vtx_body_model.theVtxModelLods):
                        break

                    if (
                        lod_index == 0
                        and self.vvd_file_data is not None
                        and self.vvd_file_data.theExtraDatas is not None
                        and len(self.vvd_file_data.theExtraDatas) > 0
                    ):
                        extra_uv_channel_count = len(self.vvd_file_data.theExtraDatas)
                    else:
                        extra_uv_channel_count = 0

                    for extra_uv_channel_index in range(-1, extra_uv_channel_count):
                        smd_file_name = create_body_group_smd_file_name(
                            a_body_model.theSmdFileNames[lod_index],
                            body_part_index,
                            model_index,
                            lod_index,
                            self.result.model_name,
                            a_body_model.name,
                            extra_uv_channel_index,
                            registry=self.registry,
                            prefix_file_names_with_model_name=self.options.prefix_file_names_with_model_name,
                        )
                        a_body_model.theSmdFileNames[lod_index] = smd_file_name
                        smd_path_file_name = os.path.join(output_path, smd_file_name)

                        self._write_mesh_smd_file(
                            smd_path_file_name,
                            lod_index,
                            a_vtx_body_model,
                            a_body_model,
                            body_part_vertex_index_start,
                            extra_uv_channel_index,
                        )

                body_part_vertex_index_start += a_body_model.vertexCount

    def _write_mesh_smd_file(
        self,
        smd_path_file_name: str,
        lod_index: int,
        a_vtx_model,
        a_model,
        body_part_vertex_index_start: int,
        extra_uv_channel_index: int,
    ) -> None:
        """Implement ``SourceModel49.WriteMeshSmdFile``."""

        def body(write_line: Callable[[str], None]) -> None:
            smd_file = SmdFile49(
                write_line,
                self.mdl_file_data,
                self.vvd_file_data,
                product_name=self.options.product_name,
                stricter_format=self.options.stricter_format,
            )
            smd_file.write_header_comment()
            smd_file.write_header_section()
            smd_file.write_nodes_section()
            smd_file.write_skeleton_section(lod_index)
            smd_file.write_triangles_section(
                a_vtx_model,
                lod_index,
                a_model,
                body_part_vertex_index_start,
                extra_uv_channel_index,
            )

        self._write_text_file(smd_path_file_name, body)

    def _write_vrd_file(self, output_path: str) -> None:
        """Implement ``SourceModel49.WriteVrdFile`` (procedural bones)."""
        if not self.mdl_file_data.theProceduralBonesCommandIsUsed:
            return
        try:
            from .core.writers.vrd import SourceVrdFile49
        except ImportError:
            self.result.warnings.append("当前构建不支持 VRD 写入。")
            return

        path = os.path.join(output_path, get_vrd_file_name(self.result.model_name))

        def body(write_line: Callable[[str], None]) -> None:
            vrd_file = SourceVrdFile49(
                write_line,
                self.mdl_file_data,
                stricter_format=self.options.stricter_format,
                product_name=self.options.product_name,
            )
            vrd_file.write_header_comment()
            vrd_file.write_commands()

        self._write_text_file(path, body)

    def _write_vta_files(self, output_path: str) -> None:
        """Implement ``SourceModel49.WriteVertexAnimationVtaFiles``.

        A body part gets a ``.vta`` only when it has more than one flex frame; the
        frame list itself is built by ``CreateFlexFrameList``.
        """
        data = self.mdl_file_data
        if not data.theFlexDescs or data.theBodyParts is None:
            return
        try:
            from .core.writers.vta import SourceVtaFile49, create_flex_frame_list
        except ImportError:
            self.result.warnings.append("当前构建不支持 VTA 写入。")
            return

        try:
            create_flex_frame_list(data)
        except MemoryError:
            raise
        except Exception as error:  # noqa: BLE001
            self.result.warnings.append(f"Failed to build the flex frame list: {error}")
            return

        for body_part_index, body_part in enumerate(data.theBodyParts):
            flex_frames = body_part.theFlexFrames
            if not flex_frames or len(flex_frames) <= 1:
                continue

            path = os.path.join(
                output_path, get_vta_file_name(self.result.model_name, body_part_index)
            )

            def body(write_line: Callable[[str], None], a_body_part=body_part) -> None:
                vta_file = SourceVtaFile49(
                    write_line,
                    data,
                    self.vvd_file_data,
                    a_body_part,
                    stricter_format=self.options.stricter_format,
                    product_name=self.options.product_name,
                )
                vta_file.write_header_comment()
                vta_file.write_header_section()
                vta_file.write_nodes_section()
                vta_file.write_skeleton_section_for_vertex_animation()
                vta_file.write_vertex_animation_section()

            self._write_text_file(path, body)
        data = self.mdl_file_data
        return bool(
            not data.theMdlFileOnlyHasAnimations
            and data.theBones
            and self.vtx_file_data is not None
            and self.vtx_file_data.lodCount > 0
        )

    def _write_bone_animation_smd_files(self, output_path: str) -> None:
        """Implement ``SourceModel49.WriteBoneAnimationSmdFiles``.

        Writes the corrective ("subtract") animations first, then every animation
        description.  Requires the ``CalcAnimation`` frame-line builder, so it does
        nothing until that module is present.
        """
        data = self.mdl_file_data
        if not data.theAnimationDescs:
            return
        try:
            from .core.writers.animation import write_skeleton_section_for_animation  # noqa: F401
        except ImportError:
            self.result.warnings.append(
                "Bone-animation SMDs: the animation frame builder is not available in "
                "this build, so no animation SMD files were written."
            )
            return

        for an_animation_desc in data.theCorrectiveAnimationDescs or []:
            relative = create_corrective_animation_smd_relative_path_file_name(
                an_animation_desc.theName, self.result.model_name, registry=self.registry
            )
            # NOTE: `WriteCorrectiveAnimationSmdFile` passes True here, which limits the
            # skeleton to the root bones only.
            self._write_bone_animation_smd_file(
                os.path.join(output_path, relative),
                None,
                an_animation_desc,
                only_write_corrective_animation_root_bones=True,
            )

        for an_animation_desc in data.theAnimationDescs:
            an_animation_desc.theSmdRelativePathFileName = (
                create_animation_smd_relative_path_file_name(
                    an_animation_desc.theSmdRelativePathFileName,
                    self.result.model_name,
                    an_animation_desc.theName,
                    registry=self.registry,
                )
            )
            self._write_bone_animation_smd_file(
                os.path.join(output_path, an_animation_desc.theSmdRelativePathFileName),
                None,
                an_animation_desc,
            )

    def _write_bone_animation_smd_file(
        self,
        path: str,
        a_sequence_desc,
        an_animation_desc,
        *,
        only_write_corrective_animation_root_bones: bool = False,
    ) -> None:
        """Implement ``SourceModel49.WriteBoneAnimationSmdFile`` /
        ``WriteCorrectiveAnimationSmdFile``."""
        from .core.writers.animation import write_skeleton_section_for_animation

        # Subtract correction: when this animation's delta data was reconstructed
        # against a real idle animation, add that idle's frame-0 pose back into
        # every frame (the exact inverse of studiomdl's subtract) so the QC's
        # ``subtract "<idle>" 0`` reproduces the same delta on recompile.
        base_frame_lines = None
        base_desc = getattr(an_animation_desc, "theSubtractCorrectionBaseDesc", None)
        subtract_axis_offset_radians = None
        if base_desc is not None:
            try:
                from .core.writers.animation import calc_animation

                base_frame_lines = calc_animation(self.mdl_file_data, None, base_desc, 0)
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - the parser's Try/Catch discipline
                base_frame_lines = None
            if not base_frame_lines:
                if getattr(an_animation_desc,"theRequiresSemanticBase",False):
                    raise ValueError(f'{self.mdl_file_data.theModelName}: {an_animation_desc.theName}: '
                                     'cannot reconstruct required touch-IK idle base pose')
                self.result.warnings.append(
                    "Subtract correction: could not compute the frame-0 pose of the "
                    "base idle animation; the subtract target will be written as-is."
                )
                base_frame_lines = None
            else:
                subtract_axis_offset_radians = self._get_subtract_axis_offset_radians(
                    a_sequence_desc, an_animation_desc
                )

        def body(write_line: Callable[[str], None]) -> None:
            smd_file = SmdFile49(
                write_line,
                self.mdl_file_data,
                product_name=self.options.product_name,
                stricter_format=self.options.stricter_format,
            )
            smd_file.write_header_comment()
            smd_file.write_header_section()
            smd_file.write_nodes_section()
            write_skeleton_section_for_animation(
                write_line,
                self.mdl_file_data,
                a_sequence_desc,
                an_animation_desc,
                only_write_corrective_animation_root_bones,
                stricter_format=self.options.stricter_format,
                subtract_base_frame_lines=base_frame_lines,
                subtract_axis_offset_radians=subtract_axis_offset_radians,
            )

        self._write_text_file(path, body)

    def _get_subtract_axis_offset_radians(self, a_sequence_desc, an_animation_desc):
        """Look up the user's per-name axis adjustment (degrees -> radians).

        Keys are matched against the animation desc's name and, when this SMD is
        written for a linked sequence, the sequence's name (the names the
        "Subtract 修正" tab lists, with the implied-``@`` prefix stripped).
        """
        offsets = self.options.subtract_axis_offsets
        if not offsets:
            return None
        candidates = []
        name = getattr(an_animation_desc, "theName", "") or ""
        candidates.append(name.lstrip("@"))
        if a_sequence_desc is not None:
            seq_name = getattr(a_sequence_desc, "theName", "") or ""
            candidates.append(seq_name.lstrip("@"))
        for key in candidates:
            if key and key in offsets:
                off = offsets[key]
                if any(off):
                    return tuple(v * math.pi / 180 for v in off)
        return None

    def _write_declare_sequence_qci_file(self, output_path: str) -> None:
        """Implement ``SourceModel49.WriteDeclareSequenceQciFile``."""
        try:
            from .core.writers.qc import QcFile49
        except ImportError:
            return

        path = os.path.join(
            output_path, get_declare_sequence_qci_file_name(self.result.model_name)
        )

        def body(write_line: Callable[[str], None]) -> None:
            qc_file = QcFile49(
                write_line,
                self.mdl_file_data,
                self.vtx_file_data,
                self.phy_file_data,
                self.result.model_name,
                registry=self.registry,
                options=self.options,
            )
            qc_file.write_header_comment()
            qc_file.write_qci_declare_sequence_lines()

        self._write_text_file(path, body)

    def _set_up_corrective_subtract_animation_blocks(self) -> None:
        """Implement ``SourceModel49.SetUpCorrectiveSubtractAnimationBlocks``.

        Sequences flagged ``STUDIO_DELTA`` reference implied animation descriptions
        (names starting with ``@``); those become "corrective" animations that the QC
        writer emits as ``subtract`` targets.

        With the subtract-correction option on, the shared helper in
        :mod:`core.subtract_correction` redirects the subtract target to a real
        looping idle animation instead (and leaves the corrective list empty).
        """
        data = self.mdl_file_data
        if not data.theSequenceDescs or not data.theAnimationDescs:
            return
        apply_corrective_subtract_setup(data, self.options)

    def _set_up_bodygroup_events(self) -> None:
        """Attach ``AE_CL_BODYGROUP_SET_VALUE`` events to the mixed sequences.

        See :mod:`core.bodygroup_events`; only runs when the feature is enabled
        and never aborts the decompile -- problems are reported as warnings.
        """
        if not getattr(self.options, "bodygroup_events", False):
            return
        data = self.mdl_file_data
        if not data.theSequenceDescs or not data.theAnimationDescs:
            return
        for warning in apply_bodygroup_event_setup(data, self.options):
            self.result.warnings.append(warning)

    def _has_lod_mesh_data(self) -> bool:
        """Implement ``SourceModel49.HasLodMeshData``."""
        data = self.mdl_file_data
        return bool(
            not data.theMdlFileOnlyHasAnimations
            and data.theBones
            and self.vtx_file_data is not None
            and self.vtx_file_data.lodCount > 0
        )

    def _has_physics_mesh_data(self) -> bool:
        data = self.mdl_file_data
        return bool(
            self.phy_file_data is not None
            and getattr(self.phy_file_data, "theSourcePhyCollisionDatas", None)
            and not data.theMdlFileOnlyHasAnimations
            and data.theBones
        )

    def _write_physics_mesh_smd_file(self, output_path: str) -> None:
        """Implement ``SourceModel49.WritePhysicsMeshSmdFile``."""
        try:
            from .core.phy import write_triangles_section_for_physics
        except ImportError:
            self.result.warnings.append("当前构建不支持 PHY 写入。")
            return

        smd_path_file_name = os.path.join(
            output_path,
            create_physics_smd_file_name(
                # The QC writer already created (and registered) this name while
                # emitting `$collisionmodel`; reusing it avoids a spurious `_2` suffix.
                getattr(self.phy_file_data, "thePhysicsMeshSmdFileName", "") or "",
                self.result.model_name,
                registry=self.registry,
            ),
        )

        # the parser transforms a single-solid collision hull through a world-to-pose
        # matrix built by `CalcAnimation` on the model's first animation description
        # (see `ProcessTransformsForPhysics`).  That only applies to MDL v48+; the
        # v44..v47 single-solid branch is a pure axis permutation.  If the frame-line
        # builder is unavailable we say so rather than silently emitting a mesh that
        # differs from the parser's.
        frame_line = self._first_anim_desc_frame_line()
        collision_datas = getattr(self.phy_file_data, "theSourcePhyCollisionDatas", None)
        needs_pose_transform = (
            frame_line is None
            and collision_datas is not None
            and len(collision_datas) == 1
            and not (44 <= self.mdl_file_data.version <= 47)
        )
        if needs_pose_transform:
            self.result.warnings.append(
                "Physics mesh: this single collision solid requires a bone-pose "
                "transform, but its animation frame could not be built. "
                "The exported physics SMD omits that transform."
            )

        def body(write_line: Callable[[str], None]) -> None:
            smd_file = SmdFile49(
                write_line,
                self.mdl_file_data,
                product_name=self.options.product_name,
                stricter_format=self.options.stricter_format,
            )
            smd_file.write_header_comment()
            smd_file.write_header_section()
            smd_file.write_nodes_section()
            smd_file.write_skeleton_section(-1)
            from .world_physics import physics_export_coordinates
            transformed,adjusted=physics_export_coordinates(self.mdl_file_data,frame_line)
            write_triangles_section_for_physics(write_line,transformed,self.phy_file_data,adjusted)

        self._write_text_file(smd_path_file_name, body)
        if getattr(self.mdl_file_data,'theIncludeWorldPhysics',None):
            import json
            from .world_physics import MANIFEST
            manifest=os.path.join(output_path,MANIFEST)
            with open(manifest,'w',encoding='utf-8') as handle:
                json.dump(self.mdl_file_data.theIncludeWorldPhysics,handle,ensure_ascii=False)
            self.result.written_files.append(manifest)

    def _first_anim_desc_frame_line(self):
        """Implement ``CalculateFirstAnimDescFrameLinesForPhysics``, if available."""
        try:
            from .core.writers.animation import calculate_first_anim_desc_frame_lines_for_physics
        except ImportError:
            return None
        try:
            return calculate_first_anim_desc_frame_lines_for_physics(self.mdl_file_data)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001
            return None
