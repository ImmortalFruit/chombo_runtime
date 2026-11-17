"""Deterministic settlement for the qualified standard nagashi-mangan profile."""

from __future__ import annotations

from typing import Any, Mapping

from engine.game_state import GameState, SEATS, Seat
from engine.rule_profiles import ResolvedRuleProfile, require_profile_match
from engine.special_hand_end import (
    HandEndCandidate,
    ResultFamily,
    SettledHandEndResult,
    TransitionDirective,
    settle_hand_end,
)


class NagashiManganSettlementError(ValueError):
    """A structured refusal to settle an incomplete nagashi result."""

    def __init__(self, code: str, details: Mapping[str, Any] = {}):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


_DIMENSION = "hand.special.nagashi_mangan"
_REQUIRED_CONCEPTS = (
    "game.hand.end.special.nagashi_mangan_definition",
    "game.hand.end.special.nagashi_mangan_profile",
    "game.hand.end.special.nagashi_mangan_settlement",
    "game.hand.end.special.nagashi_mangan_standard_profile",
)


def _standard_value(profile: ResolvedRuleProfile) -> Mapping[str, Any]:
    try:
        dimension = profile.dimensions[_DIMENSION]
        value = dimension["value"]
        concepts = set(dimension["concept_ids"])
    except (KeyError, TypeError) as error:
        raise NagashiManganSettlementError(
            "NAGASHI_MANGAN_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id}
        ) from error
    expected = {
        "adopted": True,
        "qualification": {
            "end_condition": "exhaustive_draw",
            "all_own_discards_are_terminals_or_honors": True,
            "any_own_discard_called_by_another_player": False,
            "own_open_meld_disqualifies": False,
            "tenpai_required": False,
        },
        "result_classification": "nagashi_mangan_special_draw_settlement",
        "payment": {
            "method": "mangan_tsumo_equivalent",
            "dealer_qualifier": {"each_non_dealer_pays": 4000},
            "non_dealer_qualifier": {
                "dealer_pays": 4000,
                "each_other_non_dealer_pays": 2000,
            },
        },
        "honba": {"each_payer_adds_per_honba": 100},
        "riichi_deposit": {
            "sole_qualifier_receives_all": True,
            "multiple_qualifiers": "first_qualifier_from_dealer_receives_all",
        },
        "exhaustive_draw_tenpai_payment": "not_applied",
        "dealer_transition": {
            "dealer_qualifies": "repeat_same_hand_and_increment_honba",
            "dealer_does_not_qualify": "advance_dealer_and_increment_honba",
        },
        "multiple_qualifiers": "settle_each_qualifier_independently",
    }
    if value != expected or not set(_REQUIRED_CONCEPTS) <= concepts:
        raise NagashiManganSettlementError(
            "UNSUPPORTED_NAGASHI_MANGAN_SETTLEMENT_PROFILE",
            {"profile_id": profile.profile_id},
        )
    return value


def _first_from_dealer(qualifiers: tuple[Seat, ...], dealer: Seat) -> Seat:
    order = SEATS[SEATS.index(dealer):] + SEATS[:SEATS.index(dealer)]
    return next(seat for seat in order if seat in qualifiers)


def settle_nagashi_mangan(
    state: GameState,
    profile: ResolvedRuleProfile,
    candidate: HandEndCandidate,
) -> SettledHandEndResult:
    """Settle every qualifier independently and resolve counters exactly once."""

    require_profile_match(profile, state, error_type=NagashiManganSettlementError)
    require_profile_match(profile, candidate, error_type=NagashiManganSettlementError)
    if candidate.result_family is not ResultFamily.NAGASHI_MANGAN:
        raise NagashiManganSettlementError("NAGASHI_MANGAN_CANDIDATE_REQUIRED")
    _standard_value(profile)

    deltas = {seat: 0 for seat in SEATS}
    honba_payment = state.honba * 100
    for qualifier in candidate.qualifier_seats:
        for payer in SEATS:
            if payer is qualifier:
                continue
            if qualifier is state.dealer or payer is state.dealer:
                base = 4000
            else:
                base = 2000
            payment = base + honba_payment
            deltas[payer] -= payment
            deltas[qualifier] += payment

    deposit_recipient = _first_from_dealer(candidate.qualifier_seats, state.dealer)
    deltas[deposit_recipient] += state.riichi_deposits
    pool_after = 0
    final_scores = {
        seat: state.players_by_seat[seat].score + deltas[seat] for seat in SEATS
    }
    dealer_continues = state.dealer in candidate.qualifier_seats
    directive = TransitionDirective(
        dealer_continues=dealer_continues,
        advance_hand=not dealer_continues,
        honba_after=state.honba + 1,
        riichi_deposit_pool_after=pool_after,
    )
    concepts = tuple(dict.fromkeys((*candidate.applied_concept_ids, *_REQUIRED_CONCEPTS)))
    return settle_hand_end(
        candidate,
        profile,
        initial_scores={seat: state.players_by_seat[seat].score for seat in SEATS},
        riichi_deposit_pool_before=state.riichi_deposits,
        point_deltas=deltas,
        final_scores=final_scores,
        transition_directive=directive,
        applied_concept_ids=concepts,
    )
