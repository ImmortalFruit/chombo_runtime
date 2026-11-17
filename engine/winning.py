"""Deterministic win qualification using only qualified Chombo concepts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from engine.hand_completion import TILE_NAMES, analyze_hand, parse_tiles, partial_standard_decompositions, standard_decompositions


ORPHANS = {"1m", "9m", "1p", "9p", "1s", "9s", "1z", "2z", "3z", "4z", "5z", "6z", "7z"}
DRAGONS = {"5z", "6z", "7z"}

YAKU_VALUE = {
    "game.hand.yaku.menzen_tsumo": (1, 1),
    "game.hand.yaku.tanyao": (1, 1),
    "game.hand.yaku.yakuhai": (1, 1),
    "game.hand.yaku.pinfu": (1, 1),
    "game.hand.yaku.chiitoitsu": (2, 2),
    "game.hand.yaku.toitoi": (2, 2),
    "game.hand.yaku.iipeikou": (1, 0),
    "game.hand.yaku.sanshoku_doujun": (2, 1),
    "game.hand.yaku.ittsuu": (2, 1),
    "game.hand.yaku.ryanpeikou": (3, 0),
    "game.hand.yaku.chanta": (2, 1),
    "game.hand.yaku.junchan": (3, 2),
    "game.hand.yaku.honroutou": (2, 2),
    "game.hand.yaku.honitsu": (3, 2),
    "game.hand.yaku.chinitsu": (6, 5),
    "game.hand.yaku.sanankou": (2, 2),
    "game.hand.yaku.sanshoku_doukou": (2, 2),
    "game.hand.yaku.sankantsu": (2, 2),
    "game.hand.yaku.shousangen": (2, 2),
    "game.hand.yaku.haitei": (1, 1),
    "game.hand.yaku.houtei": (1, 1),
    "game.hand.yaku.rinshan_kaihou": (1, 1),
    "game.hand.yaku.chankan": (1, 1),
    "game.hand.yaku.riichi": (1, 0),
    "game.hand.yaku.ippatsu": (1, 0),
    "game.hand.yaku.double_riichi": (2, 0),
}

REQUIRED_CONTEXT_FIELDS = (
    "player",
    "concealed",
    "prevailing_wind",
    "seat_wind",
    "winning_event",
    "riichi_established",
    "double_riichi_established",
    "ippatsu_right_active",
    "quad_count",
    "concealed_triplet_or_quad_count",
    "fixed_melds",
    "own_discards",
    "same_turn_missed_win",
    "riichi_missed_win",
)


def _normalize_tile(tile: str) -> str:
    tile = tile.strip()
    if len(tile) != 2 or tile[1] not in "mpsz" or not tile[0].isdigit():
        raise ValueError(f"invalid tile: {tile}")
    rank = "5" if tile[0] == "0" and tile[1] != "z" else tile[0]
    normalized = f"{rank}{tile[1]}"
    if normalized not in TILE_NAMES:
        raise ValueError(f"invalid tile: {tile}")
    return normalized


def _tiles(counts: tuple[int, ...]) -> list[str]:
    return [TILE_NAMES[index] for index, count in enumerate(counts) for _ in range(count)]


def _group_tiles(group: tuple[int, ...]) -> tuple[str, ...]:
    return tuple(TILE_NAMES[index] for index in group)


def _fixed_meld_signatures(context: dict[str, Any]) -> Counter:
    signatures: Counter = Counter()
    for meld in context.get("fixed_melds", []):
        raw_tiles = meld.get("tiles") if isinstance(meld, dict) else None
        if not isinstance(raw_tiles, list) or len(raw_tiles) not in (3, 4):
            raise ValueError("each fixed meld requires a tiles list of length 3 or 4")
        indices = sorted(TILE_NAMES.index(_normalize_tile(tile)) for tile in raw_tiles)
        if len(indices) == 4:
            if len(set(indices)) != 1:
                raise ValueError("a four-tile fixed meld must be a quad")
            indices = indices[:3]
        signature = tuple(indices)
        if not (len(set(signature)) == 1 or _is_sequence(signature)):
            raise ValueError("fixed meld must be a sequence, triplet, or quad")
        signatures[signature] += 1
    return signatures


def _contains_fixed_melds(decomposition: dict, required: Counter) -> bool:
    available = Counter(tuple(group) for group in decomposition["melds"])
    return all(available[signature] >= count for signature, count in required.items())


def _fixed_standard_decompositions(
    completed_counts: tuple[int, ...], context: dict[str, Any]
) -> tuple[dict, ...]:
    """Decompose a hand without permitting declared meld tiles to move groups."""
    fixed = _fixed_meld_signatures(context)
    if not fixed:
        return standard_decompositions(completed_counts)
    concealed = list(completed_counts)
    fixed_groups: list[tuple[int, ...]] = []
    for signature, occurrence_count in sorted(fixed.items()):
        for _ in range(occurrence_count):
            for index in signature:
                concealed[index] -= 1
                if concealed[index] < 0:
                    raise ValueError("fixed meld tiles are absent from the hand input")
            fixed_groups.append(signature)
    partial = partial_standard_decompositions(tuple(concealed), 4 - len(fixed_groups))
    return tuple(
        {"melds": tuple(fixed_groups) + item["melds"], "head": item["head"]}
        for item in partial
    )


def _fixed_physical_counts(counts: tuple[int, ...], context: dict[str, Any]) -> tuple[int, ...]:
    """Restore quad fourth tiles without counting included melds twice."""
    represented: Counter = Counter()
    for signature, occurrences in _fixed_meld_signatures(context).items():
        for index in signature:
            represented[index] += occurrences
    if any(counts[index] < count for index, count in represented.items()):
        raise ValueError("fixed meld tiles are absent from the hand input")
    physical = list(counts)
    for meld in context.get("fixed_melds", []):
        if len(meld["tiles"]) == 4:
            physical[TILE_NAMES.index(_normalize_tile(meld["tiles"][0]))] += 1
    if any(count > 4 for count in physical):
        raise ValueError("a hand and its fixed melds cannot contain five copies of one tile type")
    return tuple(physical)


def _fixed_wait_tiles(hand_before: str, context: dict[str, Any]) -> list[str]:
    counts = parse_tiles(hand_before)
    physical = _fixed_physical_counts(counts, context)
    waits: list[str] = []
    for index, count in enumerate(counts):
        if physical[index] >= 4:
            continue
        completed = list(counts)
        completed[index] += 1
        if _fixed_standard_decompositions(tuple(completed), context):
            waits.append(TILE_NAMES[index])
    return waits


def _is_sequence(group: tuple[int, ...]) -> bool:
    return len(group) == 3 and len(set(group)) == 3


def _contains_terminal_or_honor(group: tuple[int, ...]) -> bool:
    return any(TILE_NAMES[index] in ORPHANS for index in group)


def _wait_is_ryanmen(decomposition: dict, winning_index: int) -> bool:
    for group in decomposition["melds"]:
        if winning_index not in group or not _is_sequence(group):
            continue
        ranks = [index % 9 + 1 for index in group]
        winning_rank = winning_index % 9 + 1
        if winning_rank == ranks[1]:
            continue
        if ranks == [1, 2, 3] and winning_rank == 3:
            continue
        if ranks == [7, 8, 9] and winning_rank == 7:
            continue
        return True
    return False


def _winning_assignments(decomposition: dict[str, Any], winning_index: int, *, fixed_meld_count: int = 0) -> list[tuple[str, int]]:
    """Enumerate acquired-tile groups without reassigning declared melds."""
    result: list[tuple[str, int]] = []
    if winning_index in decomposition["head"]:
        result.append(("head", 0))
    result.extend(("meld", index) for index, meld in enumerate(decomposition["melds"])
                  if index >= fixed_meld_count and winning_index in meld)
    return result


def _structural_yaku(
    counts: tuple[int, ...], decomposition: dict, winning_index: int, context: dict[str, Any],
    *, winning_assignment: tuple[str, int] | None = None, method: str | None = None,
) -> list[dict]:
    concealed = bool(context.get("concealed", True))
    melds = list(decomposition["melds"])
    head = decomposition["head"]
    sequences = [group for group in melds if _is_sequence(group)]
    triplets = [group for group in melds if not _is_sequence(group)]
    found: list[tuple[str, int]] = []

    def add(concept_id: str, multiplier: int = 1) -> None:
        closed, opened = YAKU_VALUE[concept_id]
        value = closed if concealed else opened
        if value:
            found.append((concept_id, value * multiplier))

    sequence_counts = Counter(_group_tiles(group) for group in sequences)
    identical_pairs = sum(count // 2 for count in sequence_counts.values())
    if concealed and identical_pairs >= 2:
        add("game.hand.yaku.ryanpeikou")
    elif concealed and identical_pairs >= 1:
        add("game.hand.yaku.iipeikou")

    sequence_keys = {(group[0] // 9, group[0] % 9 + 1) for group in sequences}
    if any(all((suit, start) in sequence_keys for suit in range(3)) for start in range(1, 8)):
        add("game.hand.yaku.sanshoku_doujun")
    if any(all((suit, start) in sequence_keys for start in (1, 4, 7)) for suit in range(3)):
        add("game.hand.yaku.ittsuu")

    all_groups = melds + [head]
    all_terminal_or_honor = all(_contains_terminal_or_honor(group) for group in all_groups)
    has_honor = any(index >= 27 for group in all_groups for index in group)
    has_sequence = bool(sequences)
    if all_terminal_or_honor and has_sequence:
        if has_honor:
            add("game.hand.yaku.chanta")
        else:
            add("game.hand.yaku.junchan")

    if not sequences:
        add("game.hand.yaku.toitoi")

    triplet_keys = {(group[0] // 9, group[0] % 9 + 1) for group in triplets if group[0] < 27}
    if any(all((suit, rank) in triplet_keys for suit in range(3)) for rank in range(1, 10)):
        add("game.hand.yaku.sanshoku_doukou")

    fixed_melds = _fixed_meld_signatures(context)
    fixed_triplet_count = sum(
        count
        for signature, count in fixed_melds.items()
        if not _is_sequence(signature)
    )
    concealed_triplets = int(context.get("concealed_triplet_or_quad_count", 0))
    if winning_assignment is not None:
        concealed_fixed = _fixed_meld_signatures({"fixed_melds": [
            meld for meld in context.get("fixed_melds", []) if not meld.get("open", True)
        ]})
        concealed_triplets = len(triplets) - fixed_triplet_count + sum(
            count for signature, count in concealed_fixed.items() if not _is_sequence(signature)
        )
        kind, position = winning_assignment
        if method == "ron" and kind == "meld" and not _is_sequence(melds[position]):
            concealed_triplets -= 1
    if concealed_triplets >= 3:
        add("game.hand.yaku.sanankou")
    if int(context.get("quad_count", 0)) >= 3:
        add("game.hand.yaku.sankantsu")

    dragon_triplets = sum(TILE_NAMES[group[0]] in DRAGONS for group in triplets)
    if dragon_triplets == 2 and TILE_NAMES[head[0]] in DRAGONS:
        add("game.hand.yaku.shousangen")

    prevailing = context.get("prevailing_wind")
    seat = context.get("seat_wind")
    yakuhai_roles = 0
    for group in triplets:
        tile = TILE_NAMES[group[0]]
        if tile in DRAGONS:
            yakuhai_roles += 1
        if tile == prevailing:
            yakuhai_roles += 1
        if tile == seat:
            yakuhai_roles += 1
    if yakuhai_roles:
        add("game.hand.yaku.yakuhai", yakuhai_roles)

    pair_tile = TILE_NAMES[head[0]]
    pair_is_value = pair_tile in DRAGONS or pair_tile in {prevailing, seat}
    if concealed and len(sequences) == 4 and not pair_is_value and _wait_is_ryanmen(decomposition, winning_index):
        add("game.hand.yaku.pinfu")

    return [{"concept_id": concept_id, "han": han} for concept_id, han in found]


def _common_yaku(counts: tuple[int, ...], complete_forms: list[str], method: str, context: dict[str, Any]) -> list[dict]:
    concealed = bool(context.get("concealed", True))
    tiles = _tiles(counts)
    found: list[tuple[str, int]] = []

    def add(concept_id: str) -> None:
        closed, opened = YAKU_VALUE[concept_id]
        value = closed if concealed else opened
        if value:
            found.append((concept_id, value))

    if concealed and method == "tsumo":
        add("game.hand.yaku.menzen_tsumo")
    if all(tile not in ORPHANS for tile in tiles):
        add("game.hand.yaku.tanyao")
    if "hand.complete.seven_pairs" in complete_forms and concealed:
        add("game.hand.yaku.chiitoitsu")
    if all(tile in ORPHANS for tile in tiles) and any(tile[1] == "z" for tile in tiles) and any(tile[1] != "z" for tile in tiles):
        add("game.hand.yaku.honroutou")

    suits = {tile[1] for tile in tiles if tile[1] != "z"}
    honors = any(tile[1] == "z" for tile in tiles)
    if len(suits) == 1:
        add("game.hand.yaku.honitsu" if honors else "game.hand.yaku.chinitsu")

    event_map = {
        "last_draw_of_hand": "game.hand.yaku.haitei",
        "last_discard_of_hand": "game.hand.yaku.houtei",
        "replacement_tile_after_kan": "game.hand.yaku.rinshan_kaihou",
        "tile_used_by_other_player_for_added_kan": "game.hand.yaku.chankan",
    }
    event = context.get("winning_event")
    expected_method = {
        "last_draw_of_hand": "tsumo",
        "last_discard_of_hand": "ron",
        "replacement_tile_after_kan": "tsumo",
        "tile_used_by_other_player_for_added_kan": "ron",
    }
    if event in event_map and method == expected_method[event]:
        add(event_map[event])

    if concealed and context.get("riichi_established"):
        add("game.hand.yaku.riichi")
    if concealed and context.get("double_riichi_established"):
        found = [item for item in found if item[0] != "game.hand.yaku.riichi"]
        add("game.hand.yaku.double_riichi")
    if concealed and context.get("riichi_established") and context.get("ippatsu_right_active"):
        add("game.hand.yaku.ippatsu")

    return [{"concept_id": concept_id, "han": han} for concept_id, han in found]


def evaluate_yaku(
    completed_counts: tuple[int, ...], complete_forms: list[str], winning_tile: str, method: str, context: dict[str, Any]
) -> list[dict]:
    winning_index = TILE_NAMES.index(winning_tile)
    common = _common_yaku(completed_counts, ["hand.complete.standard"], method, context)
    candidates: list[list[dict]] = []
    fixed = _fixed_meld_signatures(context)
    for decomposition in _fixed_standard_decompositions(completed_counts, context):
        for assignment in _winning_assignments(decomposition, winning_index, fixed_meld_count=sum(fixed.values())):
            candidates.append(common + _structural_yaku(completed_counts, decomposition, winning_index, context,
                winning_assignment=assignment, method=method))
    if "hand.complete.seven_pairs" in complete_forms:
        candidates.append(_common_yaku(completed_counts, ["hand.complete.seven_pairs"], method, context))
    if not candidates:
        candidates.append(common)
    best = max(candidates, key=lambda items: (sum(item["han"] for item in items), len(items)))
    unique: dict[str, dict] = {}
    for item in best:
        existing = unique.get(item["concept_id"])
        if existing is None or item["han"] > existing["han"]:
            unique[item["concept_id"]] = item
    return [unique[key] for key in sorted(unique)]


def analyze_win(
    hand_before: str,
    winning_tile: str,
    method: str,
    *,
    context: dict[str, Any] | None = None,
) -> dict:
    """Evaluate one claimant's win through shape, yaku, furiten and declaration."""
    if method not in {"tsumo", "ron"}:
        raise ValueError("method must be tsumo or ron")
    context = dict(context or {})
    winning_tile = _normalize_tile(winning_tile)
    before = analyze_hand(hand_before)
    if before["tile_count"] != 13:
        raise ValueError("win analysis requires a 13-tile-equivalent hand before acquisition")
    fixed = _fixed_meld_signatures(context) if context.get("fixed_melds") else Counter()
    wait_tiles = _fixed_wait_tiles(hand_before, context) if fixed else [item["tile"] for item in before.get("waits", [])]
    completed_counts = parse_tiles(f"{hand_before}{winning_tile}")
    if fixed:
        complete_forms = ["hand.complete.standard"] if winning_tile in wait_tiles else []
    else:
        complete_forms = list(before_wait["complete_forms"] if (before_wait := next((item for item in before.get("waits", []) if item["tile"] == winning_tile), None)) else [])
    is_complete = bool(complete_forms)

    yaku = evaluate_yaku(completed_counts, complete_forms, winning_tile, method, context) if is_complete else []
    own_discards = {_normalize_tile(tile) for tile in context.get("own_discards", [])}
    own_discard_matches = sorted(set(wait_tiles) & own_discards)
    furiten_categories = []
    if own_discard_matches:
        furiten_categories.append("game.hand.win.furiten.own_discard")
    if context.get("same_turn_missed_win"):
        furiten_categories.append("game.hand.win.furiten.same_turn")
    if context.get("riichi_missed_win"):
        furiten_categories.append("game.hand.win.furiten.riichi_missed_win")
    furiten = bool(furiten_categories)

    missing = [field for field in REQUIRED_CONTEXT_FIELDS if field not in context]
    if missing:
        status = "INSUFFICIENT_INPUT"
        can_win = None
        failed_stage = None
    elif not is_complete:
        status, can_win, failed_stage = "NOT_QUALIFIED", False, "complete_form"
    elif not yaku:
        status, can_win, failed_stage = "NOT_QUALIFIED", False, "yaku_requirement"
    elif method == "ron" and furiten:
        status, can_win, failed_stage = "NOT_QUALIFIED", False, "furiten"
    else:
        status, can_win, failed_stage = "QUALIFIED_TO_WIN", True, None

    declared = bool(context.get("declared", False))
    declaration_result = {
        "declared": declared,
        "valid": bool(can_win and declared),
        "required_call": "ツモ" if method == "tsumo" else "ロン",
    }
    return {
        "judgment_status": status,
        "input": {"hand_before": hand_before, "winning_tile": winning_tile, "method": method},
        "waits": wait_tiles,
        "is_complete_form": is_complete,
        "complete_forms": complete_forms,
        "winning_method": f"game.hand.win.{method}",
        "qualified_yaku": yaku,
        "total_han": sum(item["han"] for item in yaku),
        "furiten_status": {
            "active": furiten,
            "categories": furiten_categories,
            "own_discard_wait_matches": own_discard_matches,
            "blocks_method": method == "ron" and furiten,
        },
        "restriction_checks": [{"concept_id": "game.hand.win.furiten", "passed": not (method == "ron" and furiten)}],
        "can_win": can_win,
        "failed_stage": failed_stage,
        "missing_inputs": missing,
        "declaration_result": declaration_result,
        "win_result": ({
            "winner": context["player"],
            "winning_method": method,
            "winning_tile": winning_tile,
            "complete_forms": complete_forms,
            "qualified_yaku": [item["concept_id"] for item in yaku],
            "furiten_status": furiten_categories,
        } if declaration_result["valid"] else None),
        "excluded_judgments": ["yakuman", "fu", "score", "payment", "dealer_continuation", "next_hand_transition"],
    }


def resolve_multiple_ron(claimants: list[dict], rule: Mapping[str, Any]) -> dict:
    """Resolve ron claimants from semantic values in a resolved rule dimension."""
    maximum_winners = rule.get("maximum_winners")
    claimant_order = rule.get("claimant_order")
    if not isinstance(maximum_winners, int) or isinstance(maximum_winners, bool) or maximum_winners < 1:
        raise ValueError("multiple-ron rule requires a positive maximum_winners")
    if (
        not isinstance(claimant_order, Sequence)
        or isinstance(claimant_order, (str, bytes))
        or not claimant_order
        or len(set(claimant_order)) != len(claimant_order)
    ):
        raise ValueError("multiple-ron rule requires a unique claimant_order")
    if not isinstance(claimants, list):
        raise ValueError("claimants must be a list of objects")
    players = set()
    positions = set()
    for claimant in claimants:
        if not isinstance(claimant, Mapping):
            raise ValueError("each claimant must be an object")
        player = claimant.get("player")
        position = claimant.get("relative_to_discarder")
        if not isinstance(player, str) or not player.strip():
            raise ValueError("each claimant requires a non-empty player string")
        if not isinstance(position, str) or position not in claimant_order:
            raise ValueError("claimant position is absent from claimant_order")
        if not isinstance(claimant.get("can_win"), bool):
            raise ValueError("each claimant requires a boolean can_win")
        if player in players or position in positions:
            raise ValueError("claimant players and positions must be unique")
        players.add(player)
        positions.add(position)
    eligible = [claimant for claimant in claimants if claimant["can_win"]]
    order = {relative: index for index, relative in enumerate(claimant_order)}
    try:
        eligible.sort(key=lambda claimant: order[claimant["relative_to_discarder"]])
    except KeyError as error:
        raise ValueError("claimant position is absent from claimant_order") from error
    winners = eligible[:maximum_winners]
    return {
        "resolution_rule": {
            "maximum_winners": maximum_winners,
            "claimant_order": list(claimant_order),
        },
        "claimants": claimants,
        "winners": [winner["player"] for winner in winners],
        "multiple_winners_approved": maximum_winners > 1 and len(winners) > 1,
    }
