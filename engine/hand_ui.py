"""Display assets and terms shared with the Nuxt build."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
TILE_ASSET_DIR = ROOT / "skills/render-hand/assets/mahjong-tiles"
POINT_STICK_DIR = ROOT / "assets/point-sticks"
SOUND_ASSET_DIR = ROOT / "assets/sounds"
DISPLAY_TERMS_PATH = ROOT / "locales/ja-JP/yaku.json"


class HandUIError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def embedded_assets() -> dict[str, str]:
    assets = {
        path.stem: f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        for path in sorted(TILE_ASSET_DIR.glob("*.png"))
    }
    if "facedown" not in assets:
        raise HandUIError("MISSING_TILE_ASSETS", {"path": str(TILE_ASSET_DIR)})
    for value, filename in (
        ("100", "riichi-stick-100.png"),
        ("1000", "riichi-stick-1000.png"),
    ):
        path = POINT_STICK_DIR / filename
        assets[f"stick-{value}"] = (
            f"data:image/png;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        )
    for path in sorted(SOUND_ASSET_DIR.glob("*.wav")):
        assets[f"sound-{path.stem}"] = (
            f"data:audio/wav;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        )
    return assets


def display_terms() -> dict[str, str]:
    """Load Japanese labels without putting localized prose in hand logs."""
    return json.loads(DISPLAY_TERMS_PATH.read_text(encoding="utf-8"))
