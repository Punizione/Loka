"""Loka model decompilation, weapon conversion and desktop interface."""

__version__ = "0.1.13"

#: The MDL versions this port decompiles, mirroring the parser's ``SourceModel.Create``.
SUPPORTED_MDL_VERSIONS = (44, 45, 46, 47, 48, 49, 54, 55, 56, 58, 59)

__all__ = ["__version__", "SUPPORTED_MDL_VERSIONS"]
