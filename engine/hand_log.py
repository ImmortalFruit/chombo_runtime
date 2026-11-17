"""Immutable v0.1 hand-event, result, and game-record contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from engine.game_state import SEATS, Seat


class HandEventKind(str, Enum):
    HAND_STARTED = "hand_started"
    ACTION_APPLIED = "action_applied"
    RESPONSE_RESOLVED = "response_resolved"
    TENPAI_DECLARED = "tenpai_declared"
    HAND_ENDED = "hand_ended"
    SETTLEMENT_APPLIED = "settlement_applied"


class HandEndReason(str, Enum):
    RON = "ron"
    TSUMO = "tsumo"
    EXHAUSTIVE_DRAW = "exhaustive_draw"
    NAGASHI_MANGAN = "nagashi_mangan"
    NINE_TERMINALS = "nine_terminals"
    FOUR_WINDS_FIRST_DISCARD = "four_winds_first_discard"
    FOUR_RIICHI = "four_riichi"
    FOUR_KANS = "four_kans"
    THREE_RON = "three_ron"


class HandLogValidationError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise HandLogValidationError("NON_STRING_DATA_KEY", {})
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise HandLogValidationError("NON_JSON_DATA_VALUE", {"type": type(value).__name__})


@dataclass(frozen=True)
class Participant:
    participant_id: str
    display_name: str
    controller_id: str

    def __post_init__(self) -> None:
        if not self.participant_id or not self.display_name or not self.controller_id:
            raise HandLogValidationError("INVALID_PARTICIPANT", {})


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze_value(value)


@dataclass(frozen=True)
class HandEvent:
    sequence: int
    event_id: str
    kind: HandEventKind
    actor: Seat | None = None
    caused_by_event_ids: tuple[str, ...] = ()
    public_data: Mapping[str, Any] = field(default_factory=dict)
    authoritative_data: Mapping[str, Any] = field(default_factory=dict)
    applied_concept_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.sequence < 0 or not self.event_id:
            raise HandLogValidationError(
                "INVALID_EVENT_IDENTITY", {"sequence": self.sequence, "event_id": self.event_id}
            )
        if any(not event_id for event_id in self.caused_by_event_ids):
            raise HandLogValidationError("INVALID_CAUSE_REFERENCE", {})
        if len(set(self.caused_by_event_ids)) != len(self.caused_by_event_ids):
            raise HandLogValidationError("DUPLICATE_CAUSE_REFERENCE", {"event_id": self.event_id})
        if any(not concept_id for concept_id in self.applied_concept_ids):
            raise HandLogValidationError("INVALID_CONCEPT_REFERENCE", {})
        if len(set(self.applied_concept_ids)) != len(self.applied_concept_ids):
            raise HandLogValidationError("DUPLICATE_CONCEPT_REFERENCE", {"event_id": self.event_id})
        if self.kind is HandEventKind.ACTION_APPLIED and self.actor is None:
            raise HandLogValidationError("MISSING_EVENT_ACTOR", {"event_id": self.event_id})
        if self.kind is HandEventKind.TENPAI_DECLARED:
            if self.actor is None:
                raise HandLogValidationError("MISSING_EVENT_ACTOR", {"event_id": self.event_id})
            if self.public_data.get("declaration") not in {"tenpai", "noten"}:
                raise HandLogValidationError(
                    "INVALID_TENPAI_DECLARATION", {"event_id": self.event_id}
                )
        object.__setattr__(self, "public_data", _frozen_mapping(self.public_data))
        object.__setattr__(self, "authoritative_data", _frozen_mapping(self.authoritative_data))


@dataclass(frozen=True)
class HandResult:
    end_reason: HandEndReason
    winners: tuple[Seat, ...]
    point_deltas: Mapping[Seat, int]
    final_scores: Mapping[Seat, int]
    applied_concept_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.winners)) != len(self.winners):
            raise HandLogValidationError("DUPLICATE_WINNER", {})
        draw_reasons = {
            HandEndReason.EXHAUSTIVE_DRAW,
            HandEndReason.NINE_TERMINALS,
            HandEndReason.FOUR_WINDS_FIRST_DISCARD,
            HandEndReason.FOUR_RIICHI,
            HandEndReason.FOUR_KANS,
            HandEndReason.THREE_RON,
        }
        if self.end_reason in draw_reasons and self.winners:
            raise HandLogValidationError("DRAW_HAS_WINNER", {})
        if self.end_reason not in draw_reasons and not self.winners:
            raise HandLogValidationError("WIN_MISSING_WINNER", {"reason": self.end_reason.value})
        if set(self.point_deltas) != set(SEATS) or set(self.final_scores) != set(SEATS):
            raise HandLogValidationError("INCOMPLETE_SEAT_SCORES", {})
        if any(not isinstance(value, int) for value in self.point_deltas.values()):
            raise HandLogValidationError("INVALID_POINT_DELTA", {})
        if any(not isinstance(value, int) for value in self.final_scores.values()):
            raise HandLogValidationError("INVALID_FINAL_SCORE", {})
        if not self.applied_concept_ids or any(not item for item in self.applied_concept_ids):
            raise HandLogValidationError("INVALID_CONCEPT_REFERENCE", {})
        if len(set(self.applied_concept_ids)) != len(self.applied_concept_ids):
            raise HandLogValidationError("DUPLICATE_CONCEPT_REFERENCE", {})
        object.__setattr__(self, "point_deltas", MappingProxyType(dict(self.point_deltas)))
        object.__setattr__(self, "final_scores", MappingProxyType(dict(self.final_scores)))


@dataclass(frozen=True)
class HandLog:
    """One authoritative v0.1 record with a separately consumable public view."""

    hand_id: str
    schema_version: str
    seed: int
    rng_id: str
    policy_id: str
    rule_profile_id: str
    rule_snapshot_id: str
    initial_public_state: Mapping[str, Any]
    authoritative_initial_state: Mapping[str, Any]
    events: tuple[HandEvent, ...]
    result: HandResult
    participants: tuple[Participant, ...] = ()
    seat_assignments: Mapping[Seat, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hand_id or self.schema_version != "chombo.hand_log.v0.1":
            raise HandLogValidationError("INVALID_HAND_LOG_IDENTITY", {})
        if not self.rng_id or not self.policy_id or not self.rule_profile_id:
            raise HandLogValidationError("MISSING_REPRODUCTION_METADATA", {})
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.rule_snapshot_id):
            raise HandLogValidationError("INVALID_RULE_SNAPSHOT", {})
        if (
            self.authoritative_initial_state.get("rule_profile_id") != self.rule_profile_id
            or self.authoritative_initial_state.get("rule_snapshot_id") != self.rule_snapshot_id
        ):
            raise HandLogValidationError("INITIAL_RULE_METADATA_MISMATCH", {})
        if not self.events:
            raise HandLogValidationError("EMPTY_EVENT_LOG", {})
        participant_ids = [item.participant_id for item in self.participants]
        if len(participant_ids) != len(set(participant_ids)):
            raise HandLogValidationError("DUPLICATE_PARTICIPANT_ID", {})
        if len(self.participants) != len(SEATS):
            raise HandLogValidationError(
                "INCOMPLETE_PARTICIPANTS", {"actual": len(self.participants)}
            )
        if set(self.seat_assignments) != set(SEATS):
            raise HandLogValidationError("INCOMPLETE_SEAT_ASSIGNMENTS", {})
        if set(self.seat_assignments.values()) != set(participant_ids):
            raise HandLogValidationError("INVALID_PARTICIPANT_ASSIGNMENT", {})

        ids: dict[str, int] = {}
        for expected, event in enumerate(self.events):
            if event.sequence != expected:
                raise HandLogValidationError(
                    "NONCONTIGUOUS_EVENT_SEQUENCE",
                    {"expected": expected, "actual": event.sequence},
                )
            if event.event_id in ids:
                raise HandLogValidationError("DUPLICATE_EVENT_ID", {"event_id": event.event_id})
            if expected == 0 and event.caused_by_event_ids:
                raise HandLogValidationError(
                    "UNEXPECTED_HAND_START_CAUSE", {"event_id": event.event_id}
                )
            if expected > 0 and not event.caused_by_event_ids:
                raise HandLogValidationError(
                    "MISSING_CAUSE_REFERENCE", {"event_id": event.event_id}
                )
            for cause in event.caused_by_event_ids:
                if cause not in ids:
                    raise HandLogValidationError(
                        "INVALID_CAUSE_REFERENCE", {"event_id": event.event_id, "cause": cause}
                    )
            ids[event.event_id] = event.sequence
        if self.events[0].kind is not HandEventKind.HAND_STARTED:
            raise HandLogValidationError("MISSING_HAND_START_EVENT", {})
        if self.events[-1].kind is not HandEventKind.SETTLEMENT_APPLIED:
            raise HandLogValidationError("MISSING_SETTLEMENT_EVENT", {})
        if not any(event.kind is HandEventKind.HAND_ENDED for event in self.events[:-1]):
            raise HandLogValidationError("MISSING_HAND_END_EVENT", {})
        hand_end = self.events[-2]
        if hand_end.kind is not HandEventKind.HAND_ENDED:
            raise HandLogValidationError("HAND_END_NOT_BEFORE_SETTLEMENT", {})
        expected_winners = tuple(seat.value for seat in self.result.winners)
        if (
            hand_end.public_data.get("reason") != self.result.end_reason.value
            or tuple(hand_end.public_data.get("winners", ())) != expected_winners
        ):
            raise HandLogValidationError("HAND_RESULT_MISMATCH", {})
        settlement = self.events[-1]
        expected_deltas = {seat.value: value for seat, value in self.result.point_deltas.items()}
        expected_scores = {seat.value: value for seat, value in self.result.final_scores.items()}
        if (
            dict(settlement.public_data.get("point_deltas", {})) != expected_deltas
            or dict(settlement.public_data.get("final_scores", {})) != expected_scores
        ):
            raise HandLogValidationError("SETTLEMENT_RESULT_MISMATCH", {})
        object.__setattr__(self, "initial_public_state", _frozen_mapping(self.initial_public_state))
        object.__setattr__(self, "authoritative_initial_state", _frozen_mapping(self.authoritative_initial_state))
        object.__setattr__(self, "seat_assignments", MappingProxyType(dict(self.seat_assignments)))

    def to_dict(self) -> dict[str, Any]:
        def event_document(event: HandEvent) -> dict[str, Any]:
            return {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "kind": event.kind.value,
                "actor": event.actor.value if event.actor else None,
                "caused_by_event_ids": list(event.caused_by_event_ids),
                "public_data": _thaw_value(event.public_data),
                "authoritative_data": _thaw_value(event.authoritative_data),
                "applied_concept_ids": list(event.applied_concept_ids),
            }

        return {
            "hand_id": self.hand_id,
            "schema_version": self.schema_version,
            "record_format": "semantic-delta.v2",
            "seed": self.seed,
            "rng_id": self.rng_id,
            "policy_id": self.policy_id,
            "rule_profile_id": self.rule_profile_id,
            "rule_snapshot_id": self.rule_snapshot_id,
            "participants": [
                {
                    "participant_id": item.participant_id,
                    "display_name": item.display_name,
                    "controller_id": item.controller_id,
                }
                for item in self.participants
            ],
            "seat_assignments": {
                seat.value: participant_id
                for seat, participant_id in self.seat_assignments.items()
            },
            "initial_public_state": _thaw_value(self.initial_public_state),
            "authoritative_initial_state": _thaw_value(self.authoritative_initial_state),
            "events": [event_document(event) for event in self.events],
            "result": {
                "end_reason": self.result.end_reason.value,
                "winners": [seat.value for seat in self.result.winners],
                "point_deltas": {seat.value: value for seat, value in self.result.point_deltas.items()},
                "final_scores": {seat.value: value for seat, value in self.result.final_scores.items()},
                "applied_concept_ids": list(self.result.applied_concept_ids),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def write_hand_log(log: HandLog, path: Path) -> None:
    """Atomically persist one canonical UTF-8 v0.1 hand log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(log.to_json() + "\n", encoding="utf-8")
    temporary.replace(path)
