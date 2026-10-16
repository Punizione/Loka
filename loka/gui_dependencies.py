"""Source-launch policy for scientific image effects unused by Loka's GUI.

This module must stay outside loka.gui: importing that package already imports
Fluent Widgets. The launcher installs the policy before the GUI package loads.
"""
from importlib.abc import MetaPathFinder
import sys


_OPTIONAL_GUI_MODULES = ('qfluentwidgets.common.image_utils', 'numpy', 'scipy')
_finder = None


class _MinimalGuiImports(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in _OPTIONAL_GUI_MODULES):
            raise ModuleNotFoundError('Optional scientific GUI effect disabled: ' + fullname, name=fullname)
        return None


def install_minimal_gui_dependencies() -> None:
    """Skip optional effects without uninstalling or removing loaded modules.

    Fluent Widgets handles the missing image-effects module with its standard
    Qt fallback. Blocking NumPy/SciPy also prevents loading their BLAS backends
    if another optional component tries to import them during this GUI run.
    """
    global _finder
    if _finder is None:
        _finder = _MinimalGuiImports()
    if _finder not in sys.meta_path:
        sys.meta_path.insert(0, _finder)
