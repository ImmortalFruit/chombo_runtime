"""Deterministic v0.1 hand-end normalization and point settlement."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from engine.game_state import SEATS, GameState, Seat
from engine.hand_log import HandEndReason, HandResult
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile


class SettlementError(ValueError):
    """A structured violation of the qualified settlement contract."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _validate_boundary(state: GameState, profile: ResolvedRuleProfile) -> None:
    require_profile_match(profile, state, error_type=SettlementError)


def _seat_set(seats: Iterable[Seat], *, field: str) -> tuple[Seat, ...]:
    normalized = tuple(seats)
    if len(set(normalized)) != len(normalized) or any(seat not in SEATS for seat in normalized):
        raise SettlementError("INVALID_SEAT_SET", {"field": field})
    return normalized


def _empty_components() -> dict[str, dict[Seat, int]]:
    return {
        name: {seat: 0 for seat in SEATS}
        for name in ("base_payment", "honba", "riichi_deposit", "tenpai_payment")
    }


def _apply_transfer(component: dict[Seat, int], payer: Seat, winner: Seat, points: int) -> None:
    if not isinstance(points, int) or isinstance(points, bool) or points < 0:
        raise SettlementError("INVALID_PAYMENT", {"points": points})
    component[payer] -= points
    component[winner] += points


def _validate_score_payments(score: Mapping[str, Any], method: str, dealer: bool) -> None:
    """Reject a scored document whose payment detail contradicts its receipt."""
    base = score.get("base_payments")
    if not isinstance(base, Mapping):
        raise SettlementError("INVALID_SCORE_PAYMENTS", {"field": "base_payments"})
    if method == "ron":
        expected_keys = {"discarder"}
        receipt = base.get("discarder")
    elif dealer:
        expected_keys = {"each_non_winner", "payer_count"}
        receipt = (
            base.get("each_non_winner") * base.get("payer_count")
            if all(isinstance(base.get(key), int) for key in expected_keys)
            else None
        )
    else:
        expected_keys = {"dealer", "each_non_dealer", "non_dealer_payer_count"}
        receipt = (
            base.get("dealer")
            + base.get("each_non_dealer") * base.get("non_dealer_payer_count")
            if all(isinstance(base.get(key), int) for key in expected_keys)
            else None
        )
    if (
        set(base) != expected_keys
        or not isinstance(receipt, int)
        or isinstance(receipt, bool)
        or receipt < 0
        or receipt != score.get("winner_base_receipt")
    ):
        raise SettlementError(
            "SCORE_PAYMENT_MISMATCH",
            {
                "base_payments": dict(base),
                "winner_base_receipt": score.get("winner_base_receipt"),
            },
        )


def _settle_win(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    winner: Seat,
    method: str,
    score: Mapping[str, Any],
    discarder: Seat | None,
) -> tuple[dict[str, dict[Seat, int]], int, tuple[str, ...]]:
    if score.get("judgment_status") != "SCORED":
        raise SettlementError("WIN_SCORE_NOT_FINAL", {})
    if method not in {"ron", "tsumo"} or score.get("input", {}).get("method") != method:
        raise SettlementError("WIN_METHOD_MISMATCH", {"method": method})
    if bool(score.get("input", {}).get("dealer")) != (winner is state.dealer):
        raise SettlementError("DEALER_STATUS_MISMATCH", {"winner": winner.value})

    _validate_score_payments(score, method, winner is state.dealer)

    components = _empty_components()
    base = score.get("base_payments", {})
    honba_rule = profile.value("settlement.honba")
    if method == "ron":
        if discarder is None or discarder is winner:
            raise SettlementError("INVALID_DISCARDER", {})
        _apply_transfer(components["base_payment"], discarder, winner, base.get("discarder"))
        _apply_transfer(
            components["honba"],
            discarder,
            winner,
            state.honba * int(honba_rule["points_per_honba"]),
        )
    else:
        if discarder is not None:
            raise SettlementError("TSUMO_HAS_DISCARDER", {"discarder": discarder.value})
        for payer in SEATS:
            if payer is winner:
                continue
            if winner is state.dealer:
                points = base.get("each_non_winner")
            elif payer is state.dealer:
                points = base.get("dealer")
            else:
                points = base.get("each_non_dealer")
            _apply_transfer(components["base_payment"], payer, winner, points)
            _apply_transfer(
                components["honba"],
                payer,
                winner,
                state.honba * int(honba_rule["tsumo_points_per_non_winner"]),
            )

    deposit_rule = profile.value("settlement.riichi_deposit")
    if deposit_rule["win"] != "approved_winner_takes_all":
        raise SettlementError("UNSUPPORTED_RULE", {"dimension_id": "settlement.riichi_deposit"})
    components["riichi_deposit"][winner] = state.riichi_deposits
    concepts = (
        "game.hand.end.win",
        "game.hand.win.result",
        "game.scoring.payment.ron" if method == "ron" else "game.scoring.payment.tsumo",
        "game.counter.honba",
        "game.hand.end.riichi_deposit",
        "game.hand.end.result",
    )
    return components, 0, concepts


def _settle_exhaustive_draw(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    declared_tenpai: tuple[Seat, ...],
) -> tuple[dict[str, dict[Seat, int]], int, tuple[str, ...]]:
    if state.live_wall:
        raise SettlementError("LIVE_WALL_NOT_EXHAUSTED", {"remaining": len(state.live_wall)})
    components = _empty_components()
    rule = profile.value("settlement.exhaustive_draw_tenpai")
    count = len(declared_tenpai)
    if count not in {0, 4}:
        total = int(rule["total_points"])
        noten = tuple(seat for seat in SEATS if seat not in declared_tenpai)
        if total % count or total % len(noten):
            raise SettlementError("NON_INTEGRAL_TENPAI_PAYMENT", {"total_points": total})
        for seat in declared_tenpai:
            components["tenpai_payment"][seat] = total // count
        for seat in noten:
            components["tenpai_payment"][seat] = -(total // len(noten))
    elif int(rule["zero_or_four_tenpai_transfer"]) != 0:
        raise SettlementError(
            "UNSUPPORTED_RULE", {"dimension_id": "settlement.exhaustive_draw_tenpai"}
        )

    deposit_rule = profile.value("settlement.riichi_deposit")
    if deposit_rule["exhaustive_draw"] != "carry_over":
        raise SettlementError("UNSUPPORTED_RULE", {"dimension_id": "settlement.riichi_deposit"})
    concepts = (
        "game.hand.end.exhaustive_draw",
        "game.hand.end.draw_tenpai_declaration",
        "game.hand.end.draw_payment",
        "game.hand.end.riichi_deposit",
        "game.hand.end.result",
    )
    return components, state.riichi_deposits, concepts


def settle_hand(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    end_reason: HandEndReason,
    winner: Seat | None = None,
    method: str | None = None,
    score: Mapping[str, Any] | None = None,
    discarder: Seat | None = None,
    declared_tenpai: Iterable[Seat] = (),
) -> dict[str, Any]:
    """Normalize a qualified win or exhaustive draw into one immutable result."""
    _validate_boundary(state, profile)
    tenpai = _seat_set(declared_tenpai, field="declared_tenpai")
    if end_reason in {HandEndReason.RON, HandEndReason.TSUMO}:
        expected_method = end_reason.value
        if winner is None or method != expected_method or score is None or tenpai:
            raise SettlementError("INVALID_WIN_SETTLEMENT_INPUT", {})
        components, pool_after, concepts = _settle_win(
            state,
            profile,
            winner=winner,
            method=method,
            score=score,
            discarder=discarder,
        )
        winners = (winner,)
    elif end_reason is HandEndReason.EXHAUSTIVE_DRAW:
        if winner is not None or method is not None or score is not None or discarder is not None:
            raise SettlementError("INVALID_DRAW_SETTLEMENT_INPUT", {})
        components, pool_after, concepts = _settle_exhaustive_draw(
            state, profile, declared_tenpai=tenpai
        )
        winners = ()
    else:
        return {
            "judgment_status": "UNSUPPORTED_RULE",
            "requested_end_reason": str(end_reason),
            **profile.execution_metadata(),
        }

    deltas = {
        seat: sum(component[seat] for component in components.values()) for seat in SEATS
    }
    final_scores = {seat: state.players_by_seat[seat].score + deltas[seat] for seat in SEATS}
    pool_before = state.riichi_deposits
    conservation = sum(deltas.values()) + pool_after - pool_before
    if conservation != 0:
        raise SettlementError("POINT_CONSERVATION_VIOLATION", {"difference": conservation})
    result = HandResult(end_reason, winners, deltas, final_scores, concepts)
    return {
        "judgment_status": "SETTLED",
        **profile.execution_metadata(),
        "end_reason": end_reason.value,
        "winner_ids": [seat.value for seat in winners],
        "dealer_id": state.dealer.value,
        "dealer_won": state.dealer in winners,
        "declared_tenpai_player_ids": [seat.value for seat in tenpai],
        "components": {
            name: {seat.value: value for seat, value in values.items()}
            for name, values in components.items()
        },
        "point_deltas": {seat.value: value for seat, value in deltas.items()},
        "final_scores": {seat.value: value for seat, value in final_scores.items()},
        "riichi_deposit_pool_before": pool_before,
        "riichi_deposit_transfer": pool_before - pool_after,
        "riichi_deposit_pool_after": pool_after,
        "conservation_difference": conservation,
        "hand_result": result,
        "next_hand_transition": "DEFERRED_TO_V0_2",
    }


def settle_multiple_ron(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    winners_and_scores: Iterable[tuple[Seat, Mapping[str, Any]]],
    discarder: Seat,
) -> dict[str, Any]:
    """Settle ordered ron winners, awarding honba and deposits to the first."""
    ordered = tuple(winners_and_scores)
    winners = _seat_set((winner for winner, _ in ordered), field="winners")
    if len(winners) < 2 or discarder in winners:
        raise SettlementError("INVALID_MULTIPLE_RON_INPUT", {})
    rule = profile.value("win.multiple_ron_resolution")
    if len(winners) > int(rule["maximum_winners"]) or rule.get("honba_and_deposit") != "first_winner_in_claimant_order":
        raise SettlementError("UNSUPPORTED_RULE", {"dimension_id": "win.multiple_ron_resolution"})
    combined = _empty_components()
    concepts: list[str] = []
    for index, (winner, score) in enumerate(ordered):
        item_state = state if index == 0 else replace(state, honba=0, riichi_deposits=0)
        components, _, item_concepts = _settle_win(
            item_state, profile, winner=winner, method="ron", score=score,
            discarder=discarder,
        )
        for name in combined:
            for seat in SEATS:
                combined[name][seat] += components[name][seat]
        for concept in item_concepts:
            if concept not in concepts:
                concepts.append(concept)
    deltas = {seat: sum(component[seat] for component in combined.values()) for seat in SEATS}
    final_scores = {seat: state.players_by_seat[seat].score + deltas[seat] for seat in SEATS}
    conservation = sum(deltas.values()) - state.riichi_deposits
    if conservation != 0:
        raise SettlementError("POINT_CONSERVATION_VIOLATION", {"difference": conservation})
    result = HandResult(HandEndReason.RON, winners, deltas, final_scores, tuple(concepts))
    return {
        "judgment_status": "SETTLED", **profile.execution_metadata(),
        "end_reason": "ron", "winner_ids": [seat.value for seat in winners],
        "dealer_id": state.dealer.value, "dealer_won": state.dealer in winners,
        "declared_tenpai_player_ids": [],
        "components": {name: {seat.value: value for seat, value in values.items()} for name, values in combined.items()},
        "point_deltas": {seat.value: value for seat, value in deltas.items()},
        "final_scores": {seat.value: value for seat, value in final_scores.items()},
        "riichi_deposit_pool_before": state.riichi_deposits,
        "riichi_deposit_transfer": state.riichi_deposits,
        "riichi_deposit_pool_after": 0,
        "conservation_difference": conservation, "hand_result": result,
        "next_hand_transition": "DEFERRED_TO_V0_2",
    }
