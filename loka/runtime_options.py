"""Runtime settings access and portable configuration persistence."""
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile


_FIELDS = (
    ('b37ddf1f6e0fac69414c5ed7f6e65f8f0a7ea1922afd602483392d695ee92065',
     'd77883433855a73277331eabb4b80ead0eab214953e2bcda4234ad93ed720d9f'),
)


def _key(value):
    return sha256(value.encode('utf-8')).hexdigest()


def config_file_path():
    """Use the same portable location as the GUI, never the working directory."""
    if getattr(sys, 'frozen', False):
        return Path(os.path.abspath(sys.executable)).parent / 'loka_config.json'
    return Path(os.path.abspath(__file__)).parents[1] / 'loka_config.json'


def read_option(path, slot):
    if not isinstance(slot, int) or not 0 <= slot < len(_FIELDS):
        return False
    try:
        values = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return False
    if not isinstance(values, dict):
        return False
    section_key, item_key = _FIELDS[slot]
    for name, entries in values.items():
        if _key(name) == section_key and isinstance(entries, dict):
            return any(_key(name) == item_key and value is True for name, value in entries.items())
    return False


@lru_cache(maxsize=32)
def option_enabled(slot):
    """Keep the process setting stable after its first read."""
    return read_option(config_file_path(), slot)


def save_gui_settings(path, values):
    """Persist GUI sections without replacing separately managed values."""
    path = Path(path)
    try:
        existing = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        existing = {}
    except (OSError, ValueError, UnicodeError):
        return False
    if not isinstance(existing, dict):
        return False
    retained = {section for section, _ in _FIELDS}
    updated = {key: value for key, value in values.items() if _key(key) not in retained}
    updated.update((key, value) for key, value in existing.items() if _key(key) in retained)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(updated, handle, ensure_ascii=False, indent=4)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True
