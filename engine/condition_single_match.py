"""E40-03 deterministic single-match condition projection.

The calculator composes the qualified scoring, settlement, transition,
match-end, and match-ranking boundaries.  It never derives a continuous point
threshold: every winning condition is a forward-settled score produced by the
scoring Engine.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from typing import Any, Iterable, Mapping

from engine.condition_candidates import (
    CandidateError,
    classify_dominance,
    generate_semantic_candidates,
    normalize_verified_candidates,
)
from engine.condition_documents import ConditionBundle, canonical_bytes
from engine.condition_limits import SearchLimitReached
from engine.final_points import FinalPointsError, rank_match_scores, calculate_final_points, calculate_provisional_points
from engine.game_state import SEATS, Seat
from engine.hand_log import HandEndReason
from engine.match_end import decide_match_end
from engine.match_state import MatchState, MatchEndDecision
from engine.next_hand_transition import calculate_next_hand_transition
from engine.rule_profiles import ResolvedRuleProfile
from engine.scoring import calculate_score
from engine.settlement import settle_hand


class SingleMatchConditionError(ValueError):
    """A structured refusal at the E40-03 composition boundary."""

    def __init__(self, code: str, pointer: str, reason: str):
        self.code = code
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"{code} at {pointer}: {reason}")


def _fail(code: str, pointer: str, reason: str) -> None:
    raise SingleMatchConditionError(code, pointer, reason)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _seat_maps(bundle: ConditionBundle) -> tuple[dict[str, Seat], dict[Seat, str]]:
    try:
        by_participant = {
            item["participant_id"]: Seat(item["seat"].lower())
            for item in bundle.state["participants"]
        }
    except (KeyError, TypeError, ValueError) as error:
        raise SingleMatchConditionError("INVALID_STATE", "/participants", "INVALID_SEAT_MAP") from error
    if len(by_participant) != 4 or set(by_participant.values()) != set(SEATS):
        _fail("INVALID_STATE", "/participants", "REQUIRES_FOUR_UNIQUE_SEATS")
    return by_participant, {seat: participant for participant, seat in by_participant.items()}


def _validate_inputs(
    bundle: ConditionBundle, match: MatchState, profile: ResolvedRuleProfile,
) -> tuple[dict[str, Seat], dict[Seat, str]]:
    request = bundle.request
    if request.get("ranking_scope") not in {"SINGLE_MATCH_RANK", "AGGREGATE_POINTS"}:
        _fail("INVALID_REQUEST", "/ranking_scope", "SINGLE_MATCH_RANK_REQUIRED")
    by_participant, by_seat = _seat_maps(bundle)
    if dict(match.seat_assignments) != by_seat:
        _fail("SNAPSHOT_MISMATCH", "/participants", "MATCH_ASSIGNMENT_MISMATCH")
    scores = {item["participant_id"]: item["value"] for item in bundle.state.get("scores", ())}
    expected_scores = {by_participant[participant]: value for participant, value in scores.items()}
    if expected_scores != dict(match.scores):
        _fail("SNAPSHOT_MISMATCH", "/scores", "MATCH_SCORE_MISMATCH")
    round_state = bundle.state.get("round_state", {})
    expected_round = {
        "round_wind": match.round_wind.value.upper(),
        "hand_number": match.hand_number,
        "dealer_participant_id": match.seat_assignments[match.dealer],
        "honba": match.honba,
        # Condition documents store deposit sticks; MatchState stores points.
        "riichi_deposits": match.riichi_deposits // 1000,
    }
    if any(round_state.get(key) != value for key, value in expected_round.items()):
        _fail("SNAPSHOT_MISMATCH", "/round_state", "MATCH_ROUND_MISMATCH")
    ref = request.get("match_rule_ref", {})
    if (ref.get("profile_id"), ref.get("snapshot_id")) != (profile.profile_id, profile.snapshot_id):
        _fail("SNAPSHOT_MISMATCH", "/match_rule_ref", "RULE_SNAPSHOT_MISMATCH")
    if (match.rule_profile_id, match.rule_snapshot_id) != (profile.profile_id, profile.snapshot_id):
        _fail("SNAPSHOT_MISMATCH", "/position_source", "MATCH_RULE_SNAPSHOT_MISMATCH")
    return by_participant, by_seat


def _score_inputs(profile: ResolvedRuleProfile, method: str, dealer: bool) -> tuple[dict[str, int], ...]:
    """Enumerate the qualified discrete fu/han result lattice.

    Twenty-fu is a tsumo-only shape and seven-pairs starts at 25 fu / two han.
    Values that collapse to the same Engine payment retain the lowest stable
    representative; settlement, not a hand-shape guess, remains authoritative.
    """

    limit = profile.value("scoring.limit_and_rounding")
    maximum_yakuman = int(limit.get("maximum_multiple_yakuman", 1))
    raw: list[dict[str, int]] = []
    for fu in (20, 25, 30, 40, 50, 60, 70, 80, 90, 100, 110):
        if fu == 20 and method != "tsumo":
            continue
        for han in range(2 if fu in {20, 25} else 1, 14):
            raw.append({"fu": fu, "han": han, "yakuman_units": 0})
    raw.extend({"fu": 0, "han": 0, "yakuman_units": units} for units in range(1, maximum_yakuman + 1))
    unique: dict[bytes, dict[str, int]] = {}
    for item in raw:
        scored = _calculate_profile_score(profile, item, method, dealer)
        identity = canonical_bytes({
            "base_payments": scored["base_payments"],
            "score_class": scored["score_class"],
        })
        unique.setdefault(identity, item)
    return tuple(sorted(unique.values(), key=lambda item: (item["yakuman_units"], item["han"], item["fu"])))


def _calculate_profile_score(
    profile: ResolvedRuleProfile, score_input: Mapping[str, int], method: str, dealer: bool,
) -> dict[str, Any]:
    limit = profile.value("scoring.limit_and_rounding")
    override = {
        **limit,
        "kiriage_mangan": [list(item) for item in limit.get("kiriage_mangan", ())],
        "ordinary_11_to_12": limit.get("ordinary_11_to_12", "sanbaiman"),
        # E40-03 applies honba exactly once in settlement.
        "honba_ron": 0,
        "honba_tsumo_per_non_winner": 0,
    }
    return calculate_score(
        **score_input, method=method, dealer=dealer,
        profile_id=str(profile.value("scoring.engine_profile")), honba_count=0,
        profile_override=override, snapshot_id_override=profile.snapshot_id,
    )


def _exhausted_match(match: MatchState) -> MatchState:
    """Create an immutable exhaustive-draw projection of the fixed hand."""

    hand = match.current_hand
    receiver = hand.players_by_seat[Seat.EAST]
    receiver = replace(receiver, concealed_hand=receiver.concealed_hand + hand.live_wall)
    players = tuple(receiver if item.seat is Seat.EAST else item for item in hand.players)
    exhausted = replace(hand, live_wall=(), players=players)
    return replace(match, current_hand=exhausted)


def _rank_evidence(
    match: MatchState, scores: Mapping[Seat, int], profile: ResolvedRuleProfile,
    by_seat: Mapping[Seat, str], target: Seat, target_rank: int, tie_policy: str,
) -> tuple[bool, dict[str, Any]]:
    ranked = rank_match_scores(match, scores, profile)
    official = {seat: rank for seat, rank, _ in ranked}
    before = {seat: rank for seat, rank, _ in rank_match_scores(match, match.scores, profile)}
    satisfied = official[target] <= target_rank
    comparison = "OFFICIAL_RANK_AT_OR_ABOVE"
    if tie_policy == "REQUIRE_STRICT_LEAD":
        # Only ties crossing the requested cutoff prevent strict attainment.
        # Ties entirely inside the qualifying range do not invalidate it.
        outside = tuple(seat for seat, _, _ in ranked[target_rank:])
        satisfied = satisfied and all(scores[target] > scores[seat] for seat in outside)
        comparison = "STRICT_GOAL_BOUNDARY"
    chaser_applied = False
    if not satisfied and tie_policy == "CHASER_ADVANTAGE":
        occupants = tuple(seat for seat, _, _ in ranked[:target_rank])
        for opponent in occupants:
            if before[target] > before[opponent] and scores[target] == scores[opponent]:
                satisfied = chaser_applied = True
                comparison = "CHASER_TIE_REACHED"
                break
    return satisfied, {
        "official_ranks": {by_seat[seat]: rank for seat, rank, _ in ranked},
        "target_rank_goal": target_rank,
        "goal_relation": "AT_OR_ABOVE",
        "target_rank_before": before[target],
        "rank_maintained": satisfied and official[target] == before[target],
        "tie_policy": tie_policy,
        "comparison": comparison,
        "chaser_advantage_applied": chaser_applied,
    }



def _project_frame(
    frame: Mapping[str, Any], bundle: ConditionBundle, match: MatchState,
    profile: ResolvedRuleProfile, by_participant: Mapping[str, Seat],
    by_seat: Mapping[Seat, str],
) -> Iterable[dict[str, Any]]:
    semantic = frame["semantic_input"]
    target = by_participant[bundle.request["target"]["participant_id"]]
    target_rank = bundle.request["target"].get("target_rank")
    tie_policy = bundle.request["target"].get("comeback_tie_policy")
    result_kind = frame["result_kind"]
    projected_match = match
    if result_kind == "DRAW":
        projected_match = _exhausted_match(match)
        tenpai = tuple(by_participant[item] for item in semantic["declared_tenpai_participant_ids"])
        settled = settle_hand(
            projected_match.current_hand, profile,
            end_reason=HandEndReason.EXHAUSTIVE_DRAW, declared_tenpai=tenpai,
        )
        variants = ((None, settled, tenpai),)
    else:
        winner = by_participant[semantic["winner_participant_id"]]
        method = semantic["method"]
        discarder = by_participant[semantic["payer_participant_ids"][0]] if method == "ron" else None
        variants = []
        for score_input in _score_inputs(profile, method, winner is match.dealer):
            score = _calculate_profile_score(
                profile, score_input, method, winner is match.dealer,
            )
            settled = settle_hand(
                match.current_hand, profile,
                end_reason=HandEndReason.RON if method == "ron" else HandEndReason.TSUMO,
                winner=winner, method=method, score=score, discarder=discarder,
            )
            variants.append((score, settled, None))
    bounds = bundle.request.get("input_constraints", {}).get("score_bounds")
    for score, settled, tenpai in variants:
        if bounds is not None and score is not None:
            receipt = score["winner_base_receipt"]
            if receipt < bounds["minimum"] or receipt > bounds["maximum"]:
                continue
        transition = calculate_next_hand_transition(
            projected_match, settled["hand_result"], profile,
            riichi_deposits_after=settled["riichi_deposit_pool_after"],
            declared_tenpai=tenpai,
        )
        match_end = decide_match_end(projected_match, transition, profile)
        if bundle.request.get("input_constraints", {}).get("force_match_end"):
            match_end = replace(match_end, decision=MatchEndDecision.END, next_hand_authorized=False,
                reason_code="EXPLICIT_FINAL_HAND_CUTOFF",
                applied_concept_ids=tuple(dict.fromkeys((*match_end.applied_concept_ids, "game.condition.explicit_final_hand_cutoff"))))
        if bundle.request["ranking_scope"] == "AGGREGATE_POINTS":
            satisfied = False  # The point-goal boundary evaluates final points after match end.
            rank_evidence = {"official_ranks": {
                by_seat[seat]: rank for seat, rank, _ in rank_match_scores(projected_match, transition.scores, profile)
            }}
        else:
            satisfied, rank_evidence = _rank_evidence(
                projected_match, transition.scores, profile, by_seat, target,
                target_rank, tie_policy,
            )
        constraints = bundle.request.get("input_constraints", {})
        if constraints.get("evaluation_point") == "MATCH_END":
            satisfied = satisfied and match_end.decision == MatchEndDecision.END
        target_delta = settled["hand_result"].point_deltas[target]
        opponent_loss = sum(
            max(0, -settled["hand_result"].point_deltas[seat])
            for seat in SEATS if seat is not target
        )
        winning_score = 0 if score is None else score["winner_base_receipt"]
        evidence = {
            "deposit_pool_after": match_end.riichi_deposits,
            "score_input": None if score is None else score["input"],
            "score_class": None if score is None else score["score_class"],
            "is_mangan": score is not None and score["score_class"] == "mangan",
            "settlement_components": settled["components"],
            "scores_before": {by_seat[seat]: match.scores[seat] for seat in SEATS},
            "scores_after": {by_seat[seat]: transition.scores[seat] for seat in SEATS},
            "rank": rank_evidence,
            "transition": {
                "next_dealer": by_seat[transition.next_dealer],
                "next_round_wind": transition.next_round_wind.value.upper(),
                "next_hand_number": transition.next_hand_number,
                "next_honba": transition.next_honba,
            },
            "match_end": {
                "decision": match_end.decision.value,
                "reason_code": match_end.reason_code,
                "riichi_deposits": match_end.riichi_deposits,
                "applied_concept_ids": match_end.applied_concept_ids,
            },
        }
        if bundle.request["ranking_scope"] == "SINGLE_MATCH_RANK" and match_end.decision == MatchEndDecision.END:
            final = calculate_final_points(match, match_end, profile)
            evidence["deposit_pool_after"] = final.deposit_pool_after
            evidence["final_points"] = {player.participant_id: {
                "rank": player.rank, "final_score": player.final_score,
                "raw_points": str(player.raw_points), "oka_points": str(player.oka_points),
                "uma_points": str(player.uma_points), "other_rank_points": str(player.other_rank_points),
                "final_competition_points": str(player.final_competition_points),
            } for player in final.players}
        if bundle.request["ranking_scope"] == "SINGLE_MATCH_RANK" and match_end.decision != MatchEndDecision.END:
            try:
                provisional = calculate_provisional_points(match, match_end, profile)
            except FinalPointsError as error:
                # A rank projection can be valid even when its profile/score precision
                # is outside the optional point-breakdown capability.
                if error.code not in {"UNSUPPORTED_SCORE_PRECISION", "UNSUPPORTED_FINAL_POINTS_RULE"}:
                    raise
                evidence["provisional_points_unavailable"] = error.code
            else:
                evidence["provisional_points"] = {player.participant_id: {
                    "final_competition_points": str(player.final_competition_points),
                } for player in provisional}
        evidence["deposit_point_value"] = str(Decimal(evidence["deposit_pool_after"]) / Decimal(profile.value("match.final_points")["raw_points_unit"]))
        yield {
            "frame_id": frame["candidate_id"],
            "forward_verified": True,
            "target_satisfied": satisfied,
            "quantity_axes": {
                "required_realizable_winning_score": winning_score,
                "required_target_score_gain_after_honba_and_deposit": max(0, target_delta),
                "required_opponent_score_loss_after_honba": opponent_loss,
            },
            "engine_result_refs": [
                "score:" + (_hash(score).split(":", 1)[1] if score is not None else "none"),
                "settlement:" + _hash(settled["components"]).split(":", 1)[1],
                "transition:" + _hash(evidence["transition"]).split(":", 1)[1],
                "match-end:" + _hash(evidence["match_end"]).split(":", 1)[1],
            ],
            "evidence": evidence,
        }


def calculate_single_match_conditions(
    bundle: ConditionBundle, match: MatchState, profile: ResolvedRuleProfile,
    *, budget=None,
) -> tuple[Mapping[str, Any], ...]:
    """Return stable, forward-verified E40-03 candidates for one fixed hand."""

    by_participant, by_seat = _validate_inputs(bundle, match, profile)
    frames = generate_semantic_candidates(bundle)
    verified: list[dict[str, Any]] = []
    for frame in frames:
        try:
            for item in _project_frame(
                frame, bundle, match, profile, by_participant, by_seat,
            ):
                if budget is not None:
                    budget.consume_transition()
                    budget.consume_candidate()
                verified.append(item)
                if len(verified) > bundle.request["search_scope"]["max_candidates"]:
                    raise CandidateError(
                        "INDETERMINATE_LIMIT", "/search_scope/max_candidates",
                        "VERIFIED_CANDIDATE_LIMIT_REACHED",
                    )
        except SearchLimitReached as error:
            partial = classify_dominance(normalize_verified_candidates(frames, verified))
            raise SearchLimitReached(error.reason, error.usage, partial) from error
    normalized = normalize_verified_candidates(frames, verified)
    return classify_dominance(normalized)
