"""Image-specific startup transforms, saved only on explicit confirmation.

Development previews write the bundled JSON on confirmation; published builds
only load those presets. An optional override supports standalone callers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .startup_artwork import StartupArtwork


@dataclass(frozen=True)
class Transform:
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class ArtworkLayout:
    avatar: Transform = field(default_factory=lambda: Transform(scale=1.35))
    background: Transform = field(default_factory=lambda: Transform(scale=1.5))


def _transform(value: object) -> Transform:
    """Validate complete transforms without accepting bools or numeric strings."""
    if not isinstance(value, dict):
        raise ValueError("Artwork transform must contain x, y, and scale")
    numbers = []
    for key in ("x", "y", "scale"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"Artwork {key} must be a finite number")
        try:
            number = float(number)
        except (OverflowError, ValueError):
            raise ValueError(f"Artwork {key} must be a finite number") from None
        if not math.isfinite(number):
            raise ValueError(f"Artwork {key} must be a finite number")
        numbers.append(number)
    x, y, scale = numbers
    if not -2000 <= x <= 2000 or not -2000 <= y <= 2000:
        raise ValueError("Artwork offsets must be between -2000 and 2000")
    if not 0.1 <= scale <= 5:
        raise ValueError("Artwork scale must be between 0.1 and 5")
    return Transform(x, y, scale)


def _document(path: Path, *, strict: bool = False) -> dict:
    """Never replace damaged files with defaults during a save."""
    empty = {"version": 1, "avatars": {}, "backgrounds": {}, "confirmations": {}}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return empty
    except (OSError, UnicodeError):
        if strict:
            raise
        return empty
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        if strict:
            raise ValueError(f"Cannot save over invalid artwork JSON: {path}") from None
        return empty
    if (not isinstance(result, dict) or type(result.get("version")) is not int
            or result["version"] != 1):
        if strict:
            raise ValueError(f"Unsupported artwork layout document: {path}")
        return empty
    for section in ("avatars", "backgrounds", "confirmations"):
        if section not in result:
            result[section] = {}
        elif not isinstance(result[section], dict):
            if strict:
                raise ValueError(f"Invalid artwork layout section {section}: {path}")
            result[section] = {}
    return result


class ArtworkLayoutStore:
    def __init__(self, base_path: Path, override_path: Path | None = None):
        self.base_path = Path(base_path)
        self.override_path = Path(override_path) if override_path is not None else None

    def load(self, artwork: StartupArtwork) -> ArtworkLayout:
        """Read valid resource entries; loading never writes or repairs files."""
        defaults = ArtworkLayout()
        avatar, background = defaults.avatar, defaults.background
        paths = [self.base_path]
        if self.override_path is not None:
            paths.append(self.override_path)
        for path in paths:
            document = _document(path)
            for section, key in (("avatars", artwork.avatar_path.name),
                                 ("backgrounds", artwork.background_path.name)):
                try:
                    transform = _transform(document[section].get(key))
                except ValueError:
                    continue
                if section == "avatars":
                    avatar = transform
                else:
                    background = transform
        return ArtworkLayout(avatar, background)

    def save(self, artwork: StartupArtwork, layout: ArtworkLayout) -> Path:
        """Atomically confirm all six values while retaining unrelated records.

        Invalid values/documents raise ValueError. Filesystem errors propagate to
        the caller so the GUI can report failure without claiming confirmation.
        """
        if not isinstance(layout, ArtworkLayout):
            raise ValueError("Artwork layout must contain avatar and background transforms")
        if not isinstance(layout.avatar, Transform) or not isinstance(layout.background, Transform):
            raise ValueError("Artwork layout must contain avatar and background transforms")
        avatar = asdict(_transform(asdict(layout.avatar)))
        background = asdict(_transform(asdict(layout.background)))
        destination = self.override_path if self.override_path is not None else self.base_path
        document = _document(destination, strict=True)
        avatar_key, background_key = artwork.avatar_path.name, artwork.background_path.name
        document["avatars"][avatar_key] = avatar
        document["backgrounds"][background_key] = background
        confirmation_key = "night" if artwork.is_night else str(artwork.pair_id)
        document["confirmations"][confirmation_key] = {
            "pair_id": artwork.pair_id,
            "is_night": artwork.is_night,
            "avatar_key": avatar_key,
            "background_key": background_key,
            "avatar": avatar,
            "background": background,
            "confirmed_at": datetime.now().astimezone().isoformat(),
        }
        # Serialize before touching the filesystem; nonfinite old records must
        # be repaired explicitly rather than disappearing during confirmation.
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=destination.parent,
                                             prefix=f".{destination.name}.", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination
