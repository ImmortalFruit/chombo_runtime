"""Deterministic match-end decision from an already-settled transition."""

from __future__ import annotations

from typing import Any, Mapping

from engine.game_state import Seat
from engine.match_state import (
    MatchEndDecision,
    MatchEndResult,
    MatchState,
    TransitionDisposition,
    TransitionResult,
)
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile, RuleProfileError


class MatchEndDecisionError(ValueError):
    """A structured refusal to infer a match-end rule or missing input."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


_DIMENSIONS = (
    "match.structure",
    "match.end.scheduled",
    "match.end.bankruptcy",
    "match.end.extension",
)


def _profile_value(profile: ResolvedRuleProfile, dimension_id: str) -> Mapping[str, Any]:
    try:
        value = profile.value(dimension_id)
    except RuleProfileError as error:
        raise MatchEndDecisionError(
            "UNRESOLVED_MATCH_END_RULE",
            {"dimension_id": dimension_id, "profile_id": profile.profile_id},
        ) from error
    if not isinstance(value, Mapping):
        raise MatchEndDecisionError(
            "UNSUPPORTED_MATCH_END_RULE",
            {"dimension_id": dimension_id, "value": value},
        )
    return value


def _require(rule: Mapping[str, Any], key: str, expected: Any, dimension_id: str) -> None:
    if rule.get(key) != expected:
        raise MatchEndDecisionError(
            "UNSUPPORTED_MATCH_END_RULE",
            {"dimension_id": dimension_id, "key": key, "value": rule.get(key)},
        )


def decide_match_end(
    match: MatchState,
    transition: TransitionResult,
    profile: ResolvedRuleProfile,
) -> MatchEndResult:
    """Authorize the next-hand candidate or end the M-League east-south match."""

    require_profile_match(profile, match, error_type=MatchEndDecisionError)
    expected_scores = {
        seat: match.scores[seat] + transition.hand_result.point_deltas[seat]
        for seat in match.scores
    }
    if (
        transition.match_id != match.match_id
        or transition.completed_hand_id != match.current_hand_id
        or transition.hand_result.final_scores != transition.scores
        or dict(transition.scores) != dict(transition.hand_result.final_scores)
        or dict(transition.scores) != expected_scores
    ):
        raise MatchEndDecisionError("TRANSITION_STATE_MISMATCH", {})

    structure = _profile_value(profile, "match.structure")
    scheduled = _profile_value(profile, "match.end.scheduled")
    bankruptcy = _profile_value(profile, "match.end.bankruptcy")
    extension = _profile_value(profile, "match.end.extension")
    final_points = _profile_value(profile, "match.final_points")
    known_no_extension = (
        extension.get("adopted") is False
        and (extension.get("must_not_create_west_round") is True or extension.get("west_round") is False)
    )
    known_extension = (
        extension.get("adopted") is True
        and extension.get("west_round") is True
        and extension.get("sudden_death") is True
        and extension.get("maximum_final_hand") == "west_4"
        and isinstance(extension.get("threshold_points"), int)
    )
    if not (known_no_extension or known_extension):
        raise MatchEndDecisionError(
            "UNSUPPORTED_MATCH_END_RULE",
            {"dimension_id": "match.end.extension", "value": dict(extension)},
        )
    _require(structure, "format", "east_south", "match.structure")
    if structure.get("scheduled_final_hand") != {
        "prevailing_wind": "south",
        "hand_number": 4,
    }:
        raise MatchEndDecisionError(
            "UNSUPPORTED_MATCH_END_RULE",
            {"dimension_id": "match.structure", "key": "scheduled_final_hand"},
        )
    at_scheduled_final = match.round_wind is Seat.SOUTH and match.hand_number == 4
    at_extension_limit = match.round_wind is Seat.WEST and match.hand_number == 4
    dealer_continues = transition.next_dealer is match.dealer
    negative = any(score < 0 for score in transition.scores.values())
    zero_or_negative = any(score <= 0 for score in transition.scores.values())
    bankruptcy_end = (
        bankruptcy.get("bankruptcy_end") is True
        and (negative or (bankruptcy.get("zero_score_ends_match") is True and zero_or_negative))
    ) or (bankruptcy.get("zero_or_negative_score_ends_match") is True and zero_or_negative)

    decision = MatchEndDecision.CONTINUE
    reason_code = "SCHEDULED_FINAL_HAND_NOT_REACHED"
    if bankruptcy_end:
        decision = MatchEndDecision.END
        reason_code = "BANKRUPTCY_THRESHOLD_REACHED"
    elif at_extension_limit:
        decision = MatchEndDecision.END
        reason_code = "EXTENSION_FINAL_HAND_REACHED"
    elif match.round_wind is Seat.WEST and extension.get("sudden_death"):
        threshold = extension.get("threshold_points")
        if isinstance(threshold, int) and max(transition.scores.values()) >= threshold:
            decision = MatchEndDecision.END
            reason_code = "EXTENSION_SUDDEN_DEATH_THRESHOLD_REACHED"
    elif at_scheduled_final:
        dealer_score = transition.scores[match.dealer]
        dealer_is_top = dealer_score == max(transition.scores.values())
        threshold = final_points.get("return_points")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
            raise MatchEndDecisionError(
                "UNSUPPORTED_MATCH_END_RULE",
                {"dimension_id": "match.final_points", "key": "return_points"},
            )
        if "dealer_win" in scheduled:
            dealer_win = match.dealer in transition.hand_result.winners
            outcome = "dealer_win" if dealer_win else ("dealer_tenpai" if dealer_continues else "dealer_noten")
            semantic = scheduled[outcome]
            if semantic == "end":
                decision = MatchEndDecision.END
            elif semantic == "continue":
                decision = MatchEndDecision.CONTINUE
            elif semantic.startswith("end_if_dealer_is_top"):
                decision = MatchEndDecision.END if dealer_is_top and dealer_score >= threshold else MatchEndDecision.CONTINUE
            elif semantic.startswith("end_if_any_player"):
                decision = MatchEndDecision.END if max(transition.scores.values()) >= threshold else MatchEndDecision.CONTINUE
            else:
                raise MatchEndDecisionError("UNSUPPORTED_MATCH_END_RULE", {"dimension_id": "match.end.scheduled", "value": semantic})
        else:
            decision = MatchEndDecision.CONTINUE if dealer_continues else MatchEndDecision.END
        if "dealer_changes" in scheduled:
            reason_code = (
                "SCHEDULED_FINAL_HAND_DEALER_CONTINUES"
                if decision is MatchEndDecision.CONTINUE
                else "SCHEDULED_FINAL_HAND_DEALER_CHANGED"
            )
        else:
            reason_code = "SCHEDULED_FINAL_HAND_CONTINUES" if decision is MatchEndDecision.CONTINUE else "SCHEDULED_FINAL_HAND_ENDED"

    expected_disposition = (
        TransitionDisposition.MATCH_END_REVIEW_REQUIRED
        if (at_scheduled_final and not dealer_continues) or at_extension_limit
        else TransitionDisposition.NEXT_HAND_CANDIDATE
    )
    if transition.disposition is not expected_disposition:
        raise MatchEndDecisionError("TRANSITION_DISPOSITION_MISMATCH", {})

    concept_ids: list[str] = []
    for concept_id in (
        "game.match.end.input",
        "game.match.end.exceptional",
        "game.match.end.continuation",
    ):
        if concept_id not in concept_ids:
            concept_ids.append(concept_id)
    for dimension_id in _DIMENSIONS:
        for concept_id in profile.dimensions[dimension_id]["concept_ids"]:
            if concept_id not in concept_ids:
                concept_ids.append(concept_id)
    if "game.match.end.result" not in concept_ids:
        concept_ids.append("game.match.end.result")

    return MatchEndResult(
        match_id=match.match_id,
        completed_hand_id=match.current_hand_id,
        decision=decision,
        reason_code=reason_code,
        next_hand_authorized=decision is MatchEndDecision.CONTINUE,
        scores=transition.scores,
        riichi_deposits=transition.carried_riichi_deposits,
        applied_concept_ids=tuple(concept_ids),
    )
