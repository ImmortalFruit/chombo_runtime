#!/usr/bin/env python3
"""Render Japanese-mahjong tile notation as a PNG."""

from __future__ import annotations

import argparse
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

SUITS = {"m": "manzu", "p": "pinzu", "s": "souzu"}
HONORS = {"1": "ton", "2": "nan", "3": "sha", "4": "pei", "5": "haku", "6": "hatsu", "7": "chun"}
HONOR_ALIASES = {"東": "1z", "南": "2z", "西": "3z", "北": "4z", "白": "5z", "發": "6z", "中": "7z"}
ORDER = {suit: index for index, suit in enumerate("mpsz")}
RANDOM_RE = re.compile(r"([mpsz]{0,4})(1[0-4]|[1-9])(r?)")
RED_FIVE_ALIAS_RE = re.compile(r"r5([mps])")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "outputs"
OUTPUT_SCALE = 0.4
MAX_RENDER_TILES = 30
MAX_RENDER_GROUPS = 8
RESAMPLE_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
HELP_TEXT = """手牌図入力:
  牌: 123m / 1m2m3m / 405p / r5p / 1234567z / 東南西北白發中
  伏牌: x    横牌: -牌    配置グループ: f(...)    重ね表示: 横牌k牌
  ランダム: [mpsz]1〜14[r]
  空白: 半角・全角・タブ・改行をすべて無視
  順序: 明示入力は保持。ランダム・自動作成は原則 m・p・s・z、数字順に理牌
  判定: 麻雀上の物理構成や成立性は検証しない
  描画資源上限: 30牌、配置グループ8組
  エラー: 不明文字、壊れた構文、空グループ、資源上限超過は警告"""


@dataclass(frozen=True)
class Tile:
    asset: str | None
    suit: str | None
    rank: int | None
    rotated: bool = False
    stacked: bool = False


@dataclass
class Hand:
    concealed: list[Tile]
    melds: list[list[Tile]]


def asset_name(suit: str, digit: str) -> str:
    if suit == "z":
        if digit not in HONORS:
            raise ValueError("z は 1〜7 のみです。")
        return HONORS[digit]
    if digit == "0":
        return f"aka-{SUITS[suit]}-5"
    if digit not in "123456789":
        raise ValueError("数牌は 0〜9 で指定してください。")
    return f"{SUITS[suit]}-{digit}"


def normalize_tile_aliases(expression: str) -> str:
    """Map approved input aliases to the existing mpsz parser notation."""
    normalized = RED_FIVE_ALIAS_RE.sub(r"0\1", expression)
    return "".join(HONOR_ALIASES.get(char, char) for char in normalized)


def remove_whitespace(expression: str) -> str:
    """Ignore all Unicode whitespace, including ASCII and full-width spaces."""
    return "".join(char for char in expression if not char.isspace())


def split_groups(expression: str) -> tuple[list[str], list[str], str | None]:
    compact = remove_whitespace(expression)
    if not compact:
        raise ValueError("式が空です。")
    default_suit = compact[-1] if compact[-1] in "mpsz" else None
    concealed: list[str] = []
    melds: list[str] = []
    buffer = ""
    index = 0
    while index < len(compact):
        if compact.startswith("f(", index):
            if buffer:
                concealed.append(buffer)
                buffer = ""
            end = compact.find(")", index + 2)
            if end < 0 or "(" in compact[index + 2:end]:
                raise ValueError("f(...) の括弧が不正です。")
            melds.append(compact[index + 2:end])
            index = end + 1
        else:
            if compact[index] in "()f":
                raise ValueError("f(...) の形式で副露を指定してください。")
            buffer += compact[index]
            index += 1
    if buffer:
        concealed.append(buffer)
    return concealed, melds, default_suit


def parse_flat(parts: list[str], default_suit: str | None, in_meld: bool) -> list[Tile]:
    tiles: list[Tile] = []
    for part in parts:
        pending = ""
        for char in part:
            if char in "mpsz":
                tiles.extend(expand_run(pending, char, in_meld, tiles[-1] if tiles else None))
                pending = ""
            else:
                pending += char
        if pending:
            if not default_suit and any(char.isdigit() for char in pending):
                raise ValueError("数字列の後に m・p・s・z を指定してください。")
            tiles.extend(expand_run(pending, default_suit, in_meld, tiles[-1] if tiles else None))
    return tiles


def expand_run(run: str, suit: str | None, in_meld: bool, previous_tile: Tile | None = None) -> list[Tile]:
    result: list[Tile] = []
    rotate_next = False
    stack_next = False
    for char in run:
        if char == "-":
            if rotate_next:
                raise ValueError("- の直後には牌を1枚指定してください。")
            rotate_next = True
        elif char == "k":
            previous = result[-1] if result else previous_tile
            if not in_meld or previous is None or not previous.rotated or previous.stacked or stack_next or rotate_next:
                raise ValueError("k は f(...) 内の横牌の直後に1回だけ使えます。")
            stack_next = True
        elif char == "x":
            if stack_next:
                raise ValueError("k の次には実牌を指定してください。")
            result.append(Tile(None, None, None, rotate_next, False))
            rotate_next = False
        elif char.isdigit():
            if suit is None:
                raise ValueError("数字列の後に牌種を指定してください。")
            result.append(Tile(asset_name(suit, char), suit, 5 if char == "0" else int(char), rotate_next or stack_next, stack_next))
            rotate_next = False
            stack_next = False
        else:
            raise ValueError(f"使用できない文字です: {char}")
    if rotate_next or stack_next:
        raise ValueError("演算子の直後に牌がありません。")
    return result


def parse_hand(expression: str) -> Hand:
    concealed_parts, meld_parts, default_suit = split_groups(normalize_tile_aliases(expression))
    if len(meld_parts) > MAX_RENDER_GROUPS:
        raise ValueError(f"配置グループは描画資源上、最大{MAX_RENDER_GROUPS}組です。")
    concealed = parse_flat(concealed_parts, default_suit, False)
    melds = [parse_flat([part], default_suit, True) for part in meld_parts]
    if any(not group for group in melds):
        raise ValueError("f(...) の配置グループには1枚以上指定してください。")
    tile_count = len(concealed) + sum(map(len, melds))
    if not 1 <= tile_count <= MAX_RENDER_TILES:
        raise ValueError(f"表示牌は描画資源上、1〜{MAX_RENDER_TILES}枚です。")
    return Hand(concealed, melds)


def random_hand(expression: str, rng: random.Random | None = None) -> Hand | None:
    match = RANDOM_RE.fullmatch(remove_whitespace(expression))
    if not match:
        return None
    suits_text, count_text, red = match.groups()
    if len(set(suits_text)) != len(suits_text):
        raise ValueError("ランダム指定の牌種は重複できません。")
    suits = list(suits_text) or list("mpsz")
    pool: list[tuple[str, str]] = []
    for suit in suits:
        ranks = "1234567" if suit == "z" else "123456789"
        for rank in ranks:
            pool.extend([(suit, rank)] * 4)
        if red and suit != "z":
            pool.remove((suit, "5"))
            pool.append((suit, "0"))
    picker = rng or random.SystemRandom()
    chosen = picker.sample(pool, int(count_text))
    draw = chosen.pop() if len(chosen) == 14 else None
    chosen.sort(key=lambda item: (ORDER[item[0]], 5 if item[1] == "0" else int(item[1]), item[1] == "0"))
    if draw:
        chosen.append(draw)
    return Hand([Tile(asset_name(s, d), s, 5 if d == "0" else int(d)) for s, d in chosen], [])


def tile_image(tile: Tile, asset_dir: Path) -> Image.Image:
    asset = "facedown" if tile.asset is None else tile.asset
    image = Image.open(asset_dir / f"{asset}.png").convert("RGBA")
    return image.rotate(90, expand=True) if tile.rotated else image


def render(hand: Hand, output: Path) -> None:
    asset_dir = Path(__file__).resolve().parents[1] / "assets" / "mahjong-tiles"
    # Do not keep an empty concealed group. Otherwise a hand containing only
    # f(...) is treated as though its first meld followed concealed tiles and
    # receives an unnecessary 40 px leading gap.
    groups = ([hand.concealed] if hand.concealed else []) + hand.melds
    loaded = [[(tile, tile_image(tile, asset_dir)) for tile in group] for group in groups]
    base_height = 135
    top_pad = 100 if any(tile.stacked for group in groups for tile in group) else 0
    widths: list[int] = []
    for group in loaded:
        visible = [(tile, image) for tile, image in group if not tile.stacked]
        widths.append(sum(image.width for _, image in visible) + max(0, len(visible) - 1) * 2)
    total_width = sum(widths) + 40 * max(0, len(groups) - 1)
    has_separated_draw = bool(hand.concealed) and len(hand.concealed) + 3 * len(hand.melds) == 14
    if has_separated_draw:
        total_width += 38
    canvas = Image.new("RGBA", (total_width, base_height + top_pad), "white")
    x = 0
    for group_index, group in enumerate(loaded):
        if group_index:
            x += 40
        last_rotated_x: int | None = None
        last_rotated_y: int | None = None
        for index, (tile, image) in enumerate(group):
            if tile.stacked:
                if last_rotated_x is None or last_rotated_y is None:
                    raise ValueError("重ね牌の基準となる横牌がありません。")
                canvas.alpha_composite(image, (last_rotated_x, last_rotated_y - image.height - 2))
                continue
            if group_index == 0 and has_separated_draw and index == len(hand.concealed) - 1:
                x += 38
            y = top_pad + base_height - image.height
            canvas.alpha_composite(image, (x, y))
            last_rotated_x = x if tile.rotated else None
            last_rotated_y = y if tile.rotated else None
            x += image.width + 2
        if group:
            x -= 2
    output.parent.mkdir(parents=True, exist_ok=True)
    scaled_size = tuple(max(1, round(dimension * OUTPUT_SCALE)) for dimension in canvas.size)
    canvas = canvas.resize(scaled_size, RESAMPLE_LANCZOS)
    canvas.convert("RGB").save(output)


def timestamped_output_path(now: datetime | None = None) -> Path:
    timestamp = now or datetime.now().astimezone()
    return OUTPUT_DIR / timestamp.strftime("%Y%m%dT%H%M%S.%f%z.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expression")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.expression == "help":
        print(HELP_TEXT)
        return
    try:
        hand = random_hand(args.expression) or parse_hand(args.expression)
        output = args.output or timestamped_output_path()
        render(hand, output)
    except (ValueError, OSError) as error:
        parser.error(f"警告: {error}")
    print(output.resolve())


if __name__ == "__main__":
    main()
