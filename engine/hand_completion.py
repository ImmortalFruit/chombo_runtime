"""Shape-only hand completion analysis for the three qualified hand forms.

This module intentionally does not evaluate yaku, score, furiten, winning
legality, visible tiles outside the input, or discard quality.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import re
from typing import Iterable


SUITS = "mpsz"
TILE_NAMES = tuple(
    [f"{rank}{suit}" for suit in "mps" for rank in range(1, 10)]
    + [f"{rank}z" for rank in range(1, 8)]
)
ORPHAN_INDICES = frozenset((0, 8, 9, 17, 18, 26, 27, 28, 29, 30, 31, 32, 33))
TOKEN_RE = re.compile(r"([0-9]+)([mpsz])")


def parse_tiles(notation: str) -> tuple[int, ...]:
    """Parse compact notation into a 34-entry count tuple.

    Red fives (0m/0p/0s) are normalized to their ordinary five tile type.
    This engine accepts concealed, unrotated tile notation only.
    """
    compact = notation.replace(" ", "")
    counts = [0] * 34
    position = 0
    for match in TOKEN_RE.finditer(compact):
        if match.start() != position:
            raise ValueError(f"unsupported notation near: {compact[position:]}")
        digits, suit = match.groups()
        for digit in digits:
            rank = 5 if digit == "0" and suit != "z" else int(digit)
            maximum = 7 if suit == "z" else 9
            if rank < 1 or rank > maximum or (suit == "z" and digit == "0"):
                raise ValueError(f"invalid tile: {digit}{suit}")
            index = (SUITS.index(suit) * 9) + rank - 1
            counts[index] += 1
            if counts[index] > 4:
                raise ValueError(f"a hand cannot contain five copies of {rank}{suit}")
        position = match.end()
    if not compact or position != len(compact):
        raise ValueError(f"unsupported notation near: {compact[position:]}")
    if sum(counts) not in (13, 14):
        raise ValueError("hand analysis requires exactly 13 or 14 tiles")
    return tuple(counts)


def _parse_tiles_for_count(notation: str, expected_count: int | None) -> tuple[int, ...]:
    """Parse concealed tiles for an analysis with already-fixed melds."""
    compact = notation.replace(" ", "")
    counts = [0] * 34
    position = 0
    for match in TOKEN_RE.finditer(compact):
        if match.start() != position:
            raise ValueError(f"unsupported notation near: {compact[position:]}")
        digits, suit = match.groups()
        for digit in digits:
            rank = 5 if digit == "0" and suit != "z" else int(digit)
            maximum = 7 if suit == "z" else 9
            if rank < 1 or rank > maximum or (suit == "z" and digit == "0"):
                raise ValueError(f"invalid tile: {digit}{suit}")
            index = (SUITS.index(suit) * 9) + rank - 1
            counts[index] += 1
            if counts[index] > 4:
                raise ValueError(f"a hand cannot contain five copies of {rank}{suit}")
        position = match.end()
    if not compact or position != len(compact):
        raise ValueError(f"unsupported notation near: {compact[position:]}")
    if expected_count is not None and sum(counts) != expected_count:
        raise ValueError(f"analysis requires exactly {expected_count} concealed tiles")
    return tuple(counts)


def _tile(index: int) -> str:
    return TILE_NAMES[index]


def _group(indices: Iterable[int]) -> str:
    values = tuple(indices)
    suit = "mpsz"[values[0] // 9]
    return "".join(str((index % 9) + 1) for index in values) + suit


@lru_cache(maxsize=None)
def standard_decompositions(counts: tuple[int, ...]) -> tuple[dict, ...]:
    """Return all 4-meld/1-head decompositions for a 14-tile hand."""
    if sum(counts) != 14:
        return ()
    results: list[dict] = []
    work = list(counts)

    def visit(melds: list[tuple[int, ...]], head: tuple[int, int] | None) -> None:
        try:
            index = next(i for i, count in enumerate(work) if count)
        except StopIteration:
            if len(melds) == 4 and head is not None:
                results.append({"melds": tuple(melds), "head": head})
            return
        if head is None and work[index] >= 2:
            work[index] -= 2
            visit(melds, (index, index))
            work[index] += 2
        if len(melds) < 4 and work[index] >= 3:
            work[index] -= 3
            visit(melds + [(index, index, index)], head)
            work[index] += 3
        if len(melds) < 4 and index < 27 and index % 9 <= 6:
            if work[index + 1] and work[index + 2]:
                work[index] -= 1
                work[index + 1] -= 1
                work[index + 2] -= 1
                visit(melds + [(index, index + 1, index + 2)], head)
                work[index] += 1
                work[index + 1] += 1
                work[index + 2] += 1

    visit([], None)
    unique = {(item["melds"], item["head"]): item for item in results}
    return tuple(unique.values())


def _is_partial_standard_complete(counts: tuple[int, ...], required_melds: int) -> bool:
    """Return whether concealed tiles form the remaining melds and one head."""
    work = list(counts)

    def visit(melds: int, head_found: bool) -> bool:
        try:
            index = next(i for i, count in enumerate(work) if count)
        except StopIteration:
            return melds == required_melds and head_found
        if not head_found and work[index] >= 2:
            work[index] -= 2
            if visit(melds, True):
                return True
            work[index] += 2
        if melds < required_melds and work[index] >= 3:
            work[index] -= 3
            if visit(melds + 1, head_found):
                return True
            work[index] += 3
        if melds < required_melds and index < 27 and index % 9 <= 6:
            if work[index + 1] and work[index + 2]:
                work[index] -= 1
                work[index + 1] -= 1
                work[index + 2] -= 1
                if visit(melds + 1, head_found):
                    return True
                work[index] += 1
                work[index + 1] += 1
                work[index + 2] += 1
        return False

    return visit(0, False)


def partial_standard_decompositions(
    counts: tuple[int, ...], required_melds: int
) -> tuple[dict, ...]:
    """Return decompositions for the concealed remainder of a called hand."""
    if required_melds < 0 or required_melds > 4:
        raise ValueError("required_melds must be between 0 and 4")
    work = list(counts)
    results: list[dict] = []

    def visit(melds: list[tuple[int, ...]], head: tuple[int, int] | None) -> None:
        try:
            index = next(index for index, count in enumerate(work) if count)
        except StopIteration:
            if len(melds) == required_melds and head is not None:
                results.append({"melds": tuple(melds), "head": head})
            return
        if head is None and work[index] >= 2:
            work[index] -= 2
            visit(melds, (index, index))
            work[index] += 2
        if len(melds) < required_melds and work[index] >= 3:
            work[index] -= 3
            visit(melds + [(index, index, index)], head)
            work[index] += 3
        if len(melds) < required_melds and index < 27 and index % 9 <= 6:
            if work[index + 1] and work[index + 2]:
                work[index] -= 1
                work[index + 1] -= 1
                work[index + 2] -= 1
                visit(melds + [(index, index + 1, index + 2)], head)
                work[index] += 1
                work[index + 1] += 1
                work[index + 2] += 1

    visit([], None)
    unique = {(item["melds"], item["head"]): item for item in results}
    return tuple(unique[key] for key in sorted(unique))


def _can_partition_fixed_melds(counts: tuple[int, ...], meld_count: int) -> bool:
    """Accept any partition into exactly the declared sequences/triplets/quads."""
    if not 3 * meld_count <= sum(counts) <= 4 * meld_count:
        return False
    if meld_count == 0:
        return not any(counts)
    index = next(i for i, count in enumerate(counts) if count)
    groups = []
    if counts[index] >= 3:
        groups.append((index,) * 3)
    if counts[index] == 4:
        groups.append((index,) * 4)
    if index < 27 and index % 9 <= 6 and counts[index + 1] and counts[index + 2]:
        groups.append((index, index + 1, index + 2))
    for group in groups:
        remaining = list(counts)
        for tile in group:
            remaining[tile] -= 1
        if _can_partition_fixed_melds(tuple(remaining), meld_count - 1):
            return True
    return False


def standard_wait_tiles_with_fixed_melds(
    notation: str, fixed_meld_count: int, fixed_tiles: str = ""
) -> tuple[str, ...]:
    """Return standard-form waits with declared melds or quads held fixed.

    A fixed quad counts as one meld, just like a fixed sequence or triplet. The
    function evaluates shape only and does not decide whether an action that
    produced the fixed meld is legal.
    """
    if fixed_meld_count < 0 or fixed_meld_count > 4:
        raise ValueError("fixed_meld_count must be between 0 and 4")
    expected_count = 13 - (fixed_meld_count * 3)
    counts = _parse_tiles_for_count(notation, expected_count)
    fixed_counts = (
        _parse_tiles_for_count(fixed_tiles, None) if fixed_tiles else (0,) * len(counts)
    )
    for index, count in enumerate(counts):
        if count + fixed_counts[index] > 4:
            raise ValueError(f"a hand cannot contain five copies of {_tile(index)}")
    if not _can_partition_fixed_melds(fixed_counts, fixed_meld_count):
        raise ValueError("fixed_tiles must form exactly fixed_meld_count sequences, triplets or quads")
    required_melds = 4 - fixed_meld_count
    waits = []
    for index, count in enumerate(counts):
        if count + fixed_counts[index] >= 4:
            continue
        candidate = list(counts)
        candidate[index] += 1
        if _is_partial_standard_complete(tuple(candidate), required_melds):
            waits.append(_tile(index))
    return tuple(waits)


def _is_seven_pairs(counts: tuple[int, ...]) -> bool:
    return sum(counts) == 14 and sum(count == 2 for count in counts) == 7


def _is_thirteen_orphans(counts: tuple[int, ...]) -> bool:
    return (
        sum(counts) == 14
        and all(counts[index] >= 1 for index in ORPHAN_INDICES)
        and sum(counts[index] >= 2 for index in ORPHAN_INDICES) == 1
        and all(counts[index] == 0 for index in range(34) if index not in ORPHAN_INDICES)
    )


def complete_forms(counts: tuple[int, ...]) -> tuple[str, ...]:
    forms = []
    if standard_decompositions(counts):
        forms.append("hand.complete.standard")
    if _is_seven_pairs(counts):
        forms.append("hand.complete.seven_pairs")
    if _is_thirteen_orphans(counts):
        forms.append("hand.complete.thirteen_orphans")
    return tuple(forms)


@lru_cache(maxsize=None)
def _standard_shanten(counts: tuple[int, ...], fixed_meld_count: int = 0) -> int:
    best = 8
    work = list(counts)

    def visit(index: int, melds: int, pairs: int, taatsu: int) -> None:
        nonlocal best
        while index < 34 and work[index] == 0:
            index += 1
        if index == 34:
            # One pair may be the head; each additional pair is also a valid
            # incomplete meld candidate (a shanpon structure).
            usable_taatsu = taatsu + (pairs - 1 if pairs else 0)
            remaining_melds = 4 - melds
            if usable_taatsu > remaining_melds:
                usable_taatsu = remaining_melds
            candidate = 8 - (melds * 2) - usable_taatsu - (1 if pairs else 0)
            if candidate < best:
                best = candidate
            return
        if work[index] >= 3:
            work[index] -= 3
            visit(index, melds + 1, pairs, taatsu)
            work[index] += 3
        if index < 27 and index % 9 <= 6 and work[index + 1] and work[index + 2]:
            work[index] -= 1
            work[index + 1] -= 1
            work[index + 2] -= 1
            visit(index, melds + 1, pairs, taatsu)
            work[index] += 1
            work[index + 1] += 1
            work[index + 2] += 1
        if work[index] >= 2:
            work[index] -= 2
            visit(index, melds, pairs + 1, taatsu)
            work[index] += 2
        if index < 27 and index % 9 <= 7 and work[index + 1]:
            work[index] -= 1
            work[index + 1] -= 1
            visit(index, melds, pairs, taatsu + 1)
            work[index] += 1
            work[index + 1] += 1
        if index < 27 and index % 9 <= 6 and work[index + 2]:
            work[index] -= 1
            work[index + 2] -= 1
            visit(index, melds, pairs, taatsu + 1)
            work[index] += 1
            work[index + 2] += 1
        work[index] -= 1
        visit(index, melds, pairs, taatsu)
        work[index] += 1

    visit(0, fixed_meld_count, 0, 0)
    return best


def standard_shanten_with_fixed_melds(notation: str, fixed_meld_count: int) -> int:
    """Return standard-form shanten with already-declared melds held fixed."""
    if fixed_meld_count < 1 or fixed_meld_count > 4:
        raise ValueError("fixed_meld_count must be between 1 and 4")
    expected_count = 13 - (fixed_meld_count * 3)
    counts = _parse_tiles_for_count(notation, expected_count)
    return _standard_shanten(counts, fixed_meld_count)


def standard_ukeire_with_fixed_melds(
    notation: str, fixed_meld_count: int, fixed_tiles: str = ""
) -> dict[str, object]:
    """Return standard-form shanten and in-hand-based ukeire for a called hand."""
    if fixed_meld_count < 1 or fixed_meld_count > 4:
        raise ValueError("fixed_meld_count must be between 1 and 4")
    counts = _parse_tiles_for_count(notation, 13 - (fixed_meld_count * 3))
    fixed_counts = _parse_tiles_for_count(fixed_tiles, None) if fixed_tiles else (0,) * 34
    current = _standard_shanten(counts, fixed_meld_count)
    ukeire = []
    for index, count in enumerate(counts):
        remaining = 4 - count - fixed_counts[index]
        if remaining <= 0:
            continue
        acquired = list(counts)
        acquired[index] += 1
        advanced = _standard_shanten(tuple(acquired), fixed_meld_count)
        if advanced < current:
            ukeire.append({"tile": _tile(index), "remaining_copies": remaining, "advanced_shanten": advanced})
    return {
        "overall_shanten": current,
        "ukeire": ukeire,
        "total_in_hand_based_ukeire_copy_count": sum(item["remaining_copies"] for item in ukeire),
    }


@lru_cache(maxsize=4096)
def _has_physical_standard_wait(counts: tuple[int, ...]) -> bool:
    """A 13-tile hand needs at least one completion without a fifth copy."""
    for index, count in enumerate(counts):
        if count == 4:
            continue
        candidate = list(counts)
        candidate[index] += 1
        if _is_partial_standard_complete(tuple(candidate), 4):
            return True
    return False


def _physical_standard_shanten(counts: tuple[int, ...]) -> int:
    structural = _standard_shanten(counts)
    if structural != 0 or 4 not in counts:
        return structural
    if sum(counts) == 13:
        return 0 if _has_physical_standard_wait(counts) else 1
    if sum(counts) == 14:
        for index, count in enumerate(counts):
            if count:
                discarded = list(counts)
                discarded[index] -= 1
                if _has_physical_standard_wait(tuple(discarded)):
                    return 0
        return 1
    return structural


def shanten_by_form(counts: tuple[int, ...]) -> dict[str, int]:
    unique = sum(count > 0 for count in counts)
    pairs = sum(count >= 2 for count in counts)
    orphan_unique = sum(counts[index] > 0 for index in ORPHAN_INDICES)
    orphan_pair = int(any(counts[index] >= 2 for index in ORPHAN_INDICES))
    return {
        "hand.complete.standard": _physical_standard_shanten(counts),
        "hand.complete.seven_pairs": 6 - pairs + max(0, 7 - unique),
        "hand.complete.thirteen_orphans": 13 - orphan_unique - orphan_pair,
    }


def _formatted_decomposition(decomposition: dict) -> dict:
    return {
        "melds": [_group(group) for group in decomposition["melds"]],
        "head": _group(decomposition["head"]),
    }


def _standard_wait_type(decomposition: dict, acquired: int) -> set[str]:
    result: set[str] = set()
    if acquired in decomposition["head"]:
        result.add("hand.wait.tanki")
    for meld in decomposition["melds"]:
        if acquired not in meld:
            continue
        if meld[0] == meld[2]:
            result.add("hand.wait.shanpon")
            continue
        remaining = list(meld)
        remaining.remove(acquired)
        ranks = [(index % 9) + 1 for index in remaining]
        acquired_rank = (acquired % 9) + 1
        if ranks in ([1, 2], [8, 9]):
            if (ranks == [1, 2] and acquired_rank == 3) or (ranks == [8, 9] and acquired_rank == 7):
                result.add("hand.wait.penchan")
            else:
                result.add("hand.wait.ryanmen")
        elif ranks[1] - ranks[0] == 2:
            result.add("hand.wait.kanchan")
        else:
            result.add("hand.wait.ryanmen")
    return result


def _waits(counts: tuple[int, ...]) -> list[dict]:
    waits = []
    for index, count in enumerate(counts):
        if count == 4:
            continue
        candidate = list(counts)
        candidate[index] += 1
        candidate_tuple = tuple(candidate)
        forms = complete_forms(candidate_tuple)
        if not forms:
            continue
        decompositions = standard_decompositions(candidate_tuple)
        wait_types = set()
        for decomposition in decompositions:
            wait_types.update(_standard_wait_type(decomposition, index))
        waits.append(
            {
                "tile": _tile(index),
                "remaining_copies": 4 - count,
                "complete_forms": list(forms),
                "wait_types": sorted(wait_types),
                "decompositions": [_formatted_decomposition(item) for item in decompositions],
            }
        )
    return waits


def _shanten_after_acquisition(counts: tuple[int, ...], acquired: int) -> tuple[int, set[str]]:
    drawn = list(counts)
    drawn[acquired] += 1
    values = shanten_by_form(tuple(drawn))
    best = min(values.values())
    return best, {form for form, value in values.items() if value == best}


def analyze_hand(notation: str) -> dict:
    counts = parse_tiles(notation)
    values = shanten_by_form(counts)
    overall = min(values.values())
    result = {
        "input": notation,
        "tile_count": sum(counts),
        "scope": "shape_only",
        "shanten_by_form": values,
        "overall_shanten": overall,
        "complete_forms": list(complete_forms(counts)),
        "excluded_judgments": ["yaku", "score", "furiten", "winning_legality", "discard_choice"],
    }
    if sum(counts) == 13 and overall == 0:
        waits = _waits(counts)
        result.update(
            {
                "tenpai": bool(waits),
                "waits": waits,
                "wait_tile_type_count": len(waits),
                "total_in_hand_based_wait_copy_count": sum(item["remaining_copies"] for item in waits),
            }
        )
    elif sum(counts) == 13 and overall > 0:
        ukeire = []
        for index, count in enumerate(counts):
            if count == 4:
                continue
            advanced, forms = _shanten_after_acquisition(counts, index)
            if advanced < overall:
                ukeire.append(
                    {
                        "tile": _tile(index),
                        "remaining_copies": 4 - count,
                        "advanced_shanten": advanced,
                        "advanced_forms": sorted(forms),
                    }
                )
        result.update(
            {
                "tenpai": False,
                "ukeire": ukeire,
                "ukeire_tile_type_count": len(ukeire),
                "total_in_hand_based_ukeire_copy_count": sum(item["remaining_copies"] for item in ukeire),
            }
        )
    return result
