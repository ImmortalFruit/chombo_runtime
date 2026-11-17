"""E40-04 aggregate-rank condition projection."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from engine.aggregate_ranking import rank_aggregate_values
from engine.condition_candidates import (
    classify_dominance,
    generate_semantic_candidates,
    normalize_verified_candidates,
)
from engine.condition_documents import ConditionBundle
from engine.condition_limits import SearchLimitReached
from engine.condition_single_match import (
    SingleMatchConditionError,
    _project_frame,
    _validate_inputs,
)
from engine.final_points import calculate_final_points
from engine.match_state import MatchEndDecision, MatchEndResult, MatchState
from engine.rule_profiles import ResolvedRuleProfile


def calculate_aggregate_conditions(
    bundle: ConditionBundle, match: MatchState, profile: ResolvedRuleProfile,
    *, budget=None,
) -> tuple[Mapping[str, Any], ...]:
    """Return candidates whose current hand completes the match, then rank totals."""

    if bundle.request.get("ranking_scope") != "AGGREGATE_RANK":
        raise SingleMatchConditionError(
            "INVALID_REQUEST", "/ranking_scope", "AGGREGATE_RANK_REQUIRED",
        )
    aggregate_profile = bundle.aggregate_profile
    if aggregate_profile is None:
        raise SingleMatchConditionError(
            "RULES_INCOMPLETE", "/aggregate_context/profile_ref", "PROFILE_NOT_RESOLVED",
        )

    single_request = dict(bundle.request)
    single_request["ranking_scope"] = "SINGLE_MATCH_RANK"
    single_bundle = ConditionBundle(single_request, bundle.state, None, bundle.support_ref)
    by_participant, by_seat = _validate_inputs(single_bundle, match, profile)
    frames = generate_semantic_candidates(bundle)
    pre_values = {
        item["participant_id"]: item["value"]
        for item in bundle.request["aggregate_context"]["values"]
    }
    target_id = bundle.request["target"]["participant_id"]
    target_rank = bundle.request["target"]["target_rank"]
    verified: list[dict[str, Any]] = []
    for frame in frames:
        try:
            projections = _project_frame(
                frame, single_bundle, match, profile, by_participant, by_seat,
            )
            for projected in projections:
                if budget is not None:
                    budget.consume_transition()
                match_end_evidence = projected["evidence"]["match_end"]
                if match_end_evidence["decision"] != MatchEndDecision.END.value:
                    continue
                scores = {
                    by_participant[participant]: value
                    for participant, value in projected["evidence"]["scores_after"].items()
                }
                match_end = MatchEndResult(
                    match_id=match.match_id,
                    completed_hand_id=match.current_hand_id,
                    decision=MatchEndDecision.END,
                    reason_code=match_end_evidence["reason_code"],
                    next_hand_authorized=False,
                    scores=scores,
                    riichi_deposits=match_end_evidence["riichi_deposits"],
                    applied_concept_ids=tuple(match_end_evidence["applied_concept_ids"]),
                )
                final = calculate_final_points(match, match_end, profile)
                final_by_participant = {
                    player.participant_id: Decimal(str(player.final_competition_points))
                    for player in final.players
                }
                aggregate = rank_aggregate_values(
                    pre_values=pre_values,
                    final_points=final_by_participant,
                    profile=aggregate_profile,
                )
                item = dict(projected)
                item["target_satisfied"] = aggregate["ranks"][target_id] <= target_rank
                evidence = dict(projected["evidence"])
                evidence["final_points"] = {
                    player.participant_id: {
                        "rank": player.rank,
                        "final_score": player.final_score,
                        "raw_points": player.raw_points,
                        "oka_points": player.oka_points,
                        "uma_points": player.uma_points,
                        "other_rank_points": player.other_rank_points,
                        "final_competition_points": player.final_competition_points,
                    }
                    for player in final.players
                }
                evidence["aggregate"] = aggregate
                evidence["aggregate_profile_ref"] = {
                    "profile_id": aggregate_profile["profile_id"],
                    "profile_version": aggregate_profile["profile_version"],
                    "content_hash": aggregate_profile["content_hash"],
                }
                item["evidence"] = evidence
                if budget is not None:
                    budget.consume_candidate()
                verified.append(item)
        except SearchLimitReached as error:
            partial = classify_dominance(normalize_verified_candidates(frames, verified))
            raise SearchLimitReached(error.reason, error.usage, partial) from error
    normalized = normalize_verified_candidates(frames, verified)
    return classify_dominance(normalized)
