"""Build model export filenames with Windows path and unique-name semantics."""

from __future__ import annotations

import os
from typing import Iterable

__all__ = [
    "DIRECTORY_SEPARATOR",
    "ALT_DIRECTORY_SEPARATOR",
    "get_normalized_path_file_name",
    "get_path",
    "get_file_name",
    "get_file_name_without_extension",
    "get_extension",
    "path_combine",
    "get_clean_path_file_name",
    "get_clean_file_name_without_extension",
    "get_unique_smd_file_name",
    "move_paths_from_texture_file_names_to_texture_paths",
    "create_body_group_smd_file_name",
    "get_animation_smd_relative_path",
    "create_animation_smd_relative_path_file_name",
    "create_corrective_animation_name",
    "create_corrective_animation_smd_relative_path_file_name",
    "get_vrd_file_name",
    "get_vta_file_name",
    "create_physics_smd_file_name",
    "get_declare_sequence_qci_file_name",
]

DIRECTORY_SEPARATOR = "\\"
ALT_DIRECTORY_SEPARATOR = "/"

# Path.GetInvalidPathChars() on Windows.
_INVALID_PATH_CHARS = set('"<>|\x00') | {chr(c) for c in range(1, 32)}
# Path.GetInvalidFileNameChars() on Windows.
_INVALID_FILE_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(0, 32)}


class SmdFileNameRegistry:
    """Tracks used SMD file names so each one can be made unique.

    Mirrors ``TheApp.SmdFileNames`` plus ``SourceFileNamesModule.GetUniqueSmdFileName``.
    The first duplicate keeps its name and the second gets a ``_2`` suffix.
    """

    def __init__(self) -> None:
        self.theSmdFileNames: list[str] = []
        self.corrective_paths: dict[tuple[str,str,bool],str] = {}

    def clear(self) -> None:
        self.theSmdFileNames.clear()
        self.corrective_paths.clear()

    def get_unique_smd_file_name(self, given_smd_file_name: str) -> str:
        """Implement ``GetUniqueSmdFileName``."""
        smd_file_name = given_smd_file_name
        name_number = 1
        while smd_file_name.lower() in self.theSmdFileNames:
            name_number += 1
            smd_file_name = given_smd_file_name + "_" + str(name_number)
        self.theSmdFileNames.append(smd_file_name.lower())
        return smd_file_name


def get_normalized_path_file_name(given_path_file_name: str) -> str:
    """Implement ``FileManager.GetNormalizedPathFileName`` (``/`` -> ``\\``)."""
    return given_path_file_name.replace(ALT_DIRECTORY_SEPARATOR, DIRECTORY_SEPARATOR)


def get_path(path_file_name: str) -> str:
    """Implement ``FileManager.GetPath`` -- the directory part, with a trailing separator.

    Returns ``""`` when there is no separator (or the separator is at index 0), and
    preserves a drive root such as ``"C:\\"``.
    """
    try:
        path_file_name = get_normalized_path_file_name(path_file_name)
        length = path_file_name.rfind(DIRECTORY_SEPARATOR)
        if length < 1:
            path_file_name = ""
        elif length > 0:
            path_file_name = path_file_name[:length]
        if len(path_file_name) == 2 and path_file_name[1] == ":":
            path_file_name += DIRECTORY_SEPARATOR
        return path_file_name
    except MemoryError:
        raise
    except Exception:
        return ""


def get_file_name(path_file_name: str) -> str:
    """Implement .NET ``Path.GetFileName`` for Windows semantics."""
    normalized = path_file_name.replace(ALT_DIRECTORY_SEPARATOR, DIRECTORY_SEPARATOR)
    index = normalized.rfind(DIRECTORY_SEPARATOR)
    return normalized[index + 1 :] if index >= 0 else normalized


def get_file_name_without_extension(path_file_name: str) -> str:
    """Implement .NET ``Path.GetFileNameWithoutExtension``."""
    name = get_file_name(path_file_name)
    index = name.rfind(".")
    return name[:index] if index > 0 else name


def get_extension(path_file_name: str) -> str:
    """Implement .NET ``Path.GetExtension`` (includes the leading ``.``)."""
    name = get_file_name(path_file_name)
    index = name.rfind(".")
    return name[index:] if index >= 0 else ""


def path_combine(path: str, file_name: str) -> str:
    """Implement .NET ``Path.Combine`` for the cases the parser uses.

    NOTE: ``FileManager.GetPath`` returns the directory *without* a trailing
    separator (except for drive roots such as ``C:\\``), so concatenating a path and a
    file name directly loses the separator -- ``"teenangst" + "zoey_hair"`` becomes
    ``"teenangstzoey_hair"``.  ``Path.Combine`` inserts it.
    """
    if not path:
        return file_name
    if not file_name:
        return path
    if path.endswith(DIRECTORY_SEPARATOR) or path.endswith(ALT_DIRECTORY_SEPARATOR):
        return path + file_name
    return path + DIRECTORY_SEPARATOR + file_name


def get_clean_path_file_name(given_path_file_name: str, return_full_path_file_name: bool) -> str:
    """Implement ``FileManager.GetCleanPathFileName``."""
    cleaned = given_path_file_name
    for ch in _INVALID_PATH_CHARS:
        cleaned = cleaned.replace(ch, "")

    if return_full_path_file_name:
        try:
            cleaned = os.path.abspath(cleaned)
        except MemoryError:
            raise
        except Exception:
            cleaned = cleaned.replace(":", "")

    cleaned_file_name = get_file_name(cleaned)
    for ch in _INVALID_FILE_NAME_CHARS:
        cleaned_file_name = cleaned_file_name.replace(ch, "")

    cleaned_path = get_path(cleaned)
    if cleaned_file_name == "":
        result = cleaned
    else:
        result = path_combine(cleaned_path, cleaned_file_name)

    return result.rstrip(DIRECTORY_SEPARATOR).rstrip(ALT_DIRECTORY_SEPARATOR)


def get_clean_file_name_without_extension(given_file_name: str) -> str:
    """Implement ``FileManager.GetCleanFileNameWithoutExtension``."""
    cleaned = given_file_name
    for ch in _INVALID_FILE_NAME_CHARS:
        cleaned = cleaned.replace(ch, "")
    index = cleaned.rfind(".")
    if index > 0:
        cleaned = cleaned[:index]
    return cleaned


def move_paths_from_texture_file_names_to_texture_paths(
    texture_paths: list[str], texture_path_file_names: list[str]
) -> None:
    """Implement ``SourceFileNamesModule.MovePathsFromTextureFileNamesToTexturePaths``.

    Moves each material file name's directory into the texture-path list (newly
    discovered paths are inserted at the front so they win over existing ones) and
    leaves only the bare material name behind.  Mutates both lists in place.
    """
    texture_paths_lowercase = [path.lower() for path in texture_paths]

    for file_name_index in range(len(texture_path_file_names)):
        a_texture_path_file_name = texture_path_file_names[file_name_index]
        a_texture_path_file_name = get_clean_path_file_name(a_texture_path_file_name, False)

        a_texture_path = get_path(a_texture_path_file_name)
        a_texture_file_name = get_file_name(a_texture_path_file_name)

        a_texture_path_lowercase = a_texture_path.lower()

        if a_texture_path_lowercase != "":
            if (
                a_texture_path_lowercase not in texture_paths_lowercase
                and (a_texture_path_lowercase + DIRECTORY_SEPARATOR) not in texture_paths_lowercase
                and (a_texture_path_lowercase + ALT_DIRECTORY_SEPARATOR) not in texture_paths_lowercase
            ):
                texture_paths.insert(0, a_texture_path)
                texture_paths_lowercase.insert(0, a_texture_path_lowercase)

            texture_path_file_names[file_name_index] = a_texture_file_name


def create_body_group_smd_file_name(
    given_body_group_smd_file_name: str,
    body_part_index: int,
    model_index: int,
    lod_index: int,
    model_name: str,
    body_model_name: str,
    extra_uv_channel_index: int = -1,
    *,
    registry: SmdFileNameRegistry,
    prefix_file_names_with_model_name: bool = False,
) -> str:
    """Implement ``SourceFileNamesModule.CreateBodyGroupSmdFileName``."""
    body_group_smd_file_name = ""

    if given_body_group_smd_file_name:
        body_group_smd_file_name = given_body_group_smd_file_name
        if lod_index == 0 and extra_uv_channel_index >= 0:
            body_group_smd_file_name = get_file_name_without_extension(given_body_group_smd_file_name)
            body_group_smd_file_name += "_uv"
            body_group_smd_file_name += str(extra_uv_channel_index)
            body_group_smd_file_name += get_extension(given_body_group_smd_file_name)
    else:
        try:
            body_model_file_name = get_file_name(body_model_name.strip("\x00"))
            if _has_invalid_file_name_chars(body_model_file_name):
                body_model_file_name = f"body{body_part_index}_model{model_index}"
        except MemoryError:
            raise
        except Exception:
            body_model_file_name = f"body{body_part_index}_model{model_index}"

        body_model_file_name_without_extension = get_file_name_without_extension(body_model_file_name)

        if prefix_file_names_with_model_name and not body_model_file_name.lower().startswith(
            model_name.lower()
        ):
            body_group_smd_file_name += model_name + "_"

        body_group_smd_file_name += body_model_file_name_without_extension
        if lod_index == 0:
            if extra_uv_channel_index >= 0:
                body_group_smd_file_name += "_uv"
                body_group_smd_file_name += str(extra_uv_channel_index)
        else:
            body_group_smd_file_name += "_lod"
            body_group_smd_file_name += str(lod_index)

        body_group_smd_file_name = get_clean_file_name_without_extension(body_group_smd_file_name)
        if not body_group_smd_file_name.strip('.'):
            body_group_smd_file_name=f'body{body_part_index}_model{model_index}'
        body_group_smd_file_name = registry.get_unique_smd_file_name(body_group_smd_file_name)

        body_group_smd_file_name += ".smd"

    return body_group_smd_file_name


def _has_invalid_file_name_chars(value: str) -> bool:
    """Implement ``FileManager.FilePathHasInvalidChars``."""
    return any(ch in _INVALID_FILE_NAME_CHARS for ch in value)


def get_animation_smd_relative_path(model_name: str, place_in_subfolder: bool = True) -> str:
    """Implement ``SourceFileNamesModule.GetAnimationSmdRelativePath``."""
    if place_in_subfolder:
        return model_name + "_anims"
    return ""


def create_animation_smd_relative_path_file_name(
    given_animation_smd_relative_path_file_name: str,
    model_name: str,
    animation_name: str,
    blend_index: int = -2,
    *,
    registry: SmdFileNameRegistry,
    place_in_subfolder: bool = True,
) -> str:
    """Implement ``SourceFileNamesModule.CreateAnimationSmdRelativePathFileName``.

    ``blend_index`` selects the naming form:

    * ``>= 0``  -> ``<name>_blend<index+1 as 2 digits>``
    * ``-1``    -> the animation name unchanged
    * ``-2`` (default) -> the animation name with a *single* leading ``@`` removed

    When ``place_in_subfolder`` is false the animation is instead named
    ``<model>_anim_<original name>`` (with the ``@`` still present).
    """
    if given_animation_smd_relative_path_file_name:
        return given_animation_smd_relative_path_file_name

    original_name = animation_name.strip("\x00")
    for ch in _INVALID_FILE_NAME_CHARS:
        original_name = original_name.replace(ch, "")

    if _has_invalid_file_name_chars(original_name):
        original_name = "anim"

    if blend_index >= 0:
        animation_name = original_name + "_blend" + f"{blend_index + 1:02d}"
    elif blend_index == -1:
        animation_name = original_name
    else:
        if original_name == "":
            animation_name = ""
        elif original_name.startswith("@"):
            animation_name = original_name[1:]
        else:
            animation_name = original_name

    if not place_in_subfolder:
        animation_name = model_name + "_anim_" + original_name

    relative_path = get_animation_smd_relative_path(model_name)
    combined = path_combine(relative_path, animation_name)

    # NOTE: GetUniqueSmdFileName is applied to the *whole relative path*, including the
    # "_anims" folder prefix, exactly as the VB does.
    combined = registry.get_unique_smd_file_name(combined)

    if get_extension(combined) != ".smd":
        combined += ".smd"

    return combined


def create_corrective_animation_name(given_animation_smd_relative_path_file_name: str) -> str:
    """Implement ``SourceFileNamesModule.CreateCorrectiveAnimationName``.

    The corrective (subtract) target of an animation is named after the *animation*,
    not the model -- e.g. ``anim_LeftHandle01_KnobTurn`` becomes
    ``anim_LeftHandle01_KnobTurn_corrective_animation``.
    """
    return given_animation_smd_relative_path_file_name + "_corrective_animation"


def create_corrective_animation_smd_relative_path_file_name(
    given_animation_smd_relative_path_file_name: str,
    model_name: str,
    *,
    registry: SmdFileNameRegistry | None = None,
    place_in_subfolder: bool = True,
) -> str:
    """Implement ``CreateCorrectiveAnimationSmdRelativePathFileName``.

    Cache the physical name separately from its logical QC symbol so malformed
    Windows filename characters cannot prevent conversion's QC preflight.
    """
    key=(model_name,given_animation_smd_relative_path_file_name,place_in_subfolder)
    if registry is not None and key in registry.corrective_paths:
        return registry.corrective_paths[key]
    file_name=create_corrective_animation_name(given_animation_smd_relative_path_file_name)
    for char in _INVALID_FILE_NAME_CHARS:file_name=file_name.replace(char,'')
    relative=path_combine(get_animation_smd_relative_path(model_name,place_in_subfolder),file_name)
    if registry is not None:relative=registry.get_unique_smd_file_name(relative)
    relative+='.smd'
    if registry is not None:registry.corrective_paths[key]=relative
    return relative


def get_vrd_file_name(model_name: str) -> str:
    """Implement ``SourceFileNamesModule.GetVrdFileName``."""
    return model_name + ".vrd"


def get_vta_file_name(model_name: str, body_part_index: int) -> str:
    """Implement ``SourceFileNamesModule.GetVtaFileName``.

    The suffix is the **1-based** body part index padded to two digits, so the first
    body part yields ``<model>_01.vta``.
    """
    return f"{model_name}_{body_part_index + 1:02d}.vta"


def create_physics_smd_file_name(
    given_physics_smd_file_name: str,
    model_name: str,
    *,
    registry: SmdFileNameRegistry,
) -> str:
    """Implement ``SourceFileNamesModule.CreatePhysicsSmdFileName``."""
    if given_physics_smd_file_name:
        return given_physics_smd_file_name

    file_name = model_name + "_physics"
    file_name = registry.get_unique_smd_file_name(file_name)
    return file_name + ".smd"


def get_declare_sequence_qci_file_name(model_name: str) -> str:
    """Implement ``SourceFileNamesModule.GetDeclareSequenceQciFileName``."""
    return model_name + "_declare_sequence.qci"
