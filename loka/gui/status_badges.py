"""Settings footer status items."""
from ..runtime_options import option_enabled


_ITEMS = (
    (0, '骨骼强兼'),
)


def active_badges():
    return tuple((slot, label) for slot, label in _ITEMS if option_enabled(slot))
