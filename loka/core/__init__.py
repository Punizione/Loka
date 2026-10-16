"""Binary format parsers for Source engine model files.

Modules
-------
``reader``      :class:`BinaryReader` reproducing .NET ``BinaryReader`` semantics.
``constants``   MDL/VTX flag and size constants.
``vectors``     ``SourceVector`` / ``SourceQuaternion``.
``numfmt``      .NET-compatible number formatting.
``naming``      ``FileManager`` / ``SourceFileNamesModule`` path helpers.
``mdl_data``    The MDL object model (the parser's field names).
``mdl_file``    MDL parser.
``vtx_data`` / ``vtx``   VTX parser.
``vvd``         VVD parser.
``phy``         PHY (VPhysics collision) parser.
``ani``         Bone-animation decoder and external ``.ani`` parsing.
``writers``     Text writers: SMD, QC, VTA, VRD.
"""

from __future__ import annotations
