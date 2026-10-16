"""Choose one paired avatar/background for a GUI launch, without Qt or config."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import sys


PREVIEW_CHOICES = ("1", "2", "3", "4", "5", "6", "night")
BACKGROUND_PIDS = ("81404651", "83636146", "129758406", "134532634", "97870774", "122424908")


@dataclass(frozen=True)
class StartupArtwork:
    pair_id: int
    avatar_path: Path
    background_path: Path
    is_night: bool = False
    background_opacity: float = 0.5

    @property
    def background_pid(self) -> str:
        return "148882854" if self.is_night else BACKGROUND_PIDS[self.pair_id - 1]


def asset_directory() -> Path:
    """Resolve source assets or PyInstaller's extracted assets."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "loka" / "gui" / "assets"
    return Path(__file__).resolve().parent / "assets"


def select_startup_artwork(*, preview: str | None = None,
                           now: datetime | None = None, rng=None) -> StartupArtwork:
    """Draw uniformly from six pairs, then optionally override only the background.

    The local-time night interval is [20:00, 04:00); its override has a 1/100
    chance. Manual previews use no draws and never consult or persist settings.
    The night preview uses pair 1's avatar to make manual review repeatable.
    """
    if preview is not None:
        if preview not in PREVIEW_CHOICES:
            raise ValueError(f"Unknown artwork preview: {preview!r}")
        is_night = preview == "night"
        pair_id = 1 if is_night else int(preview)
    else:
        rng = random if rng is None else rng
        now = datetime.now() if now is None else now
        pair_id = rng.randrange(6) + 1
        is_night = (now.hour >= 20 or now.hour < 4) and rng.randrange(100) == 0

    avatar_id = pair_id
    root = asset_directory() / "startup"
    background_name = "background_night.png" if is_night else f"background_{pair_id:02}.png"
    # Pair 2 reuses the existing transparent cutout, whose alpha already bakes
    # in 50% opacity. Applying another 50% effect would fade it twice.
    opacity = 1.0 if pair_id == 2 and not is_night else 0.5
    return StartupArtwork(pair_id, root / f"avatar_{avatar_id:02}.png",
                          root / background_name, is_night, opacity)
