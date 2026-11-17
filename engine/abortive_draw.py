"""Qualified Tenhou abortive-draw detection, settlement, and kan guard."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from engine.game_state import GameState, MeldKind, RiichiState, SEATS, Seat
from engine.rule_profiles import ResolvedRuleProfile, require_profile_match
from engine.special_hand_end import (
    EvaluationTiming,
    HandEndCandidate,
    HandEndTrigger,
    ReasonCode,
    ResultFamily,
    SettledHandEndResult,
    TransitionDirective,
    settle_hand_end,
)


class AbortiveDrawError(ValueError):
    """A structured refusal to detect or settle an abortive draw."""

    def __init__(self, code: str, details: Mapping[str, Any] = {}):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class DiscardResponseObservation:
    """Facts owned by response resolution and unavailable from GameState alone."""

    event_id: str
    discarded_tile_id: str
    response_window_closed: bool
    qualified_ron_seats: tuple[Seat, ...] = ()
    accepted_call: bool = False

    def __post_init__(self) -> None:
        seats = tuple(self.qualified_ron_seats)
        if not self.event_id or not self.discarded_tile_id:
            raise AbortiveDrawError("INVALID_ABORTIVE_DRAW_TRIGGER")
        if type(self.response_window_closed) is not bool or type(self.accepted_call) is not bool:
            raise AbortiveDrawError("INVALID_ABORTIVE_DRAW_TRIGGER")
        if any(not isinstance(seat, Seat) for seat in seats) or len(seats) != len(set(seats)):
            raise AbortiveDrawError("INVALID_QUALIFIED_RON_SEATS")
        object.__setattr__(self, "qualified_ron_seats", seats)


@dataclass(frozen=True)
class AbortiveDrawDetection:
    adopted: bool
    reason_code: ReasonCode
    candidate: HandEndCandidate | None

    def __post_init__(self) -> None:
        if self.reason_code not in _PROFILE_REASON_BY_CODE:
            raise AbortiveDrawError("UNQUALIFIED_ABORTIVE_DRAW_REASON")
        if not self.adopted and self.candidate is not None:
            raise AbortiveDrawError("ABORTIVE_DRAW_CANDIDATE_MISMATCH")
        if self.candidate is not None and self.candidate.reason_code is not self.reason_code:
            raise AbortiveDrawError("ABORTIVE_DRAW_CANDIDATE_MISMATCH")


_DIMENSION = "hand.abortive_draws"
_GENERAL_CONCEPT = "game.hand.end.abortive_draw"
_PROFILE_REASON_BY_CODE = {
    ReasonCode.NINE_TERMINALS: "nine_terminals",
    ReasonCode.FOUR_WINDS_FIRST_DISCARD: "four_winds_first_discard",
    ReasonCode.FOUR_RIICHI: "four_riichi",
    ReasonCode.FOUR_KANS: "four_kans",
    ReasonCode.THREE_RON: "three_ron",
}
_CONCEPT_BY_CODE = {
    reason: f"ruleset.tenhou.abortive_draw.{name}"
    for reason, name in _PROFILE_REASON_BY_CODE.items()
}
_EXPECTED_SHARED_RESULT = {
    "score_movements": "none",
    "exhaustive_draw_tenpai_payment": "not_applied",
    "dealer_transition": "dealer_continues",
    "honba_transition": "increment_by_one",
    "riichi_deposit_transition": "carry_over",
    "round_and_hand_number": "unchanged",
}
_EXPECTED_PRIORITY = {
    "qualified_win_on_triggering_discard": "win_precedes_abortive_draw",
    "three_ron": "three_qualified_ron_claims_become_abortive_draw_instead_of_wins",
}


def _dimension(profile: ResolvedRuleProfile) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    try:
        entry = profile.dimensions[_DIMENSION]
        value = entry["value"]
        concepts = tuple(entry["concept_ids"])
    except (KeyError, TypeError) as error:
        raise AbortiveDrawError(
            "ABORTIVE_DRAW_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id}
        ) from error
    if not isinstance(value, Mapping) or type(value.get("adopted")) is not bool:
        raise AbortiveDrawError(
            "ABORTIVE_DRAW_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id}
        )
    if not all(isinstance(item, str) and item for item in concepts) or len(concepts) != len(set(concepts)):
        raise AbortiveDrawError(
            "ABORTIVE_DRAW_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id}
        )
    return value, concepts


def _executable_profile(
    profile: ResolvedRuleProfile, reason_code: ReasonCode
) -> tuple[Mapping[str, Any], tuple[str, ...]] | None:
    value, concepts = _dimension(profile)
    if value["adopted"] is False:
        return None
    reason_name = _PROFILE_REASON_BY_CODE.get(reason_code)
    reason_rules = value.get("reasons")
    required_concepts = {_GENERAL_CONCEPT, _CONCEPT_BY_CODE.get(reason_code)}
    if (
        reason_name is None
        or not isinstance(reason_rules, Mapping)
        or set(reason_rules) != set(_PROFILE_REASON_BY_CODE.values())
        or value.get("shared_result") != _EXPECTED_SHARED_RESULT
        or value.get("priority") != _EXPECTED_PRIORITY
        or None in required_concepts
        or not required_concepts <= set(concepts)
    ):
        raise AbortiveDrawError(
            "ABORTIVE_DRAW_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id}
        )
    return reason_rules[reason_name], concepts


def _candidate(
    profile: ResolvedRuleProfile,
    reason_code: ReasonCode,
    event_id: str,
    concepts: Sequence[str],
    *,
    declarer: Seat | None = None,
    related_seats: tuple[Seat, ...] = (),
    related_tile_ids: tuple[str, ...] = (),
) -> HandEndCandidate:
    timing = (
        EvaluationTiming.DRAW_ACTION
        if reason_code is ReasonCode.NINE_TERMINALS
        else EvaluationTiming.RESPONSE_RESOLUTION
    )
    return HandEndCandidate(
        result_family=ResultFamily.ABORTIVE_DRAW,
        reason_code=reason_code,
        ordinary_winner_seats=(),
        qualifier_seats=(),
        declared_tenpai_seats=(),
        trigger=HandEndTrigger(
            event_id,
            timing,
            declarer=declarer,
            related_seats=related_seats,
            related_tile_ids=related_tile_ids,
        ),
        rule_profile_id=profile.profile_id,
        rule_snapshot_id=profile.snapshot_id,
        applied_concept_ids=tuple(concepts),
    )


def _result(
    profile: ResolvedRuleProfile,
    reason_code: ReasonCode,
    candidate: HandEndCandidate | None,
) -> AbortiveDrawDetection:
    return AbortiveDrawDetection(True, reason_code, candidate)


def _is_terminal_or_honor(tile_type: str) -> bool:
    return tile_type.endswith("z") or tile_type[0] in {"1", "9"}


def detect_nine_terminals(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    declarer: Seat,
    trigger_event_id: str,
    first_self_draw_tile_id: str,
) -> AbortiveDrawDetection:
    """Accept a valid first-draw declaration without inferring event history."""

    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.NINE_TERMINALS)
    if resolved is None:
        return AbortiveDrawDetection(False, ReasonCode.NINE_TERMINALS, None)
    rules, concepts = resolved
    player = state.players_by_seat[declarer]
    expected = {
        "activation": "player_declaration",
        "minimum_distinct_terminal_or_honor_types": 9,
        "declaration_window": "after_first_self_draw_before_first_discard",
        "requires_no_prior_call_or_concealed_kan": True,
        "requires_no_prior_discard_by_declarer": True,
        "acceptance_timing": "immediately_after_valid_declaration",
    }
    if rules != expected:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "nine_terminals"})
    if not trigger_event_id or declarer is not state.current_turn:
        raise AbortiveDrawError("NINE_TERMINALS_DECLARATION_OUTSIDE_WINDOW")
    if player.river or any(item.melds for item in state.players):
        return _result(profile, ReasonCode.NINE_TERMINALS, None)
    hand_ids = {tile.tile_id for tile in player.concealed_hand}
    if first_self_draw_tile_id not in hand_ids:
        raise AbortiveDrawError("FIRST_SELF_DRAW_TILE_MISMATCH")
    distinct = {
        tile.tile_type for tile in player.concealed_hand if _is_terminal_or_honor(tile.tile_type)
    }
    candidate = None
    if len(distinct) >= rules["minimum_distinct_terminal_or_honor_types"]:
        candidate = _candidate(
            profile,
            ReasonCode.NINE_TERMINALS,
            trigger_event_id,
            concepts,
            declarer=declarer,
            related_tile_ids=(first_self_draw_tile_id,),
        )
    return _result(profile, ReasonCode.NINE_TERMINALS, candidate)


def _response_ready(observation: DiscardResponseObservation) -> bool:
    if not observation.response_window_closed:
        raise AbortiveDrawError("RESPONSE_WINDOW_NOT_CLOSED")
    return not observation.qualified_ron_seats and not observation.accepted_call


def detect_four_winds_first_discard(
    state: GameState,
    profile: ResolvedRuleProfile,
    observation: DiscardResponseObservation,
) -> AbortiveDrawDetection:
    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.FOUR_WINDS_FIRST_DISCARD)
    if resolved is None:
        return AbortiveDrawDetection(False, ReasonCode.FOUR_WINDS_FIRST_DISCARD, None)
    rules, concepts = resolved
    expected = {
        "activation": "automatic",
        "condition": "all_four_players_first_discard_is_same_wind_tile",
        "requires_no_call_before_fourth_discard": True,
        "acceptance_timing": "after_fourth_first_discard_response_window_closes_without_qualified_win_or_call",
    }
    if rules != expected:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "four_winds_first_discard"})
    ready = _response_ready(observation)
    rivers = [player.river for player in state.players]
    if any(len(river) != 1 for river in rivers) or any(player.melds for player in state.players):
        return _result(profile, ReasonCode.FOUR_WINDS_FIRST_DISCARD, None)
    first_discards = tuple(river[0].tile for river in rivers)
    if observation.discarded_tile_id not in {tile.tile_id for tile in first_discards}:
        raise AbortiveDrawError("TRIGGERING_DISCARD_MISMATCH")
    same_wind = len({tile.tile_type for tile in first_discards}) == 1 and first_discards[0].tile_type in {
        "1z", "2z", "3z", "4z"
    }
    candidate = None
    if ready and same_wind:
        candidate = _candidate(
            profile,
            ReasonCode.FOUR_WINDS_FIRST_DISCARD,
            observation.event_id,
            concepts,
            related_tile_ids=tuple(tile.tile_id for tile in first_discards),
        )
    return _result(profile, ReasonCode.FOUR_WINDS_FIRST_DISCARD, candidate)


def detect_four_riichi(
    state: GameState,
    profile: ResolvedRuleProfile,
    observation: DiscardResponseObservation,
) -> AbortiveDrawDetection:
    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.FOUR_RIICHI)
    if resolved is None:
        return AbortiveDrawDetection(False, ReasonCode.FOUR_RIICHI, None)
    rules, concepts = resolved
    if rules != {
        "activation": "automatic",
        "condition": "all_four_players_have_established_riichi",
        "unestablished_riichi_declaration_does_not_count": True,
        "acceptance_timing": "after_fourth_riichi_discard_response_window_closes_without_qualified_win",
    }:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "four_riichi"})
    if not observation.response_window_closed:
        raise AbortiveDrawError("RESPONSE_WINDOW_NOT_CLOSED")
    candidate = None
    if not observation.qualified_ron_seats and all(
        player.riichi is RiichiState.ESTABLISHED for player in state.players
    ):
        candidate = _candidate(
            profile,
            ReasonCode.FOUR_RIICHI,
            observation.event_id,
            concepts,
            related_seats=SEATS,
            related_tile_ids=(observation.discarded_tile_id,),
        )
    return _result(profile, ReasonCode.FOUR_RIICHI, candidate)


def detect_four_kans(
    state: GameState,
    profile: ResolvedRuleProfile,
    observation: DiscardResponseObservation,
) -> AbortiveDrawDetection:
    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.FOUR_KANS)
    if resolved is None:
        return AbortiveDrawDetection(False, ReasonCode.FOUR_KANS, None)
    rules, concepts = resolved
    if rules != {
        "activation": "automatic",
        "condition": "four_completed_kans_exist_across_at_least_two_players",
        "single_player_four_kans_exception": True,
        "acceptance_timing": "after_fourth_kan_replacement_draw_discard_response_window_closes_without_qualified_win",
        "fifth_kan_is_not_authorized": True,
        "fifth_kan_intent": "reject_without_state_transition",
    }:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "four_kans"})
    if not observation.response_window_closed:
        raise AbortiveDrawError("RESPONSE_WINDOW_NOT_CLOSED")
    owners = tuple(
        player.seat
        for player in state.players
        if any(meld.kind in {MeldKind.OPEN_KAN, MeldKind.CONCEALED_KAN, MeldKind.ADDED_KAN} for meld in player.melds)
    )
    candidate = None
    if state.kan_count == 4 and len(owners) >= 2 and not observation.qualified_ron_seats:
        candidate = _candidate(
            profile,
            ReasonCode.FOUR_KANS,
            observation.event_id,
            concepts,
            related_seats=owners,
            related_tile_ids=(observation.discarded_tile_id,),
        )
    return _result(profile, ReasonCode.FOUR_KANS, candidate)


def detect_three_ron(
    state: GameState,
    profile: ResolvedRuleProfile,
    observation: DiscardResponseObservation,
) -> AbortiveDrawDetection:
    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.THREE_RON)
    if resolved is None:
        return AbortiveDrawDetection(False, ReasonCode.THREE_RON, None)
    rules, concepts = resolved
    if rules != {
        "activation": "automatic_during_discard_response_resolution",
        "condition": "exactly_three_independently_qualified_ron_claims_on_same_discard",
        "acceptance_timing": "after_all_required_responses_are_collected",
        "result_replaces_ron_winners": True,
        "zero_one_or_two_qualified_ron_claims_do_not_trigger": True,
    }:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "three_ron"})
    if not observation.response_window_closed:
        raise AbortiveDrawError("RESPONSE_WINDOW_NOT_CLOSED")
    candidate = None
    if len(observation.qualified_ron_seats) == 3:
        candidate = _candidate(
            profile,
            ReasonCode.THREE_RON,
            observation.event_id,
            concepts,
            related_seats=observation.qualified_ron_seats,
            related_tile_ids=(observation.discarded_tile_id,),
        )
    return _result(profile, ReasonCode.THREE_RON, candidate)


def require_kan_intent_authorized(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    actor: Seat,
) -> None:
    """Reject every fifth-kan intent before any state transition."""

    require_profile_match(profile, state, error_type=AbortiveDrawError)
    resolved = _executable_profile(profile, ReasonCode.FOUR_KANS)
    if resolved is None:
        return
    rules, _ = resolved
    if rules.get("fifth_kan_is_not_authorized") is not True:
        raise AbortiveDrawError("UNSUPPORTED_ABORTIVE_DRAW_PROFILE", {"reason": "four_kans"})
    if state.kan_count >= 4:
        raise AbortiveDrawError(
            "FIFTH_KAN_NOT_AUTHORIZED", {"actor": actor.value, "kan_count": state.kan_count}
        )


def settle_abortive_draw(
    state: GameState,
    profile: ResolvedRuleProfile,
    candidate: HandEndCandidate,
) -> SettledHandEndResult:
    """Apply the shared no-payment, renchan, honba+1, deposit-carry result."""

    require_profile_match(profile, state, error_type=AbortiveDrawError)
    require_profile_match(profile, candidate, error_type=AbortiveDrawError)
    if candidate.result_family is not ResultFamily.ABORTIVE_DRAW:
        raise AbortiveDrawError("ABORTIVE_DRAW_CANDIDATE_REQUIRED")
    resolved = _executable_profile(profile, candidate.reason_code)
    if resolved is None:
        raise AbortiveDrawError("ABORTIVE_DRAW_NOT_ADOPTED")
    _, concepts = resolved
    deltas = MappingProxyType({seat: 0 for seat in SEATS})
    scores = {seat: state.players_by_seat[seat].score for seat in SEATS}
    directive = TransitionDirective(
        dealer_continues=True,
        advance_hand=False,
        honba_after=state.honba + 1,
        riichi_deposit_pool_after=state.riichi_deposits,
    )
    applied = tuple(dict.fromkeys((*candidate.applied_concept_ids, *concepts)))
    return settle_hand_end(
        candidate,
        profile,
        initial_scores=scores,
        riichi_deposit_pool_before=state.riichi_deposits,
        point_deltas=deltas,
        final_scores=scores,
        transition_directive=directive,
        applied_concept_ids=applied,
    )
