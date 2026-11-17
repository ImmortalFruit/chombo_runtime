"""Deterministic rule-profile driven transition after one settled hand."""

from __future__ import annotations

from typing import Any, Mapping

from engine.game_state import SEATS, Seat
from engine.hand_log import HandEndReason, HandResult
from engine.match_state import MatchState, TransitionDisposition, TransitionResult
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile, RuleProfileError


class NextHandTransitionError(ValueError):
    """A structured refusal to derive a transition from incomplete input or rules."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


_DIMENSIONS = (
    "transition.dealer",
    "transition.honba",
    "transition.riichi_deposit",
    "transition.round_and_seat_winds",
    "match.end.extension",
)


def _profile_value(profile: ResolvedRuleProfile, dimension_id: str) -> Mapping[str, Any]:
    try:
        value = profile.value(dimension_id)
    except RuleProfileError as error:
        raise NextHandTransitionError(
            "UNRESOLVED_TRANSITION_RULE",
            {"dimension_id": dimension_id, "profile_id": profile.profile_id},
        ) from error
    if not isinstance(value, Mapping):
        raise NextHandTransitionError(
            "UNSUPPORTED_TRANSITION_RULE",
            {"dimension_id": dimension_id, "value": value},
        )
    return value


def _require_rule(rule: Mapping[str, Any], key: str, expected: Any, dimension: str) -> None:
    if rule.get(key) != expected:
        raise NextHandTransitionError(
            "UNSUPPORTED_TRANSITION_RULE",
            {"dimension_id": dimension, "key": key, "value": rule.get(key)},
        )


def _next_seat(seat: Seat) -> Seat:
    return SEATS[(SEATS.index(seat) + 1) % len(SEATS)]


def _next_honba(rule: Mapping[str, Any], *, dealer_continues: bool, exhaustive_draw: bool, current: int) -> int:
    if dealer_continues:
        semantic = rule.get("dealer_continues")
    elif exhaustive_draw:
        semantic = rule.get("exhaustive_draw_and_dealer_changes", rule.get("dealer_changes"))
    else:
        semantic = rule.get("non_dealer_win", rule.get("dealer_changes"))
    if semantic == "increment_by_one":
        return current + 1
    if semantic == "reset_to_zero":
        return 0
    if semantic == "remain_zero" and current == 0:
        return 0
    raise NextHandTransitionError(
        "UNSUPPORTED_TRANSITION_RULE",
        {"dimension_id": "transition.honba", "value": semantic},
    )


def calculate_next_hand_transition(
    match: MatchState,
    hand_result: HandResult,
    profile: ResolvedRuleProfile,
    *,
    riichi_deposits_after: int,
    declared_tenpai: tuple[Seat, ...] | None = None,
) -> TransitionResult:
    """Return the next-state candidate without authorizing or generating a hand."""

    require_profile_match(profile, match, error_type=NextHandTransitionError)
    expected_scores = {
        seat: match.scores[seat] + hand_result.point_deltas[seat] for seat in SEATS
    }
    if dict(hand_result.final_scores) != expected_scores:
        raise NextHandTransitionError("HAND_RESULT_STATE_MISMATCH", {})
    if not isinstance(riichi_deposits_after, int) or riichi_deposits_after < 0:
        raise NextHandTransitionError(
            "INVALID_RIICHI_DEPOSIT_POOL", {"value": riichi_deposits_after}
        )

    dealer_rule = _profile_value(profile, "transition.dealer")
    honba_rule = _profile_value(profile, "transition.honba")
    deposit_rule = _profile_value(profile, "transition.riichi_deposit")
    round_rule = _profile_value(profile, "transition.round_and_seat_winds")

    abortive_reasons = {
        HandEndReason.NINE_TERMINALS,
        HandEndReason.FOUR_WINDS_FIRST_DISCARD,
        HandEndReason.FOUR_RIICHI,
        HandEndReason.FOUR_KANS,
        HandEndReason.THREE_RON,
    }
    if hand_result.end_reason is HandEndReason.EXHAUSTIVE_DRAW:
        if declared_tenpai is None:
            raise NextHandTransitionError(
                "MISSING_DECLARED_TENPAI", {"required_for": "exhaustive_draw"}
            )
        if len(set(declared_tenpai)) != len(declared_tenpai) or any(
            seat not in SEATS for seat in declared_tenpai
        ):
            raise NextHandTransitionError("INVALID_DECLARED_TENPAI", {})
        _require_rule(
            deposit_rule,
            "exhaustive_draw",
            "carry_entire_pool_forward",
            "transition.riichi_deposit",
        )
        expected_deposits = (
            match.riichi_deposits
            + sum(match.scores.values())
            - sum(hand_result.final_scores.values())
        )
        if riichi_deposits_after != expected_deposits:
            raise NextHandTransitionError(
                "RIICHI_DEPOSIT_CARRYOVER_MISMATCH",
                {"expected": expected_deposits, "actual": riichi_deposits_after},
            )
        dealer_continues = match.dealer in declared_tenpai
        dealer_key = (
            "dealer_continues_on_exhaustive_draw_when_tenpai"
            if dealer_continues
            else "dealer_changes_on_exhaustive_draw_when_noten"
        )
        _require_rule(dealer_rule, dealer_key, True, "transition.dealer")
        next_honba = _next_honba(
            honba_rule, dealer_continues=dealer_continues,
            exhaustive_draw=True, current=match.honba,
        )
    elif hand_result.end_reason is HandEndReason.NAGASHI_MANGAN:
        if declared_tenpai is not None:
            raise NextHandTransitionError(
                "UNEXPECTED_DECLARED_TENPAI", {"end_reason": hand_result.end_reason.value}
            )
        if riichi_deposits_after != 0:
            raise NextHandTransitionError("RIICHI_DEPOSIT_TRANSFER_MISMATCH", {})
        dealer_continues = match.dealer in hand_result.winners
        next_honba = match.honba + 1
    elif hand_result.end_reason in abortive_reasons:
        if declared_tenpai is not None:
            raise NextHandTransitionError(
                "UNEXPECTED_DECLARED_TENPAI", {"end_reason": hand_result.end_reason.value}
            )
        expected_deposits = (
            match.riichi_deposits
            + sum(match.scores.values())
            - sum(hand_result.final_scores.values())
        )
        if riichi_deposits_after != expected_deposits:
            raise NextHandTransitionError(
                "RIICHI_DEPOSIT_CARRYOVER_MISMATCH",
                {"expected": expected_deposits, "actual": riichi_deposits_after},
            )
        dealer_continues = True
        next_honba = match.honba + 1
    else:
        if declared_tenpai is not None:
            raise NextHandTransitionError(
                "UNEXPECTED_DECLARED_TENPAI", {"end_reason": hand_result.end_reason.value}
            )
        _require_rule(
            deposit_rule,
            "qualified_win",
            "transfer_to_approved_winner",
            "transition.riichi_deposit",
        )
        if riichi_deposits_after != 0:
            raise NextHandTransitionError("RIICHI_DEPOSIT_TRANSFER_MISMATCH", {})
        dealer_continues = match.dealer in hand_result.winners
        if dealer_continues:
            _require_rule(
                dealer_rule, "dealer_continues_on_win", True, "transition.dealer"
            )
            next_honba = _next_honba(
                honba_rule, dealer_continues=True,
                exhaustive_draw=False, current=match.honba,
            )
        else:
            next_honba = _next_honba(
                honba_rule, dealer_continues=False,
                exhaustive_draw=False, current=match.honba,
            )

    if dealer_continues:
        _require_rule(
            round_rule,
            "dealer_continues",
            "same_hand_and_seat_winds",
            "transition.round_and_seat_winds",
        )
        next_dealer = match.dealer
        next_round_wind = match.round_wind
        next_hand_number = match.hand_number
        disposition = (
            TransitionDisposition.MATCH_END_REVIEW_REQUIRED
            if match.round_wind is Seat.WEST and match.hand_number == 4
            else TransitionDisposition.NEXT_HAND_CANDIDATE
        )
    else:
        _require_rule(
            round_rule,
            "dealer_changes",
            "advance_hand_and_rotate_seat_winds",
            "transition.round_and_seat_winds",
        )
        next_dealer = _next_seat(match.dealer)
        if match.round_wind is Seat.WEST and match.hand_number == 4:
            next_round_wind = Seat.WEST
            next_hand_number = 4
            disposition = TransitionDisposition.MATCH_END_REVIEW_REQUIRED
        elif match.round_wind is Seat.SOUTH and match.hand_number == 4:
            extension = _profile_value(profile, "match.end.extension")
            next_round_wind = Seat.WEST if extension.get("adopted") else Seat.SOUTH
            next_hand_number = 1 if extension.get("adopted") else 4
            disposition = TransitionDisposition.MATCH_END_REVIEW_REQUIRED
        elif match.hand_number == 4:
            next_round_wind = Seat.SOUTH
            next_hand_number = 1
            disposition = TransitionDisposition.NEXT_HAND_CANDIDATE
        else:
            next_round_wind = match.round_wind
            next_hand_number = match.hand_number + 1
            disposition = TransitionDisposition.NEXT_HAND_CANDIDATE

    concept_ids: list[str] = []
    for dimension_id in _DIMENSIONS:
        for concept_id in profile.dimensions[dimension_id]["concept_ids"]:
            if concept_id not in concept_ids:
                concept_ids.append(concept_id)
    for concept_id in ("game.hand.transition.result",):
        if concept_id not in concept_ids:
            concept_ids.append(concept_id)

    return TransitionResult(
        match_id=match.match_id,
        completed_hand_id=match.current_hand_id,
        hand_result=hand_result,
        disposition=disposition,
        next_dealer=next_dealer,
        next_round_wind=next_round_wind,
        next_hand_number=next_hand_number,
        next_honba=next_honba,
        carried_riichi_deposits=riichi_deposits_after,
        scores=hand_result.final_scores,
        applied_concept_ids=tuple(concept_ids),
    )
