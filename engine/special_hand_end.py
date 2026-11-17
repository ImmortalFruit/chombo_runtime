"""Immutable v0.3.1 hand-end candidates and settled-result contracts.

This module owns classification, structural validation, and conflict resolution.
Reason-specific detection and settlement remain separate Engine boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from engine.game_state import SEATS, Seat
from engine.rule_profiles import ResolvedRuleProfile, require_profile_match


class SpecialHandEndError(ValueError):
    """A structured refusal to classify or settle a hand-end candidate."""

    def __init__(self, code: str, details: Mapping[str, Any] = {}):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class ResultFamily(str, Enum):
    ORDINARY_WIN = "ORDINARY_WIN"
    EXHAUSTIVE_DRAW = "EXHAUSTIVE_DRAW"
    NAGASHI_MANGAN = "NAGASHI_MANGAN"
    ABORTIVE_DRAW = "ABORTIVE_DRAW"


class ReasonCode(str, Enum):
    RON = "RON"
    TSUMO = "TSUMO"
    WALL_EXHAUSTED = "WALL_EXHAUSTED"
    NAGASHI_MANGAN = "NAGASHI_MANGAN"
    NINE_TERMINALS = "NINE_TERMINALS"
    FOUR_WINDS_FIRST_DISCARD = "FOUR_WINDS_FIRST_DISCARD"
    FOUR_RIICHI = "FOUR_RIICHI"
    FOUR_KANS = "FOUR_KANS"
    THREE_RON = "THREE_RON"


class EvaluationTiming(str, Enum):
    RESPONSE_RESOLUTION = "RESPONSE_RESOLUTION"
    DRAW_ACTION = "DRAW_ACTION"
    WALL_EXHAUSTED = "WALL_EXHAUSTED"


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_REASON_FAMILY = {
    ReasonCode.RON: ResultFamily.ORDINARY_WIN,
    ReasonCode.TSUMO: ResultFamily.ORDINARY_WIN,
    ReasonCode.WALL_EXHAUSTED: ResultFamily.EXHAUSTIVE_DRAW,
    ReasonCode.NAGASHI_MANGAN: ResultFamily.NAGASHI_MANGAN,
    ReasonCode.NINE_TERMINALS: ResultFamily.ABORTIVE_DRAW,
    ReasonCode.FOUR_WINDS_FIRST_DISCARD: ResultFamily.ABORTIVE_DRAW,
    ReasonCode.FOUR_RIICHI: ResultFamily.ABORTIVE_DRAW,
    ReasonCode.FOUR_KANS: ResultFamily.ABORTIVE_DRAW,
    ReasonCode.THREE_RON: ResultFamily.ABORTIVE_DRAW,
}
_REASON_TIMING = {
    ReasonCode.RON: EvaluationTiming.RESPONSE_RESOLUTION,
    ReasonCode.TSUMO: EvaluationTiming.DRAW_ACTION,
    ReasonCode.WALL_EXHAUSTED: EvaluationTiming.WALL_EXHAUSTED,
    ReasonCode.NAGASHI_MANGAN: EvaluationTiming.WALL_EXHAUSTED,
    ReasonCode.NINE_TERMINALS: EvaluationTiming.DRAW_ACTION,
    ReasonCode.FOUR_WINDS_FIRST_DISCARD: EvaluationTiming.RESPONSE_RESOLUTION,
    ReasonCode.FOUR_RIICHI: EvaluationTiming.RESPONSE_RESOLUTION,
    ReasonCode.FOUR_KANS: EvaluationTiming.RESPONSE_RESOLUTION,
    ReasonCode.THREE_RON: EvaluationTiming.RESPONSE_RESOLUTION,
}


def _seats(value: Sequence[Seat], *, field_name: str) -> tuple[Seat, ...]:
    result = tuple(value)
    if any(not isinstance(seat, Seat) for seat in result):
        raise SpecialHandEndError("INVALID_SEAT_REFERENCE", {"field": field_name})
    if len(result) != len(set(result)):
        raise SpecialHandEndError("DUPLICATE_SEAT_REFERENCE", {"field": field_name})
    return result


def _concepts(value: Sequence[str]) -> tuple[str, ...]:
    result = tuple(value)
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise SpecialHandEndError("INVALID_CONCEPT_REFERENCE")
    if len(result) != len(set(result)):
        raise SpecialHandEndError("DUPLICATE_CONCEPT_REFERENCE")
    return result


def _profile_identity(profile_id: str, snapshot_id: str) -> None:
    if not profile_id or not _HASH.fullmatch(snapshot_id):
        raise SpecialHandEndError("INVALID_RULE_SNAPSHOT")


@dataclass(frozen=True)
class HandEndTrigger:
    """The causal identity needed to revalidate one candidate."""

    event_id: str
    evaluation_timing: EvaluationTiming
    declarer: Seat | None = None
    related_seats: tuple[Seat, ...] = ()
    related_tile_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.event_id or not isinstance(self.evaluation_timing, EvaluationTiming):
            raise SpecialHandEndError("INVALID_HAND_END_TRIGGER")
        if self.declarer is not None and not isinstance(self.declarer, Seat):
            raise SpecialHandEndError("INVALID_SEAT_REFERENCE", {"field": "declarer"})
        related_seats = _seats(self.related_seats, field_name="related_seats")
        tile_ids = tuple(self.related_tile_ids)
        if any(not isinstance(tile_id, str) or not tile_id for tile_id in tile_ids):
            raise SpecialHandEndError("INVALID_TILE_REFERENCE")
        if len(tile_ids) != len(set(tile_ids)):
            raise SpecialHandEndError("DUPLICATE_TILE_REFERENCE")
        object.__setattr__(self, "related_seats", related_seats)
        object.__setattr__(self, "related_tile_ids", tile_ids)


@dataclass(frozen=True)
class HandEndCandidate:
    """A detected but not yet settled semantic hand end."""

    result_family: ResultFamily
    reason_code: ReasonCode
    ordinary_winner_seats: tuple[Seat, ...]
    qualifier_seats: tuple[Seat, ...]
    declared_tenpai_seats: tuple[Seat, ...]
    trigger: HandEndTrigger
    rule_profile_id: str
    rule_snapshot_id: str
    applied_concept_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result_family, ResultFamily) or not isinstance(
            self.reason_code, ReasonCode
        ):
            raise SpecialHandEndError("UNQUALIFIED_SPECIAL_END_REASON")
        if _REASON_FAMILY[self.reason_code] is not self.result_family:
            raise SpecialHandEndError("HAND_END_FAMILY_REASON_MISMATCH")
        if _REASON_TIMING[self.reason_code] is not self.trigger.evaluation_timing:
            raise SpecialHandEndError("HAND_END_TRIGGER_MISMATCH")
        winners = _seats(self.ordinary_winner_seats, field_name="ordinary_winner_seats")
        qualifiers = _seats(self.qualifier_seats, field_name="qualifier_seats")
        tenpai = _seats(self.declared_tenpai_seats, field_name="declared_tenpai_seats")
        occupied = [*winners, *qualifiers, *tenpai]
        if len(occupied) != len(set(occupied)):
            raise SpecialHandEndError("HAND_END_SEAT_ROLE_OVERLAP")
        if self.result_family is ResultFamily.ORDINARY_WIN:
            valid_roles = bool(winners) and not qualifiers and not tenpai
        elif self.result_family is ResultFamily.EXHAUSTIVE_DRAW:
            valid_roles = not winners and not qualifiers
        elif self.result_family is ResultFamily.NAGASHI_MANGAN:
            valid_roles = not winners and bool(qualifiers) and not tenpai
        else:
            valid_roles = not winners and not qualifiers and not tenpai
        if not valid_roles:
            raise SpecialHandEndError(
                "INVALID_HAND_END_SEAT_ROLES", {"result_family": self.result_family.value}
            )
        _profile_identity(self.rule_profile_id, self.rule_snapshot_id)
        object.__setattr__(self, "ordinary_winner_seats", winners)
        object.__setattr__(self, "qualifier_seats", qualifiers)
        object.__setattr__(self, "declared_tenpai_seats", tenpai)
        object.__setattr__(self, "applied_concept_ids", _concepts(self.applied_concept_ids))


@dataclass(frozen=True)
class TransitionDirective:
    """Already-resolved transition values; consumers must not infer from family."""

    dealer_continues: bool
    advance_hand: bool
    honba_after: int
    riichi_deposit_pool_after: int

    def __post_init__(self) -> None:
        if type(self.dealer_continues) is not bool or type(self.advance_hand) is not bool:
            raise SpecialHandEndError("INVALID_TRANSITION_DIRECTIVE")
        if self.dealer_continues is self.advance_hand:
            raise SpecialHandEndError("INCONSISTENT_TRANSITION_DIRECTIVE")
        if type(self.honba_after) is not int or self.honba_after < 0:
            raise SpecialHandEndError("INVALID_TRANSITION_DIRECTIVE")
        if type(self.riichi_deposit_pool_after) is not int or self.riichi_deposit_pool_after < 0:
            raise SpecialHandEndError("INVALID_TRANSITION_DIRECTIVE")


@dataclass(frozen=True)
class SettledHandEndResult:
    """A v0.3.1 semantic result after exactly one family-specific settlement."""

    candidate: HandEndCandidate
    point_deltas: Mapping[Seat, int]
    final_scores: Mapping[Seat, int]
    riichi_deposit_pool_after: int
    transition_directive: TransitionDirective
    applied_concept_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if set(self.point_deltas) != set(SEATS) or set(self.final_scores) != set(SEATS):
            raise SpecialHandEndError("INCOMPLETE_SEAT_SCORES")
        if any(type(value) is not int for value in self.point_deltas.values()):
            raise SpecialHandEndError("INVALID_POINT_DELTA")
        if any(type(value) is not int for value in self.final_scores.values()):
            raise SpecialHandEndError("INVALID_FINAL_SCORE")
        if self.riichi_deposit_pool_after != self.transition_directive.riichi_deposit_pool_after:
            raise SpecialHandEndError("TRANSITION_DEPOSIT_POOL_MISMATCH")
        concepts = _concepts(self.applied_concept_ids)
        missing = sorted(set(self.candidate.applied_concept_ids) - set(concepts))
        if missing:
            raise SpecialHandEndError("MISSING_APPLIED_CONCEPT", {"concept_ids": missing})
        object.__setattr__(self, "point_deltas", MappingProxyType(dict(self.point_deltas)))
        object.__setattr__(self, "final_scores", MappingProxyType(dict(self.final_scores)))
        object.__setattr__(self, "applied_concept_ids", concepts)


def resolve_hand_end_candidates(
    candidates: Sequence[HandEndCandidate], profile: ResolvedRuleProfile
) -> HandEndCandidate:
    """Resolve only priorities fixed by D31-02; refuse every unknown conflict."""

    items = tuple(candidates)
    if not items:
        raise SpecialHandEndError("HAND_END_CANDIDATE_REQUIRED")
    for candidate in items:
        require_profile_match(profile, candidate, error_type=SpecialHandEndError)
    event_ids = {candidate.trigger.event_id for candidate in items}
    if len(event_ids) != 1:
        raise SpecialHandEndError("HAND_END_CANDIDATE_CAUSE_MISMATCH")
    semantic_keys = {
        (
            candidate.result_family,
            candidate.reason_code,
            candidate.ordinary_winner_seats,
            candidate.qualifier_seats,
            candidate.declared_tenpai_seats,
        )
        for candidate in items
    }
    if len(semantic_keys) != len(items):
        raise SpecialHandEndError("DUPLICATE_HAND_END_CANDIDATE")
    if len(items) == 1:
        return items[0]

    ordinary = [item for item in items if item.result_family is ResultFamily.ORDINARY_WIN]
    three_ron = [item for item in items if item.reason_code is ReasonCode.THREE_RON]
    if len(three_ron) == 1 and len(ordinary) == 1 and len(items) == 2:
        if set(ordinary[0].ordinary_winner_seats) == set(three_ron[0].trigger.related_seats):
            return three_ron[0]
    if ordinary:
        if len(ordinary) == 1:
            return ordinary[0]
        raise SpecialHandEndError("SPECIAL_END_PRIORITY_UNRESOLVED")

    nagashi = [item for item in items if item.result_family is ResultFamily.NAGASHI_MANGAN]
    exhaustive = [item for item in items if item.result_family is ResultFamily.EXHAUSTIVE_DRAW]
    if len(nagashi) == 1 and len(exhaustive) == 1 and len(items) == 2:
        return nagashi[0]
    raise SpecialHandEndError("SPECIAL_END_PRIORITY_UNRESOLVED")


def settle_hand_end(
    candidate: HandEndCandidate,
    profile: ResolvedRuleProfile,
    *,
    initial_scores: Mapping[Seat, int],
    riichi_deposit_pool_before: int,
    point_deltas: Mapping[Seat, int],
    final_scores: Mapping[Seat, int],
    transition_directive: TransitionDirective,
    applied_concept_ids: Sequence[str],
) -> SettledHandEndResult:
    """Validate one family-specific settlement without calculating its rule values."""

    require_profile_match(profile, candidate, error_type=SpecialHandEndError)
    if set(initial_scores) != set(SEATS) or any(
        type(value) is not int for value in initial_scores.values()
    ):
        raise SpecialHandEndError("INCOMPLETE_INITIAL_SCORES")
    if type(riichi_deposit_pool_before) is not int or riichi_deposit_pool_before < 0:
        raise SpecialHandEndError("INVALID_RIICHI_DEPOSIT_POOL")
    if set(point_deltas) != set(SEATS) or set(final_scores) != set(SEATS):
        raise SpecialHandEndError("INCOMPLETE_SEAT_SCORES")
    expected_scores = {seat: initial_scores[seat] + point_deltas[seat] for seat in SEATS}
    if dict(final_scores) != expected_scores:
        raise SpecialHandEndError("HAND_END_SCORE_MISMATCH")
    before_total = sum(initial_scores.values()) + riichi_deposit_pool_before
    after_total = sum(final_scores.values()) + transition_directive.riichi_deposit_pool_after
    if before_total != after_total:
        raise SpecialHandEndError(
            "HAND_END_POINT_CONSERVATION_FAILED",
            {"before_total": before_total, "after_total": after_total},
        )
    return SettledHandEndResult(
        candidate=candidate,
        point_deltas=point_deltas,
        final_scores=final_scores,
        riichi_deposit_pool_after=transition_directive.riichi_deposit_pool_after,
        transition_directive=transition_directive,
        applied_concept_ids=tuple(applied_concept_ids),
    )
