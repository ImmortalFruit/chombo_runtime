"""Compose an authorized next-hand state from settled inter-hand results."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from engine.game_state import SEATS, Seat
from engine.match_state import (
    MatchEndDecision,
    MatchEndResult,
    MatchState,
    TransitionDisposition,
    TransitionResult,
)
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile
from engine.setup import RNG_ID, SHUFFLE_SPEC_ID, create_initial_state


class NextHandStateError(ValueError):
    """A structured refusal to create a hand from unauthorized inputs."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class NextHandInitializationResult:
    """The new active match state and deterministic deal metadata."""

    seed: str
    rng_id: str
    shuffle_spec_id: str
    match_state: MatchState
    applied_concept_ids: tuple[str, ...]


def _next_seat(seat: Seat) -> Seat:
    return SEATS[(SEATS.index(seat) + 1) % len(SEATS)]


def _validate_inputs(
    match: MatchState,
    transition: TransitionResult,
    match_end: MatchEndResult,
    profile: ResolvedRuleProfile,
) -> None:
    require_profile_match(profile, match, error_type=NextHandStateError)
    if (
        transition.match_id != match.match_id
        or transition.completed_hand_id != match.current_hand_id
        or transition.hand_result.final_scores != transition.scores
        or dict(transition.scores) != dict(match_end.scores)
    ):
        raise NextHandStateError("TRANSITION_STATE_MISMATCH", {})
    expected_scores = {
        seat: match.scores[seat] + transition.hand_result.point_deltas[seat]
        for seat in SEATS
    }
    if dict(transition.scores) != expected_scores:
        raise NextHandStateError("TRANSITION_STATE_MISMATCH", {})
    if (
        match_end.match_id != match.match_id
        or match_end.completed_hand_id != match.current_hand_id
        or match_end.riichi_deposits != transition.carried_riichi_deposits
    ):
        raise NextHandStateError("MATCH_END_RESULT_MISMATCH", {})
    if (
        match_end.decision is not MatchEndDecision.CONTINUE
        or not match_end.next_hand_authorized
    ):
        raise NextHandStateError(
            "NEXT_HAND_NOT_AUTHORIZED", {"decision": match_end.decision.value}
        )
    reviewed_extension = (
        transition.disposition is TransitionDisposition.MATCH_END_REVIEW_REQUIRED
        and transition.next_round_wind is Seat.WEST
        and profile.value("match.end.extension").get("adopted") is True
    )
    if transition.disposition is not TransitionDisposition.NEXT_HAND_CANDIDATE and not reviewed_extension:
        raise NextHandStateError("TRANSITION_NOT_READY", {})
    if transition.next_dealer not in {match.dealer, _next_seat(match.dealer)}:
        raise NextHandStateError("INVALID_DEALER_TRANSITION", {})


def _rotate_to_next_seat_winds(
    match: MatchState, transition: TransitionResult
) -> tuple[dict[Seat, str], dict[Seat, int]]:
    """Return participant assignments and scores keyed by next-hand seat wind."""

    if transition.next_dealer is match.dealer:
        return dict(match.seat_assignments), dict(transition.scores)

    old_winds_from_new_east = tuple(
        SEATS[(SEATS.index(transition.next_dealer) + offset) % len(SEATS)]
        for offset in range(len(SEATS))
    )
    assignments = {
        new_wind: match.seat_assignments[old_wind]
        for new_wind, old_wind in zip(SEATS, old_winds_from_new_east)
    }
    scores = {
        new_wind: transition.scores[old_wind]
        for new_wind, old_wind in zip(SEATS, old_winds_from_new_east)
    }
    return assignments, scores


def initialize_next_hand_state(
    match: MatchState,
    transition: TransitionResult,
    match_end: MatchEndResult,
    profile: ResolvedRuleProfile,
    *,
    next_hand_id: str,
    seed: str,
) -> NextHandInitializationResult:
    """Create a fresh hand only after an explicit continuation authorization."""

    if not next_hand_id or next_hand_id == match.current_hand_id:
        raise NextHandStateError("INVALID_NEXT_HAND_ID", {})
    _validate_inputs(match, transition, match_end, profile)
    assignments, scores = _rotate_to_next_seat_winds(match, transition)

    setup = create_initial_state(
        seed,
        profile,
        dealer=Seat.EAST,
        round_wind=transition.next_round_wind,
        hand_number=transition.next_hand_number,
    )
    hand = setup.state
    players = tuple(
        replace(player, score=scores[player.seat]) for player in hand.players
    )
    hand = replace(
        hand,
        players=players,
        honba=transition.next_honba,
        riichi_deposits=transition.carried_riichi_deposits,
    )

    next_match = MatchState(
        match_id=match.match_id,
        participants=match.participants,
        seat_assignments=assignments,
        initial_dealer=match.initial_dealer,
        dealer=Seat.EAST,
        round_wind=transition.next_round_wind,
        hand_number=transition.next_hand_number,
        honba=transition.next_honba,
        riichi_deposits=transition.carried_riichi_deposits,
        scores=scores,
        rule_profile_id=profile.profile_id,
        rule_snapshot_id=profile.snapshot_id,
        current_hand_id=next_hand_id,
        current_hand=hand,
    )
    concept_ids = (
        "game.hand.transition.state_carryover",
        "game.hand.transition.seat_wind_assignment",
        "game.hand.transition.per_hand_reset",
        "game.match.setup.virtual_wall_and_deal_generation",
        "game.match.end.result",
        "game.hand.transition.next_hand_initialization",
    )
    return NextHandInitializationResult(
        seed, RNG_ID, SHUFFLE_SPEC_ID, next_match, concept_ids
    )
