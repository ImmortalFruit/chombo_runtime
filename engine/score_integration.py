"""Integrated qualified win, yaku, fu, dora, and payment calculation."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from engine.hand_completion import TILE_NAMES, analyze_hand, parse_tiles
from engine.scoring import (
    calculate_score,
    validate_default_profile_snapshot,
)
from engine.rule_profiles import ResolvedRuleProfile
from engine.winning import (
    DRAGONS,
    ORPHANS,
    _common_yaku,
    _fixed_standard_decompositions,
    _fixed_meld_signatures,
    _structural_yaku,
    _winning_assignments,
    analyze_win,
)


WINDS = {"1z", "2z", "3z", "4z"}
GREEN_TILES = {"2s", "3s", "4s", "6s", "8s", "6z"}


def _normalize_tile(tile: str) -> str:
    if len(tile) != 2 or tile[1] not in "mpsz" or tile[0] not in "0123456789":
        raise ValueError(f"invalid tile: {tile}")
    normalized = f"{'5' if tile[0] == '0' else tile[0]}{tile[1]}"
    if normalized not in TILE_NAMES:
        raise ValueError(f"invalid tile: {tile}")
    return normalized


def _notation_tiles(notation: str) -> list[str]:
    digits: list[str] = []
    result: list[str] = []
    for character in notation.replace(" ", ""):
        if character.isdigit():
            digits.append(character)
        elif character in "mpsz" and digits:
            result.extend(f"{digit}{character}" for digit in digits)
            digits = []
        else:
            raise ValueError(f"invalid compact tile notation: {notation}")
    if digits:
        raise ValueError(f"invalid compact tile notation: {notation}")
    return result


def _next_dora(indicator: str) -> str:
    indicator = _normalize_tile(indicator)
    rank, suit = int(indicator[0]), indicator[1]
    if suit != "z":
        return f"{1 if rank == 9 else rank + 1}{suit}"
    if rank <= 4:
        return f"{1 if rank == 4 else rank + 1}z"
    return f"{5 if rank == 7 else rank + 1}z"


def _all_physical_tiles(hand_before: str, winning_tile: str, context: dict[str, Any]) -> list[str]:
    tiles = _notation_tiles(hand_before) + [winning_tile]
    for meld in context.get("fixed_melds", []):
        # A caller may model a fixed meld outside the 13-tile-equivalent input.
        if meld.get("included_in_hand", True) is False:
            tiles.extend(meld["tiles"])
        elif len(meld.get("tiles", ())) == 4:
            # Shape analysis represents a quad as three equivalent tiles, but
            # every physical tile in the quad still counts for dora.
            tiles.append(meld["tiles"][3])
    return tiles


def _dora_result(tiles: list[str], context: dict[str, Any], rule: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rule = rule or {"standard": True, "red": True, "kan": True, "ura": True, "kan_ura": True, "han_each": 1}
    face_up = list(context.get("dora_indicators", []))
    ura = list(context.get("ura_dora_indicators", []))
    if ura and not context.get("riichi_established"):
        raise ValueError("ura dora indicators require established riichi")
    normalized = [_normalize_tile(tile) for tile in tiles]
    reasons: list[dict[str, Any]] = []
    face_up = face_up if rule.get("kan", False) else face_up[:1]
    ura = ura if rule.get("kan_ura", False) else ura[:1]
    if not rule.get("standard", False):
        face_up = []
    if not rule.get("ura", False):
        ura = []
    for kind, indicators in (("standard_or_kan", face_up), ("ura_or_kan_ura", ura)):
        for indicator in indicators:
            dora = _next_dora(indicator)
            count = normalized.count(dora)
            if count:
                reasons.append({"kind": kind, "indicator": _normalize_tile(indicator), "dora": dora, "han": count * rule.get("han_each", 1)})
    red_count = sum(tile[0] == "0" and tile[1] in "mps" for tile in tiles) if rule.get("red", False) else 0
    if red_count:
        reasons.append({"kind": "red", "han": red_count})
    return {"han": sum(item["han"] for item in reasons), "reasons": reasons}


def _is_sequence(group: tuple[int, ...]) -> bool:
    return len(set(group)) == 3


def _wait_type(decomposition: dict[str, Any], winning_index: int, target: tuple[str, int]) -> str:
    kind, position = target
    if kind == "head":
        return "tanki"
    group = decomposition["melds"][position]
    if not _is_sequence(group):
        return "shanpon"
    remaining = list(group)
    remaining.remove(winning_index)
    ranks = [index % 9 + 1 for index in remaining]
    winning_rank = winning_index % 9 + 1
    if ranks[1] - ranks[0] == 2:
        return "kanchan"
    if (ranks == [1, 2] and winning_rank == 3) or (ranks == [8, 9] and winning_rank == 7):
        return "penchan"
    return "ryanmen"


def _fixed_details(context: dict[str, Any]) -> dict[tuple[int, ...], list[dict[str, Any]]]:
    result: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    for meld in context.get("fixed_melds", []):
        indices = tuple(sorted(TILE_NAMES.index(_normalize_tile(tile)) for tile in meld["tiles"]))
        signature = indices[:3] if len(indices) == 4 else indices
        result.setdefault(signature, []).append(meld)
    return result


def _fu_result(
    decomposition: dict[str, Any], assignment: tuple[str, int], method: str,
    context: dict[str, Any], yaku: list[dict], rule: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rule = rule or {"double_wind_pair_fu": 2, "chiitoitsu_fu": 25, "pinfu_tsumo_fu": 20, "open_pinfu_shape_ron_fu": 30, "rinshan_tsumo_fu": 2}
    yaku_ids = {item["concept_id"] for item in yaku}
    if "game.hand.yaku.chiitoitsu" in yaku_ids:
        value = int(rule["chiitoitsu_fu"])
        return {"raw_fu": value, "fu": value, "components": [{"kind": "chiitoitsu", "fu": value}]}
    concealed = bool(context["concealed"])
    if "game.hand.yaku.pinfu" in yaku_ids and method == "tsumo":
        value = int(rule["pinfu_tsumo_fu"])
        return {"raw_fu": value, "fu": value, "components": [{"kind": "pinfu_tsumo", "fu": value}]}

    components = [{"kind": "base", "fu": 20}]
    if concealed and method == "ron":
        components.append({"kind": "menzen_ron", "fu": 10})
    elif method == "tsumo":
        components.append({"kind": "tsumo", "fu": 2})
    wait = _wait_type(decomposition, TILE_NAMES.index(context["winning_tile"]), assignment)
    if wait in {"tanki", "kanchan", "penchan"}:
        components.append({"kind": "wait", "wait_type": wait, "fu": 2})

    pair_tile = TILE_NAMES[decomposition["head"][0]]
    if pair_tile in DRAGONS:
        components.append({"kind": "value_pair", "tile": pair_tile, "fu": 2})
    elif pair_tile in {context["prevailing_wind"], context["seat_wind"]}:
        double_wind = context["prevailing_wind"] == context["seat_wind"] == pair_tile
        components.append({"kind": "value_pair", "tile": pair_tile, "fu": int(rule["double_wind_pair_fu"]) if double_wind else 2})

    fixed = _fixed_details(context)
    used: Counter = Counter()
    for index, group in enumerate(decomposition["melds"]):
        if _is_sequence(group):
            continue
        signature = tuple(group)
        details = fixed.get(signature, [])
        fixed_meld = details[used[signature]] if used[signature] < len(details) else None
        if fixed_meld:
            used[signature] += 1
        is_quad = bool(fixed_meld and len(fixed_meld["tiles"]) == 4)
        is_open = bool(fixed_meld and fixed_meld.get("open", True))
        if assignment == ("meld", index) and method == "ron":
            is_open = True
        terminal_or_honor = TILE_NAMES[group[0]] in ORPHANS
        fu = (8 if is_quad else 2) * (2 if terminal_or_honor else 1) * (1 if is_open else 2)
        components.append({"kind": "quad" if is_quad else "triplet", "open": is_open, "tile": TILE_NAMES[group[0]], "fu": fu})
    raw = sum(item["fu"] for item in components)
    if not concealed and method == "ron" and raw == 20:
        value = int(rule["open_pinfu_shape_ron_fu"])
        return {"raw_fu": raw, "fu": value, "components": components, "exception": "open_pinfu_shape_ron"}
    return {"raw_fu": raw, "fu": ((raw + 9) // 10) * 10, "components": components}


def _yakuman(
    decomposition: dict[str, Any] | None, counts: tuple[int, ...], context: dict[str, Any],
    *, winning_assignment: tuple[str, int] | None = None, method: str | None = None,
) -> list[str]:
    tiles = [TILE_NAMES[index] for index, count in enumerate(counts) for _ in range(count)]
    found: list[str] = []
    concealed = bool(context["concealed"])
    if context.get("dealer") and context.get("win_before_first_discard"):
        found.append("tenhou")
    if not context.get("dealer") and context.get("win_on_uninterrupted_first_draw"):
        found.append("chiihou")
    if set(tiles) >= ORPHANS and all(tile in ORPHANS for tile in tiles):
        found.append("kokushi_musou")
    if all(tile in GREEN_TILES for tile in tiles):
        found.append("ryuuiisou")
    if all(tile.endswith("z") for tile in tiles):
        found.append("tsuuiisou")
    if all(tile[0] in "19" and tile[1] in "mps" for tile in tiles):
        found.append("chinroutou")
    if concealed and len({tile[1] for tile in tiles}) == 1 and tiles[0][1] in "mps":
        ranks = Counter(tile[0] for tile in tiles)
        for removable in list(ranks):
            ranks[removable] -= 1
            if sorted(rank for rank, amount in ranks.items() for _ in range(amount)) == list("1112345678999"):
                found.append("chuuren_poutou")
                break
            ranks[removable] += 1
    if decomposition:
        triplet_tiles = [TILE_NAMES[group[0]] for group in decomposition["melds"] if not _is_sequence(group)]
        head = TILE_NAMES[decomposition["head"][0]]
        if sum(tile in DRAGONS for tile in triplet_tiles) == 3:
            found.append("daisangen")
        wind_triplets = sum(tile in WINDS for tile in triplet_tiles)
        if wind_triplets == 4:
            found.append("daisuushii")
        elif wind_triplets == 3 and head in WINDS:
            found.append("shousuushii")
        # Runtime's count contains declared concealed kans only. Derive the
        # complete shape here, excluding the triplet completed by shanpon ron.
        if (
            concealed
            and len(triplet_tiles) == 4
            and all(not meld.get("open", True) for meld in context.get("fixed_melds", []))
            and (method == "tsumo" or (method == "ron" and winning_assignment == ("head", 0)))
        ):
            found.append("suuankou")
        if int(context.get("quad_count", -1)) == 4:
            found.append("suukantsu")
    return sorted(set(found))


def integrate_win_score(
    hand_before: str,
    winning_tile: str,
    method: str,
    *,
    context: dict[str, Any],
    profile_id: str,
    dealer: bool,
    honba_count: int = 0,
    rule_profile: ResolvedRuleProfile | None = None,
) -> dict[str, Any]:
    """Trace every valid completion candidate through highest-point selection."""
    if profile_id == "aotenjo_numeric_v1":
        return {
            "judgment_status": "REVIEW_REQUIRED",
            "reason": "aotenjo supports explicit numeric scoring only",
            "candidates": [],
        }
    snapshot = validate_default_profile_snapshot()
    if not snapshot["valid"]:
        return {"judgment_status": "REVIEW_REQUIRED", "invalid_sources": snapshot["invalid_sources"]}
    context = dict(context, dealer=dealer, winning_tile=_normalize_tile(winning_tile))
    ordinary_rule = rule_profile.value("yaku.ordinary_catalog_and_values") if rule_profile else {}
    if ordinary_rule.get("ippatsu") is False:
        context["ippatsu_right_active"] = False
    qualification = analyze_win(hand_before, winning_tile, method, context=context)
    if qualification["judgment_status"] != "QUALIFIED_TO_WIN":
        return {"judgment_status": qualification["judgment_status"], "qualification": qualification, "candidates": []}

    wait = next(
        (item for item in analyze_hand(hand_before)["waits"] if item["tile"] == context["winning_tile"]),
        {"complete_forms": qualification["complete_forms"]},
    )
    counts = parse_tiles(f"{hand_before}{winning_tile}")
    physical_tiles = _all_physical_tiles(hand_before, winning_tile, context)
    dora = _dora_result(physical_tiles, context, rule_profile.value("dora.profile") if rule_profile else None)
    limit_rule = rule_profile.value("scoring.limit_and_rounding") if rule_profile else None
    honba_rule = rule_profile.value("settlement.honba") if rule_profile else None
    score_override = None
    if limit_rule is not None and honba_rule is not None:
        score_override = {
            **limit_rule,
            "kiriage_mangan": [list(item) for item in limit_rule.get("kiriage_mangan", ())],
            "ordinary_11_to_12": limit_rule.get("ordinary_11_to_12", "sanbaiman"),
            "honba_ron": honba_rule.get("points_per_honba", 0) if honba_rule.get("ron_payer") == "discarder" else 0,
            "honba_tsumo_per_non_winner": honba_rule.get("tsumo_points_per_non_winner", 0),
        }
    def score(**values):
        return calculate_score(
            **values,
            profile_override=score_override,
            snapshot_id_override=rule_profile.snapshot_id if rule_profile else None,
        )
    fixed = _fixed_meld_signatures(context)
    candidates: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    decompositions = list(_fixed_standard_decompositions(counts, context))
    fixed_meld_count = sum(fixed.values())
    for decomposition_index, decomposition in enumerate(decompositions):
        assignments = _winning_assignments(decomposition, TILE_NAMES.index(context["winning_tile"]), fixed_meld_count=fixed_meld_count)
        for assignment_index, assignment in enumerate(assignments):
            yakuman = _yakuman(decomposition, counts, context, winning_assignment=assignment, method=method)
            candidate_id = f"standard-{decomposition_index}-{assignment_index}"
            if yakuman:
                score_result = score(fu=0, han=0, yakuman_units=len(yakuman), method=method, dealer=dealer, profile_id=profile_id, honba_count=honba_count)
                candidates.append({"candidate_id": candidate_id, "form": "hand.complete.standard", "decomposition": decomposition, "winning_assignment": assignment, "yakuman": yakuman, "ordinary_yaku": [], "dora": {"han": 0, "reasons": []}, "fu": None, "score": score_result})
                continue
            yaku = _common_yaku(counts, ["hand.complete.standard"], method, context) + _structural_yaku(counts, decomposition, TILE_NAMES.index(context["winning_tile"]), context, winning_assignment=assignment, method=method)
            unique = {item["concept_id"]: item for item in yaku}
            yaku = [unique[key] for key in sorted(unique)]
            if not context.get("concealed") and ordinary_rule.get("open_tanyao") is False:
                yaku = [item for item in yaku if item["concept_id"] != "game.hand.yaku.tanyao"]
            candidate_wait = _wait_type(decomposition, TILE_NAMES.index(context["winning_tile"]), assignment)
            if candidate_wait != "ryanmen":
                yaku = [item for item in yaku if item["concept_id"] != "game.hand.yaku.pinfu"]
            if not yaku:
                continue
            try:
                fu = _fu_result(
                    decomposition, assignment, method, context, yaku,
                    rule_profile.value("scoring.fu") if rule_profile else None,
                )
            except NotImplementedError as error:
                unsupported.append({"candidate_id": candidate_id, "reason": str(error)})
                continue
            han = sum(item["han"] for item in yaku) + dora["han"]
            score_result = score(fu=fu["fu"], han=han, method=method, dealer=dealer, profile_id=profile_id, honba_count=honba_count)
            candidates.append({"candidate_id": candidate_id, "form": "hand.complete.standard", "decomposition": decomposition, "winning_assignment": assignment, "wait_type": candidate_wait, "yakuman": [], "ordinary_yaku": yaku, "dora": dora, "total_han": han, "fu": fu, "score": score_result})

    if "hand.complete.seven_pairs" in wait["complete_forms"]:
        yakuman = _yakuman(None, counts, context)
        yaku = [] if yakuman else _common_yaku(counts, ["hand.complete.seven_pairs"], method, context)
        han = sum(item["han"] for item in yaku) + (0 if yakuman else dora["han"])
        score_result = score(fu=0 if yakuman else 25, han=0 if yakuman else han, yakuman_units=len(yakuman), method=method, dealer=dealer, profile_id=profile_id, honba_count=honba_count)
        candidates.append({"candidate_id": "seven-pairs", "form": "hand.complete.seven_pairs", "decomposition": None, "winning_assignment": ("pair", 0), "yakuman": yakuman, "ordinary_yaku": yaku, "dora": {"han": 0, "reasons": []} if yakuman else dora, "total_han": 0 if yakuman else han, "fu": None if yakuman else {"raw_fu": 25, "fu": 25, "components": [{"kind": "chiitoitsu", "fu": 25}]}, "score": score_result})
    if "hand.complete.thirteen_orphans" in wait["complete_forms"]:
        yakuman = _yakuman(None, counts, context)
        score_result = score(fu=0, han=0, yakuman_units=len(yakuman), method=method, dealer=dealer, profile_id=profile_id, honba_count=honba_count)
        candidates.append({"candidate_id": "thirteen-orphans", "form": "hand.complete.thirteen_orphans", "decomposition": None, "winning_assignment": ("orphan", 0), "yakuman": yakuman, "ordinary_yaku": [], "dora": {"han": 0, "reasons": []}, "fu": None, "score": score_result})

    if not candidates:
        return {"judgment_status": "UNSUPPORTED_RULE" if unsupported else "NOT_QUALIFIED", "qualification": qualification, "candidates": [], "unsupported_candidates": unsupported}
    highest = max(item["score"]["winner_base_receipt"] for item in candidates)
    high_point_candidates = [
        item for item in candidates
        if item["score"]["winner_base_receipt"] == highest
    ]
    high_point_candidates.sort(
        key=lambda item: (
            -int(item.get("total_han", 0)),
            int((item.get("fu") or {}).get("fu", 0)),
        )
    )
    selected = [item["candidate_id"] for item in high_point_candidates]
    return {
        "judgment_status": "SCORED",
        "profile": {"snapshot_id": rule_profile.snapshot_id if rule_profile else snapshot["snapshot_id"], "profile_id": profile_id},
        "qualification": qualification,
        "candidates": candidates,
        "unsupported_candidates": unsupported,
        "selected_candidate_ids": selected,
        "highest_winner_receipt_before_honba_and_deposits": highest,
        "selection_rule": "highest_winner_base_receipt_then_han_with_stable_ties",
        "excluded_judgments": ["riichi_deposits", "responsibility_payment", "multiple_winner_settlement", "dealer_continuation", "next_hand_transition"],
    }
