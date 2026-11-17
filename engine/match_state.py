"""Immutable v0.2 match-state and inter-hand transition contracts.

This module defines ownership boundaries only. ``HandResult`` owns the settled
outcome of one hand, ``TransitionResult`` owns the rule-derived candidate for
what follows it, and ``MatchState`` owns an active match with exactly one
separately generated current hand. Transition calculation, match-end decisions,
and next-hand generation are later engine boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Mapping

from engine.game_state import GameState, SEATS, Seat
from engine.hand_log import HandResult, Participant


class MatchStateValidationError(ValueError):
    """A structured match-contract violation."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class TransitionDisposition(str, Enum):
    """What may consume an inter-hand transition candidate next."""

    NEXT_HAND_CANDIDATE = "next_hand_candidate"
    MATCH_END_REVIEW_REQUIRED = "match_end_review_required"


class MatchEndDecision(str, Enum):
    """Whether a validated transition may initialize another hand."""

    CONTINUE = "CONTINUE"
    END = "END"


def _validate_snapshot(profile_id: str, snapshot_id: str) -> None:
    if not profile_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot_id):
        raise MatchStateValidationError("INVALID_RULE_SNAPSHOT", {})


def _freeze_seat_mapping(
    value: Mapping[Seat, Any], *, code: str
) -> Mapping[Seat, Any]:
    if set(value) != set(SEATS):
        raise MatchStateValidationError(
            code,
            {"seats": sorted(seat.value if isinstance(seat, Seat) else str(seat) for seat in value)},
        )
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class MatchState:
    """Complete immutable match state while one generated hand is active."""

    match_id: str
    participants: tuple[Participant, ...]
    seat_assignments: Mapping[Seat, str]
    initial_dealer: Seat
    dealer: Seat
    round_wind: Seat
    hand_number: int
    honba: int
    riichi_deposits: int
    scores: Mapping[Seat, int]
    rule_profile_id: str
    rule_snapshot_id: str
    current_hand_id: str
    current_hand: GameState

    def __post_init__(self) -> None:
        if not self.match_id or not self.current_hand_id:
            raise MatchStateValidationError("INVALID_MATCH_IDENTITY", {})
        _validate_snapshot(self.rule_profile_id, self.rule_snapshot_id)

        if any(
            not isinstance(seat, Seat)
            for seat in (self.initial_dealer, self.dealer, self.round_wind)
        ):
            raise MatchStateValidationError("INVALID_SEAT", {})
        if self.round_wind not in {Seat.EAST, Seat.SOUTH, Seat.WEST}:
            raise MatchStateValidationError(
                "INVALID_ROUND_WIND", {"round_wind": self.round_wind.value}
            )

        participants = tuple(self.participants)
        participant_ids = [participant.participant_id for participant in participants]
        if len(participants) != len(SEATS) or len(set(participant_ids)) != len(SEATS):
            raise MatchStateValidationError(
                "INVALID_PARTICIPANTS", {"participant_ids": participant_ids}
            )
        assignments = _freeze_seat_mapping(
            self.seat_assignments, code="INCOMPLETE_SEAT_ASSIGNMENTS"
        )
        if set(assignments.values()) != set(participant_ids):
            raise MatchStateValidationError("INVALID_PARTICIPANT_ASSIGNMENT", {})

        scores = _freeze_seat_mapping(self.scores, code="INCOMPLETE_SEAT_SCORES")
        if any(not isinstance(score, int) for score in scores.values()):
            raise MatchStateValidationError("INVALID_SCORE", {})
        if self.hand_number not in range(1, 5):
            raise MatchStateValidationError(
                "INVALID_HAND_NUMBER", {"hand_number": self.hand_number}
            )
        if self.honba < 0 or self.riichi_deposits < 0:
            raise MatchStateValidationError(
                "INVALID_COUNTER",
                {"honba": self.honba, "riichi_deposits": self.riichi_deposits},
            )

        hand_scores = {
            seat: self.current_hand.players_by_seat[seat].score for seat in SEATS
        }
        expected_hand_values = {
            "dealer": self.dealer,
            "round_wind": self.round_wind,
            "hand_number": self.hand_number,
            "honba": self.honba,
            "riichi_deposits": self.riichi_deposits,
            "scores": scores,
            "rule_profile_id": self.rule_profile_id,
            "rule_snapshot_id": self.rule_snapshot_id,
        }
        actual_hand_values = {
            "dealer": self.current_hand.dealer,
            "round_wind": self.current_hand.round_wind,
            "hand_number": self.current_hand.hand_number,
            "honba": self.current_hand.honba,
            "riichi_deposits": self.current_hand.riichi_deposits,
            "scores": hand_scores,
            "rule_profile_id": self.current_hand.rule_profile_id,
            "rule_snapshot_id": self.current_hand.rule_snapshot_id,
        }
        mismatches = [
            name
            for name, expected in expected_hand_values.items()
            if actual_hand_values[name] != expected
        ]
        if mismatches:
            raise MatchStateValidationError(
                "CURRENT_HAND_MISMATCH", {"fields": mismatches}
            )

        object.__setattr__(self, "seat_assignments", assignments)
        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "participants", participants)


@dataclass(frozen=True)
class TransitionResult:
    """Rule-derived state candidate after one settled hand.

    It never contains a wall, deal, per-hand player state, or authorization to
    start another hand. A later match-end decision must authorize the candidate
    before the next-hand initializer creates a new ``GameState``.
    """

    match_id: str
    completed_hand_id: str
    hand_result: HandResult
    disposition: TransitionDisposition
    next_dealer: Seat
    next_round_wind: Seat
    next_hand_number: int
    next_honba: int
    carried_riichi_deposits: int
    scores: Mapping[Seat, int]
    applied_concept_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.match_id or not self.completed_hand_id:
            raise MatchStateValidationError("INVALID_TRANSITION_IDENTITY", {})
        if not isinstance(self.disposition, TransitionDisposition) or any(
            not isinstance(seat, Seat)
            for seat in (self.next_dealer, self.next_round_wind)
        ):
            raise MatchStateValidationError("INVALID_TRANSITION_VALUE", {})
        if self.next_round_wind not in {Seat.EAST, Seat.SOUTH, Seat.WEST}:
            raise MatchStateValidationError(
                "INVALID_ROUND_WIND", {"round_wind": self.next_round_wind.value}
            )
        if self.next_hand_number not in range(1, 5):
            raise MatchStateValidationError(
                "INVALID_HAND_NUMBER", {"hand_number": self.next_hand_number}
            )
        if self.next_honba < 0 or self.carried_riichi_deposits < 0:
            raise MatchStateValidationError(
                "INVALID_COUNTER",
                {"honba": self.next_honba, "riichi_deposits": self.carried_riichi_deposits},
            )
        scores = _freeze_seat_mapping(self.scores, code="INCOMPLETE_SEAT_SCORES")
        if any(not isinstance(score, int) for score in scores.values()):
            raise MatchStateValidationError("INVALID_SCORE", {})
        if dict(scores) != dict(self.hand_result.final_scores):
            raise MatchStateValidationError("HAND_RESULT_SCORE_MISMATCH", {})
        applied_concept_ids = tuple(self.applied_concept_ids)
        if not applied_concept_ids or any(
            not concept_id for concept_id in applied_concept_ids
        ):
            raise MatchStateValidationError("INVALID_CONCEPT_REFERENCE", {})
        if len(set(applied_concept_ids)) != len(applied_concept_ids):
            raise MatchStateValidationError("DUPLICATE_CONCEPT_REFERENCE", {})
        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "applied_concept_ids", applied_concept_ids)


@dataclass(frozen=True)
class MatchEndResult:
    """Final match-end decision without final-point calculation.

    ``next_hand_authorized`` is intentionally redundant with ``decision`` so a
    later initializer can validate authorization without inferring it from a
    reason code. Final deposit allocation and final points remain later stages.
    """

    match_id: str
    completed_hand_id: str
    decision: MatchEndDecision
    reason_code: str
    next_hand_authorized: bool
    scores: Mapping[Seat, int]
    riichi_deposits: int
    applied_concept_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.match_id or not self.completed_hand_id or not self.reason_code:
            raise MatchStateValidationError("INVALID_MATCH_END_IDENTITY", {})
        if not isinstance(self.decision, MatchEndDecision):
            raise MatchStateValidationError("INVALID_MATCH_END_DECISION", {})
        if self.next_hand_authorized is not (
            self.decision is MatchEndDecision.CONTINUE
        ):
            raise MatchStateValidationError("INVALID_NEXT_HAND_AUTHORIZATION", {})
        scores = _freeze_seat_mapping(self.scores, code="INCOMPLETE_SEAT_SCORES")
        if any(not isinstance(score, int) for score in scores.values()):
            raise MatchStateValidationError("INVALID_SCORE", {})
        if not isinstance(self.riichi_deposits, int) or self.riichi_deposits < 0:
            raise MatchStateValidationError(
                "INVALID_RIICHI_DEPOSIT_POOL", {"value": self.riichi_deposits}
            )
        concept_ids = tuple(self.applied_concept_ids)
        if not concept_ids or any(not concept_id for concept_id in concept_ids):
            raise MatchStateValidationError("INVALID_CONCEPT_REFERENCE", {})
        if len(set(concept_ids)) != len(concept_ids):
            raise MatchStateValidationError("DUPLICATE_CONCEPT_REFERENCE", {})
        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "applied_concept_ids", concept_ids)
