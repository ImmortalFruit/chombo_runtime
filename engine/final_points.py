"""Deterministic final-point calculation for an ended four-player match."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Mapping

from engine.game_state import SEATS, Seat
from engine.match_state import MatchEndDecision, MatchEndResult, MatchState
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile, RuleProfileError


class FinalPointsError(ValueError):
    """A structured refusal to infer missing or inconsistent final-point data."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class PlayerFinalPoints:
    """One participant's auditable final-point components."""

    participant_id: str
    seat: Seat
    rank: int
    tied_participant_ids: tuple[str, ...]
    score_before_deposit: int
    deposit_award_points: int
    final_score: int
    raw_points: float
    oka_points: float
    uma_points: float
    other_rank_points: float
    value_before_rounding: float
    rounding_difference: float
    final_competition_points: float


@dataclass(frozen=True)
class FinalPointsResult:
    """Final scores, competition points, and table-wide conservation evidence."""

    match_id: str
    completed_hand_id: str
    rule_profile_id: str
    rule_snapshot_id: str
    players: tuple[PlayerFinalPoints, ...]
    remaining_deposit_points_before: int
    deposit_resolution: str
    deposit_awards: Mapping[Seat, int]
    deposit_pool_after: int
    unallocated_table_balance: int
    component_sums: Mapping[str, float]
    final_points_sum: float
    validation_results: tuple[str, ...]
    applied_concept_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deposit_awards", MappingProxyType(dict(self.deposit_awards)))
        object.__setattr__(self, "component_sums", MappingProxyType(dict(self.component_sums)))


_DIMENSIONS = ("match.start.points", "match.end.final_deposit", "match.final_points")


def _profile_value(profile: ResolvedRuleProfile, dimension_id: str) -> Mapping[str, Any]:
    try:
        value = profile.value(dimension_id)
    except RuleProfileError as error:
        raise FinalPointsError(
            "UNRESOLVED_FINAL_POINTS_RULE",
            {"dimension_id": dimension_id, "profile_id": profile.profile_id},
        ) from error
    if not isinstance(value, Mapping):
        raise FinalPointsError(
            "UNSUPPORTED_FINAL_POINTS_RULE",
            {"dimension_id": dimension_id, "value": value},
        )
    return value


def _require(rule: Mapping[str, Any], key: str, expected: Any, dimension_id: str) -> None:
    if rule.get(key) != expected:
        raise FinalPointsError(
            "UNSUPPORTED_FINAL_POINTS_RULE",
            {"dimension_id": dimension_id, "key": key, "value": rule.get(key)},
        )


def _priority_seats(match: MatchState) -> tuple[Seat, ...]:
    """Return current winds ordered by proximity to the original dealer."""

    rotations = (0 if match.round_wind is Seat.EAST else 4) + match.hand_number - 1

    def original_index(current_seat: Seat) -> int:
        return (SEATS.index(current_seat) + rotations) % len(SEATS)

    initial_index = SEATS.index(match.initial_dealer)
    return tuple(
        sorted(
            SEATS,
            key=lambda seat: (original_index(seat) - initial_index) % len(SEATS),
        )
    )


def rank_match_scores(
    match: MatchState,
    scores: Mapping[Seat, int],
    profile: ResolvedRuleProfile,
) -> tuple[tuple[Seat, int, tuple[Seat, ...]], ...]:
    """Rank one match score table with the profile's qualified tie rule.

    This is the reusable ranking boundary shared by final-point calculation and
    v0.4 condition projections.  It deliberately does not allocate deposits or
    add oka/uma; callers must pass the exact score table for their evaluation
    point.
    """

    require_profile_match(profile, match, error_type=FinalPointsError)
    if set(scores) != set(SEATS) or any(type(value) is not int for value in scores.values()):
        raise FinalPointsError("INVALID_RANK_SCORE_TABLE", {})
    points_rule = _profile_value(profile, "match.final_points")
    priority = _priority_seats(match)
    ordered = tuple(sorted(SEATS, key=lambda seat: (-scores[seat], priority.index(seat))))
    tie_rule = points_rule.get("tie_rule")
    rank_by_seat: dict[Seat, int] = {}
    tied_by_seat: dict[Seat, tuple[Seat, ...]] = {}
    if tie_rule == "east_1_seat_order":
        for index, seat in enumerate(ordered):
            rank_by_seat[seat] = index + 1
            tied_by_seat[seat] = (seat,)
    elif tie_rule == "average_occupied_rank_points":
        for score in sorted(set(scores.values()), reverse=True):
            group = tuple(seat for seat in priority if scores[seat] == score)
            rank = sum(scores[item] > score for item in SEATS) + 1
            for seat in group:
                rank_by_seat[seat] = rank
                tied_by_seat[seat] = group
    else:
        raise FinalPointsError(
            "UNSUPPORTED_FINAL_POINTS_RULE",
            {"dimension_id": "match.final_points", "tie_rule": tie_rule},
        )
    return tuple((seat, rank_by_seat[seat], tied_by_seat[seat]) for seat in ordered)


def _split_units(total: int, seats: tuple[Seat, ...], priority: tuple[Seat, ...]) -> dict[Seat, int]:
    quotient, remainder = divmod(total, len(seats))
    ordered = tuple(seat for seat in priority if seat in seats)
    return {
        seat: quotient + (1 if ordered.index(seat) < remainder else 0)
        for seat in seats
    }


def _validate_inputs(
    match: MatchState,
    match_end: MatchEndResult,
    profile: ResolvedRuleProfile,
) -> None:
    require_profile_match(profile, match, error_type=FinalPointsError)
    if (
        match_end.match_id != match.match_id
        or match_end.completed_hand_id != match.current_hand_id
    ):
        raise FinalPointsError("MATCH_END_RESULT_MISMATCH", {})
    if match_end.decision is not MatchEndDecision.END or match_end.next_hand_authorized:
        raise FinalPointsError(
            "MATCH_NOT_ENDED", {"decision": match_end.decision.value}
        )


def _generic_final_points(
    match: MatchState,
    match_end: MatchEndResult,
    profile: ResolvedRuleProfile,
    start_rule: Mapping[str, Any],
    deposit_rule: Mapping[str, Any],
    points_rule: Mapping[str, Any],
) -> FinalPointsResult:
    starting_points = start_rule.get("points_per_player")
    return_points = points_rule.get("return_points")
    raw_unit = points_rule.get("raw_points_unit")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (starting_points, return_points, raw_unit)) or raw_unit <= 0:
        raise FinalPointsError("UNSUPPORTED_FINAL_POINTS_RULE", {"dimension_id": "match.final_points"})
    deposits = match_end.riichi_deposits
    if sum(match_end.scores.values()) + deposits != starting_points * len(SEATS):
        raise FinalPointsError("TABLE_BALANCE_MISMATCH", {})
    priority = _priority_seats(match)
    top_score = max(match_end.scores.values())
    top_seats = tuple(seat for seat in priority if match_end.scores[seat] == top_score)
    mode = deposit_rule.get("resolution_mode")
    deposit_awards = {seat: 0 for seat in SEATS}
    deposit_pool_after = 0
    if mode == "FIRST_PLACE_TAKES_ALL":
        deposit_awards[top_seats[0]] = deposits
    elif mode == "LEAVE_ON_TABLE":
        deposit_pool_after = deposits
    else:
        raise FinalPointsError("UNSUPPORTED_FINAL_POINTS_RULE", {"dimension_id": "match.end.final_deposit"})
    final_scores = {seat: match_end.scores[seat] + deposit_awards[seat] for seat in SEATS}

    ranked = rank_match_scores(match, final_scores, profile)
    ordered = tuple(item[0] for item in ranked)
    rank_by_seat = {seat: rank for seat, rank, _ in ranked}
    tied_by_seat = {seat: tied for seat, _, tied in ranked}

    rank_values = points_rule.get("rank_points")
    if rank_values is None:
        table = points_rule.get("rank_points_by_positive_player_count")
        rank_values = table.get(str(sum(score >= return_points for score in final_scores.values()))) if isinstance(table, Mapping) else None
    if not isinstance(rank_values, (list, tuple)) or len(rank_values) != 4 or not all(isinstance(value, (int, float)) for value in rank_values):
        raise FinalPointsError("UNSUPPORTED_FINAL_POINTS_RULE", {"dimension_id": "match.final_points"})

    oka_total = points_rule.get("oka_included_in_first_place_points", 0)
    if not isinstance(oka_total, (int, float)) or isinstance(oka_total, bool) or oka_total < 0:
        raise FinalPointsError(
            "UNSUPPORTED_FINAL_POINTS_RULE",
            {"dimension_id": "match.final_points", "oka_included_in_first_place_points": oka_total},
        )
    rank_value_sum = round(sum(float(value) for value in rank_values), 10)
    if rank_value_sum not in {0.0, float(oka_total)}:
        raise FinalPointsError(
            "UNSUPPORTED_FINAL_POINTS_RULE",
            {"dimension_id": "match.final_points", "rank_points_sum": rank_value_sum,
             "oka_included_in_first_place_points": oka_total},
        )
    rank_values_include_oka = oka_total > 0 and rank_value_sum == float(oka_total)

    # Preserve the declared decimal values exactly through component arithmetic.
    # Convert once at the existing float API boundary, rather than adding floats.
    rank_points: dict[Seat, Fraction] = {}
    for seat in SEATS:
        rank = rank_by_seat[seat]
        group = tied_by_seat[seat]
        occupied = range(rank - 1, rank - 1 + len(group))
        rank_points[seat] = sum((Fraction(str(rank_values[index])) for index in occupied), Fraction(0)) / len(group)
    oka_points = {seat: Fraction(0) for seat in SEATS}
    oka_points[ordered[0]] = Fraction(str(oka_total))
    other_rank_points = dict(rank_points)
    if rank_values_include_oka:
        other_rank_points[ordered[0]] -= Fraction(str(oka_total))
    raw_values = {seat: Fraction(final_scores[seat] - return_points, raw_unit) for seat in SEATS}
    final_values = {seat: raw_values[seat] + oka_points[seat] + other_rank_points[seat] for seat in SEATS}
    players = tuple(
        PlayerFinalPoints(
            participant_id=match.seat_assignments[seat], seat=seat,
            rank=rank_by_seat[seat],
            tied_participant_ids=tuple(match.seat_assignments[item] for item in tied_by_seat[seat]),
            score_before_deposit=match_end.scores[seat],
            deposit_award_points=deposit_awards[seat], final_score=final_scores[seat],
            raw_points=float(raw_values[seat]),
            oka_points=float(oka_points[seat]), uma_points=0.0,
            other_rank_points=float(other_rank_points[seat]),
            value_before_rounding=float(final_values[seat]),
            rounding_difference=0.0,
            final_competition_points=float(final_values[seat]),
        )
        for seat in SEATS
    )
    component_sums = {
        "raw_points": round(sum(player.raw_points for player in players), 10),
        "oka_points": round(sum(player.oka_points for player in players), 10),
        "uma_points": 0.0,
        "other_rank_points": round(sum(player.other_rank_points for player in players), 10),
        "rounding_difference": 0.0,
    }
    concepts = []
    for dimension_id in _DIMENSIONS:
        for concept_id in profile.dimensions[dimension_id]["concept_ids"]:
            if concept_id not in concepts:
                concepts.append(concept_id)
    return FinalPointsResult(
        match_id=match.match_id, completed_hand_id=match.current_hand_id,
        rule_profile_id=profile.profile_id, rule_snapshot_id=profile.snapshot_id,
        players=players, remaining_deposit_points_before=deposits,
        deposit_resolution=mode, deposit_awards=deposit_awards,
        deposit_pool_after=deposit_pool_after, unallocated_table_balance=deposit_pool_after,
        component_sums=component_sums,
        final_points_sum=round(sum(player.final_competition_points for player in players), 10),
        validation_results=("MATCH_ENDED", "RULE_SNAPSHOT_MATCHED", "DEPOSIT_RESOLUTION_MATCHED_PROFILE", "TABLE_BALANCE_CONSERVED", "RANK_COMPONENTS_MATCHED_PROFILE"),
        applied_concept_ids=tuple(concepts),
    )


def _calculate_points(
    match: MatchState,
    match_end: MatchEndResult,
    profile: ResolvedRuleProfile,
    *, provisional: bool = False,
) -> FinalPointsResult:
    """Resolve final deposits and produce the profile-defined point breakdown."""

    require_profile_match(profile, match, error_type=FinalPointsError)
    start_rule = _profile_value(profile, "match.start.points")
    deposit_rule = _profile_value(profile, "match.end.final_deposit")
    points_rule = _profile_value(profile, "match.final_points")

    if deposit_rule.get("resolution_mode") != "AWARD" or "official_rank_points" not in points_rule:
        result = _generic_final_points(match, match_end, profile, start_rule,
            {"resolution_mode": "LEAVE_ON_TABLE"} if provisional else deposit_rule, points_rule)
        return replace(result, validation_results=("PROVISIONAL_POINTS",)) if provisional else result

    starting_points = start_rule.get("points_per_player")
    return_points = points_rule.get("return_points")
    raw_unit = points_rule.get("raw_points_unit")
    precision = points_rule.get("precision_points")
    if (
        not isinstance(starting_points, int)
        or not isinstance(return_points, int)
        or not isinstance(raw_unit, int)
        or raw_unit <= 0
        or precision != 0.1
    ):
        raise FinalPointsError("UNSUPPORTED_FINAL_POINTS_RULE", {"dimension_id": "match.final_points"})

    _require(deposit_rule, "resolution_mode", "AWARD", "match.end.final_deposit")
    _require(deposit_rule, "single_top", "transfer_entire_pool_to_top_player", "match.end.final_deposit")
    _require(deposit_rule, "tied_top", "split_pool_among_tied_top_players", "match.end.final_deposit")
    _require(deposit_rule, "three_way_remainder_priority", "closer_to_starting_dealer", "match.end.final_deposit")
    _require(points_rule, "tie_rule", "average_occupied_rank_points", "match.final_points")
    _require(points_rule, "tied_first_oka", "split_equally_to_0.1", "match.final_points")
    _require(points_rule, "remainder_priority", "closer_to_starting_dealer", "match.final_points")
    _require(points_rule, "final_deposit", "profile_defined_top_resolution", "match.final_points")

    official = points_rule.get("official_rank_points")
    oka = points_rule.get("oka")
    uma = points_rule.get("uma")
    if not all(
        isinstance(values, (list, tuple))
        and len(values) == len(SEATS)
        and all(isinstance(value, (int, float)) for value in values)
        for values in (official, oka, uma)
    ):
        raise FinalPointsError("UNSUPPORTED_FINAL_POINTS_RULE", {"dimension_id": "match.final_points"})
    official_tenths = tuple(round(float(value) * 10) for value in official)
    oka_tenths = tuple(round(float(value) * 10) for value in oka)
    uma_tenths = tuple(round(float(value) * 10) for value in uma)
    if any(oka_tenths[index] + uma_tenths[index] != official_tenths[index] for index in range(4)):
        raise FinalPointsError("INCONSISTENT_RANK_POINT_COMPONENTS", {})

    precision_score_unit = round(raw_unit * precision)
    if precision_score_unit <= 0 or any(score % precision_score_unit for score in match_end.scores.values()):
        raise FinalPointsError("UNSUPPORTED_SCORE_PRECISION", {})
    deposits = match_end.riichi_deposits
    if deposits % precision_score_unit:
        raise FinalPointsError("UNSUPPORTED_DEPOSIT_PRECISION", {"value": deposits})

    expected_table_total = starting_points * len(SEATS)
    if sum(match_end.scores.values()) + deposits != expected_table_total:
        raise FinalPointsError(
            "TABLE_BALANCE_MISMATCH",
            {
                "player_scores": sum(match_end.scores.values()),
                "deposit_pool": deposits,
                "expected": expected_table_total,
            },
        )

    priority = _priority_seats(match)
    top_score = max(match_end.scores.values())
    top_seats = tuple(seat for seat in priority if match_end.scores[seat] == top_score)
    deposit_units = {} if provisional else _split_units(deposits // precision_score_unit, top_seats, priority)
    deposit_awards = {
        seat: deposit_units.get(seat, 0) * precision_score_unit for seat in SEATS
    }
    final_scores = {
        seat: match_end.scores[seat] + deposit_awards[seat] for seat in SEATS
    }
    retained_deposits = deposits if provisional else 0
    if sum(final_scores.values()) + retained_deposits != expected_table_total:
        raise FinalPointsError("TABLE_BALANCE_MISMATCH", {"stage": "after_deposit"})

    ranked_groups: list[tuple[int, tuple[Seat, ...]]] = []
    occupied = 0
    for score in sorted(set(final_scores.values()), reverse=True):
        group = tuple(seat for seat in priority if final_scores[seat] == score)
        ranked_groups.append((occupied + 1, group))
        occupied += len(group)

    player_values: dict[Seat, tuple[int, tuple[Seat, ...], int, int]] = {}
    for rank, group in ranked_groups:
        indexes = range(rank - 1, rank - 1 + len(group))
        group_oka = sum(oka_tenths[index] for index in indexes)
        group_uma = sum(uma_tenths[index] for index in indexes)
        oka_alloc = _split_units(group_oka, group, priority)
        uma_alloc = _split_units(group_uma, group, priority)
        for seat in group:
            player_values[seat] = (rank, group, oka_alloc[seat], uma_alloc[seat])

    players: list[PlayerFinalPoints] = []
    for seat in SEATS:
        rank, tied_seats, player_oka, player_uma = player_values[seat]
        raw_tenths = (final_scores[seat] - return_points) * 10
        if raw_tenths % raw_unit:
            raise FinalPointsError("UNSUPPORTED_SCORE_PRECISION", {"seat": seat.value})
        raw_tenths //= raw_unit
        before_tenths = raw_tenths + player_oka + player_uma
        participant_id = match.seat_assignments[seat]
        players.append(
            PlayerFinalPoints(
                participant_id=participant_id,
                seat=seat,
                rank=rank,
                tied_participant_ids=tuple(match.seat_assignments[item] for item in tied_seats),
                score_before_deposit=match_end.scores[seat],
                deposit_award_points=deposit_awards[seat],
                final_score=final_scores[seat],
                raw_points=raw_tenths / 10,
                oka_points=player_oka / 10,
                uma_points=player_uma / 10,
                other_rank_points=0.0,
                value_before_rounding=before_tenths / 10,
                rounding_difference=0.0,
                final_competition_points=before_tenths / 10,
            )
        )

    component_sums = {
        "raw_points": round(sum(player.raw_points for player in players), 1),
        "oka_points": round(sum(player.oka_points for player in players), 1),
        "uma_points": round(sum(player.uma_points for player in players), 1),
        "other_rank_points": 0.0,
        "rounding_difference": 0.0,
    }
    final_points_sum = round(sum(player.final_competition_points for player in players), 1)
    expected_final_sum = round((expected_table_total - retained_deposits - return_points * len(SEATS)) / raw_unit + sum(official), 1)
    if final_points_sum != expected_final_sum:
        raise FinalPointsError(
            "FINAL_POINTS_SUM_MISMATCH",
            {"actual": final_points_sum, "expected": expected_final_sum},
        )

    concept_ids: list[str] = []
    for dimension_id in _DIMENSIONS:
        for concept_id in profile.dimensions[dimension_id]["concept_ids"]:
            if concept_id not in concept_ids:
                concept_ids.append(concept_id)
    for concept_id in (
        "game.match.points.input",
        "game.match.points.breakdown",
        "game.match.final_points.input",
    ):
        if concept_id not in concept_ids:
            concept_ids.append(concept_id)

    return FinalPointsResult(
        match_id=match.match_id,
        completed_hand_id=match.current_hand_id,
        rule_profile_id=profile.profile_id,
        rule_snapshot_id=profile.snapshot_id,
        players=tuple(players),
        remaining_deposit_points_before=deposits,
        deposit_resolution="PROVISIONAL_RETAIN" if provisional else "AWARD",
        deposit_awards=deposit_awards,
        deposit_pool_after=retained_deposits,
        unallocated_table_balance=retained_deposits,
        component_sums=component_sums,
        final_points_sum=final_points_sum,
        validation_results=(
            "PROVISIONAL_POINTS" if provisional else "MATCH_ENDED",
            "RULE_SNAPSHOT_MATCHED",
            "DEPOSIT_RESOLUTION_MATCHED_PROFILE",
            "TABLE_BALANCE_CONSERVED",
            "RANK_COMPONENTS_MATCHED_PROFILE",
            "ROUNDING_MATCHED_PROFILE",
            "FINAL_POINTS_SUM_MATCHED_PROFILE",
        ),
        applied_concept_ids=tuple(concept_ids),
    )


def calculate_final_points(match: MatchState, match_end: MatchEndResult, profile: ResolvedRuleProfile) -> FinalPointsResult:
    """Resolve final points only after an actual qualified match end."""
    _validate_inputs(match, match_end, profile)
    return _calculate_points(match, match_end, profile)


def calculate_provisional_points(match: MatchState, position: MatchEndResult, profile: ResolvedRuleProfile) -> tuple[PlayerFinalPoints, ...]:
    """Apply current score/rank components without ending the match or awarding deposits."""
    if position.match_id != match.match_id or position.completed_hand_id != match.current_hand_id:
        raise FinalPointsError("MATCH_END_RESULT_MISMATCH", {})
    return _calculate_points(match, position, profile, provisional=True).players
