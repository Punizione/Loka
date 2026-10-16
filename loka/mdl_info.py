"""Read a light-weight animation overview from an ``.mdl`` file.

Used by the GUI's "Subtract 修正" tab: it parses just enough of the MDL
(header, animations, sequences) to list every ``$animation`` / ``$sequence``
with its flags and to pre-select the eligible idle candidates that the
subtract-correction feature can use as a subtract base.  No companion files
are needed and nothing is written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .core.bodygroup_events import BodygroupOverview, collect_bodygroup_overviews
from .core.constants import STUDIO_DELTA, STUDIO_LOOPING
from .core.mdl_data import SourceMdlFileData49
from .core.mdl_file import SourceMdlFile49
from .core.reader import BinaryReader
from .core.subtract_correction import find_idle_candidates

__all__ = ["MdlAnimationItem", "MdlOverview", "read_mdl_overview"]


@dataclass
class MdlAnimationItem:
    """One ``$animation`` or ``$sequence`` of the model."""

    name: str
    kind: str  # "animation" | "sequence"
    is_loop: bool
    is_delta: bool
    frame_count: int = 0
    #: generates a subtract option (is a delta sequence / its animation)
    is_subtract: bool = False
    #: the sequence's activity name ("" when none)
    activity: str = ""

    @property
    def kind_label(self) -> str:
        return "$animation" if self.kind == "animation" else "$sequence"


@dataclass
class MdlOverview:
    """What :func:`read_mdl_overview` produced."""

    mdl_path: str = ""
    model_name: str = ""
    version: int = 0
    items: list[MdlAnimationItem] = field(default_factory=list)
    #: looping "idle" $animations usable as a subtract base
    idle_candidates: list[MdlAnimationItem] = field(default_factory=list)
    #: names of every bodygroup (for the "≥3 distinct bodygroups" check)
    bodygroup_names: list[str] = field(default_factory=list)
    #: bodygroups containing ``mixed_`` entries, most-mixed first
    mixed_bodygroups: list[BodygroupOverview] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def read_mdl_overview(mdl_path: str) -> MdlOverview:
    """Parse the MDL's animation/sequence tables (read-only)."""
    overview = MdlOverview(mdl_path=os.path.abspath(mdl_path))
    overview.model_name = os.path.splitext(os.path.basename(mdl_path))[0]

    if not os.path.isfile(mdl_path):
        overview.errors.append(f"MDL file not found: {mdl_path}")
        return overview

    try:
        from .decompiler import detect_mdl_version

        overview.version = detect_mdl_version(mdl_path)
    except ValueError as error:
        overview.errors.append(str(error))
        return overview

    data = SourceMdlFileData49()
    try:
        reader = BinaryReader.from_file(mdl_path)
        mdl_file = SourceMdlFile49(reader, data)
        mdl_file.read_mdl_header00("MDL File Header 00")
        mdl_file.read_mdl_header01("MDL File Header 01")
        if data.localAnimationCount > 0:
            try:
                mdl_file.read_local_animation_descs()
                mdl_file.read_animation_sections()
                mdl_file.read_animation_mdl_blocks()
            except Exception:  # noqa: BLE001 - the parser swallows this too
                pass
        mdl_file.read_sequence_descs()
        mdl_file.read_body_parts()
    except Exception as error:  # noqa: BLE001
        overview.errors.append(f"Failed to read MDL file: {error}")
        return overview

    subtract_names: set[int] = set()
    delta_sequences: list = []
    animation_descs = data.theAnimationDescs or []

    def _sequence_frame_count(a_sequence_desc) -> int:
        indexes = a_sequence_desc.theAnimDescIndexes or []
        if indexes:
            try:
                return animation_descs[indexes[0]].frameCount
            except (IndexError, TypeError):
                return 0
        return 0

    for a_sequence_desc in data.theSequenceDescs or []:
        is_delta = (a_sequence_desc.flags & STUDIO_DELTA) != 0
        overview.items.append(
            MdlAnimationItem(
                name=a_sequence_desc.theName or "",
                kind="sequence",
                is_loop=(a_sequence_desc.flags & STUDIO_LOOPING) != 0,
                is_delta=is_delta,
                frame_count=_sequence_frame_count(a_sequence_desc),
                is_subtract=is_delta,
                activity=a_sequence_desc.theActivityName or "",
            )
        )
        if is_delta:
            delta_sequences.append(a_sequence_desc)
            for index in a_sequence_desc.theAnimDescIndexes or []:
                try:
                    subtract_names.add(id(animation_descs[index]))
                except (IndexError, TypeError):
                    continue

    candidate_ids={id(a) for a in find_idle_candidates(data,
        [a for a in animation_descs if id(a) in subtract_names])}
    for an_animation_desc in data.theAnimationDescs or []:
        name = an_animation_desc.theName or ""
        is_implied = name == "" or name[0] == "@"
        is_delta = (an_animation_desc.flags & STUDIO_DELTA) != 0
        overview.items.append(
            MdlAnimationItem(
                name=name if name else "(implied)",
                kind="animation",
                is_loop=(an_animation_desc.flags & STUDIO_LOOPING) != 0,
                is_delta=is_delta,
                frame_count=an_animation_desc.frameCount,
                is_subtract=id(an_animation_desc) in subtract_names,
            )
        )
        if id(an_animation_desc) in candidate_ids:
            overview.idle_candidates.append(overview.items[-1])

    overview.bodygroup_names = [
        a_body_part.theName or ""
        for a_body_part in (data.theBodyParts or [])
    ]
    overview.mixed_bodygroups = collect_bodygroup_overviews(data)

    return overview
