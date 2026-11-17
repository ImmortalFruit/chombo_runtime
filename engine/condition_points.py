"""Qualified point-goal projection using the existing scoring/match boundaries."""
from decimal import Decimal, InvalidOperation
from time import monotonic_ns

from engine.condition_candidates import (
    CandidateError, _plain, classify_dominance, generate_semantic_candidates,
    normalize_verified_candidates,
)
from engine.application_schema import INPUTS, validate
from engine.condition_documents import ConditionBundle
from engine.condition_form import _hash, prepare_state, selected_profile
from engine.condition_limits import SearchBudget, SearchLimitReached
from engine.condition_single_match import _project_frame, _validate_inputs
from engine.condition_skill import match_from_condition_state, qualified_concept_ids
from engine.final_points import calculate_final_points
from engine.match_state import MatchEndDecision, MatchEndResult
from engine.navigator import NavigatorError


POINT_CONCEPT_IDS = (
    "game.condition.aggregate_points_condition",
    "game.condition.points_minimal_condition",
    "game.condition.reachability", "game.condition.candidate_dominance",
    "game.condition.aggregate_profile.value_and_addition",
)
CALCULATOR_VERSION = "chombo.point-conditions.v0.5.2"
POLICY = "EXACT_SUM_NO_EXTRA_ADJUSTMENTS"


def point_comparison(before, addition, target, comparison):
    """Compare exact decimal values; never change the meaning of equality."""
    if comparison not in {"GT", "GTE"}:
        raise NavigatorError("INVALID_REQUEST")
    try:
        before, addition, target = (Decimal(str(value)) for value in (before, addition, target))
    except InvalidOperation as error:
        raise NavigatorError("INVALID_REQUEST") from error
    if not all(value.is_finite() for value in (before, addition, target)):
        raise NavigatorError("INVALID_REQUEST")
    after = before + addition
    satisfied = after > target if comparison == "GT" else after >= target
    return {"before": str(before), "addition": str(addition), "after": str(after),
            "target": str(target), "comparison": comparison, "satisfied": satisfied}


def calculate_point_conditions(inputs, profile, *, producer, qualified_concepts, scope=None, clock_ns=monotonic_ns):
    """Require qualification before generating or projecting any candidate."""
    validate(inputs, INPUTS["condition.points_query"])
    if (inputs["rule_ref"]["profile_id"], inputs["rule_ref"]["snapshot_id"]) != (profile.profile_id, profile.snapshot_id):
        raise NavigatorError("SNAPSHOT_MISMATCH")
    state = prepare_state(inputs, profile, producer=producer)
    if inputs["aggregation_policy"] != POLICY:
        raise NavigatorError("RULES_INCOMPLETE")
    if any(p["aggregate_points"] is None for p in inputs["players"]):
        raise NavigatorError("INPUT_INCOMPLETE")
    target = {"participant_id": inputs["target_participant_id"],
              "target_points": inputs["target_points"], "comparison": inputs["comparison"]}
    allowed = inputs["allowed_result_kinds"]
    limits = scope or {"max_depth": 1, "max_candidates": 10000, "max_transitions": 4096, "max_wall_time_ms": 60000}
    rule_ref = {"profile_id": profile.profile_id, "version": "1.0.0",
                "snapshot_id": profile.snapshot_id, "content_hash": profile.snapshot_id}
    result = {"status": "KNOWLEDGE_UNQUALIFIED", "reason_code": "UNQUALIFIED_CONCEPT",
              "calculator_version": CALCULATOR_VERSION, "input_digest": _hash(inputs),
              "goal": {**target, "aggregation_policy": POLICY}, "match_rule_ref": rule_ref,
              "concept_refs": list(POINT_CONCEPT_IDS), "usage": {"depth": 0, "transitions": 0, "candidates": 0},
              "candidates": [], "partial_candidates": [], "complete_search_evidence": None}
    required = set(POINT_CONCEPT_IDS)
    if inputs.get("force_match_end"):
        required.add("game.condition.explicit_final_hand_cutoff")
        result["concept_refs"].append("game.condition.explicit_final_hand_cutoff")
    if not required <= set(qualified_concepts):
        return result
    prior = {p["participant_id"]: p["aggregate_points"] for p in inputs["players"]}
    request = {"calculation_basis": "INPUT_CONSTRAINED_COMEBACK", "ranking_scope": "AGGREGATE_POINTS",
               "target": target, "position_source": {"kind": "INLINE_STATE"}, "match_rule_ref": rule_ref,
               "input_constraints": {"evaluation_point": "MATCH_END", "allowed_result_kinds": allowed,
                                     "pre_aggregate_points": prior, "aggregation_policy": POLICY,
                                     "force_match_end": inputs.get("force_match_end", False)},
               "search_scope": {**limits, "allowed_result_kinds": allowed}}
    bundle = ConditionBundle(request, state, None, None)
    match = match_from_condition_state(state, profile)
    by_participant, by_seat = _validate_inputs(bundle, match, profile)
    budget = SearchBudget(limits, clock_ns=clock_ns)
    verified = []
    frames = ()
    try:
        budget.enter_depth()
        frames = generate_semantic_candidates(bundle)
        for frame in frames:
            for projected in _project_frame(frame, bundle, match, profile, by_participant, by_seat):
                budget.consume_transition()
                budget.consume_candidate()
                evidence = projected["evidence"]
                ending = evidence["match_end"]
                if ending["decision"] != MatchEndDecision.END.value:
                    continue
                scores = {by_participant[p]: value for p, value in evidence["scores_after"].items()}
                end = MatchEndResult(match_id=match.match_id, completed_hand_id=match.current_hand_id,
                    decision=MatchEndDecision.END, reason_code=ending["reason_code"], next_hand_authorized=False,
                    scores=scores, riichi_deposits=ending["riichi_deposits"],
                    applied_concept_ids=tuple(ending["applied_concept_ids"]))
                final = calculate_final_points(match, end, profile)
                evidence["deposit_pool_after"] = final.deposit_pool_after
                evidence["deposit_point_value"] = str(Decimal(final.deposit_pool_after) / Decimal(profile.value("match.final_points")["raw_points_unit"]))
                points = {player.participant_id: player.final_competition_points for player in final.players}
                comparisons = {p: point_comparison(prior[p], points[p], inputs["target_points"], inputs["comparison"])
                               for p in prior}
                # Only the chosen participant is judged; other totals are auditable components.
                projected["target_satisfied"] = comparisons[target["participant_id"]]["satisfied"]
                evidence["points_goal"] = comparisons[target["participant_id"]]
                evidence["aggregate_points"] = {p: {k: comparisons[p][k] for k in ("before", "addition", "after")} for p in prior}
                evidence["final_points"] = {player.participant_id: {
                    "rank": player.rank, "final_score": player.final_score,
                    "raw_points": str(player.raw_points), "oka_points": str(player.oka_points),
                    "uma_points": str(player.uma_points), "other_rank_points": str(player.other_rank_points),
                    "final_competition_points": str(player.final_competition_points),
                } for player in final.players}
                verified.append(projected)
                budget.check_time()
        candidates = _plain(classify_dominance(normalize_verified_candidates(frames, verified)))
        candidates = [c for c in candidates if c["target_satisfied"]]
        result.update(status="ACHIEVABLE" if candidates else "UNREACHABLE", reason_code=None, candidates=candidates,
                      complete_search_evidence={"search_complete": True, "candidate_set_digest": _hash(candidates),
                                                "scope": "INPUT_HAND_ENDING_MATCH_ONLY"})
    except (SearchLimitReached, CandidateError) as error:
        if isinstance(error, CandidateError) and error.code != "INDETERMINATE_LIMIT":
            raise
        partial = _plain(classify_dominance(normalize_verified_candidates(frames, verified)))
        result.update(status="INDETERMINATE_LIMIT", reason_code=error.reason,
                      partial_candidates=[c for c in partial if c["target_satisfied"]])
    result["usage"] = budget.usage()
    return result


def points_query(inputs, *, store, concepts_dir, producer):
    profile = selected_profile(inputs, store)
    return calculate_point_conditions(inputs, profile, producer=producer,
                                      qualified_concepts=qualified_concept_ids(concepts_dir))
