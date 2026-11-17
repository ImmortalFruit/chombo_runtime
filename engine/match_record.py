"""Canonical east-south match records and observer-scoped replay projection."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from engine.game_state import SEATS
from engine.hand_viewer import viewer_document
from engine.match_orchestrator import MatchRun
from engine.match_integrity import build_match_integrity, verify_match_integrity
from engine.observer_projection import Observer, ObserverKind, PUBLIC_OBSERVER, human_observer


SCHEMA_VERSION = "chombo.match_record.v0.2"


class MatchRecordError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _seat_points(values: Mapping[Any, Any]) -> dict[str, int]:
    return {seat.value: int(values[seat]) for seat in SEATS}


def _hand_entry(document: Mapping[str, Any]) -> dict[str, Any]:
    omitted = {"participants", "rule_profile_id", "rule_snapshot_id"}
    return {key: deepcopy(value) for key, value in document.items() if key not in omitted}


def match_record_document(run: MatchRun) -> dict[str, Any]:
    """Build a match record without repeating match-wide metadata in every hand."""
    first = run.hand_logs[0]
    participants = [
        {
            "participant_id": item.participant_id,
            "display_name": item.display_name,
            "controller_id": item.controller_id,
        }
        for item in first.participants
    ]
    transitions = [
        {
            "completed_hand_id": item.completed_hand_id,
            "disposition": item.disposition.value,
            "next_dealer": item.next_dealer.value,
            "next_round_wind": item.next_round_wind.value,
            "next_hand_number": item.next_hand_number,
            "next_honba": item.next_honba,
            "carried_riichi_deposits": item.carried_riichi_deposits,
            "scores": _seat_points(item.scores),
            "applied_concept_ids": list(item.applied_concept_ids),
        }
        for item in run.transitions
    ]
    final = run.final_points
    result = {
        "completed_hand_id": run.match_end.completed_hand_id,
        "reason_code": run.match_end.reason_code,
        "scores_before_deposit": _seat_points(run.match_end.scores),
        "remaining_riichi_deposits": run.match_end.riichi_deposits,
        "players": [
            {
                "participant_id": item.participant_id,
                "seat": item.seat.value,
                "rank": item.rank,
                "final_score": item.final_score,
                "raw_points": item.raw_points,
                "rank_points": item.oka_points + item.uma_points + item.other_rank_points,
                "final_competition_points": item.final_competition_points,
            }
            for item in final.players
        ],
        "final_points_sum": final.final_points_sum,
        "applied_concept_ids": list(final.applied_concept_ids),
    }
    document = {
        "schema_version": SCHEMA_VERSION,
        "record_format": "semantic-delta.v2",
        "match_id": run.match_id,
        "format": "east_south",
        "seed": run.seed,
        "rule_profile_id": first.rule_profile_id,
        "rule_snapshot_id": first.rule_snapshot_id,
        "participants": participants,
        "hands": [_hand_entry(log.to_dict()) for log in run.hand_logs],
        "transitions": transitions,
        "result": result,
    }
    document["integrity"] = build_match_integrity(document)
    validate_match_record(document)
    return document


def hydrated_hand_document(record: Mapping[str, Any], index: int) -> dict[str, Any]:
    hands = record["hands"]
    if not 0 <= index < len(hands):
        raise MatchRecordError("HAND_INDEX_OUT_OF_RANGE", {"index": index})
    return deepcopy(dict(hands[index])) | {
        "rule_profile_id": record["rule_profile_id"],
        "rule_snapshot_id": record["rule_snapshot_id"],
        "participants": deepcopy(record["participants"]),
    }


def validate_match_record(document: Mapping[str, Any], *, verify_integrity_hashes: bool = True) -> None:
    if document.get("schema_version") != SCHEMA_VERSION or document.get("format") != "east_south":
        raise MatchRecordError("INVALID_MATCH_RECORD_IDENTITY", {})
    if document.get("record_format") != "semantic-delta.v2" or not document.get("match_id"):
        raise MatchRecordError("INVALID_MATCH_RECORD_IDENTITY", {})
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(document.get("rule_snapshot_id", ""))):
        raise MatchRecordError("INVALID_RULE_SNAPSHOT", {})
    participants = document.get("participants")
    hands = document.get("hands")
    transitions = document.get("transitions")
    if not isinstance(participants, Sequence) or isinstance(participants, (str, bytes)) or len(participants) != 4:
        raise MatchRecordError("INVALID_PARTICIPANTS", {})
    participant_ids = [item.get("participant_id") for item in participants if isinstance(item, Mapping)]
    if len(participant_ids) != 4 or len(set(participant_ids)) != 4:
        raise MatchRecordError("INVALID_PARTICIPANTS", {})
    if not isinstance(hands, Sequence) or isinstance(hands, (str, bytes)) or not hands:
        raise MatchRecordError("EMPTY_MATCH_RECORD", {})
    if not isinstance(transitions, Sequence) or len(transitions) != len(hands):
        raise MatchRecordError("TRANSITION_COUNT_MISMATCH", {})
    hand_ids: list[str] = []
    previous_participant_scores: Mapping[str, Any] | None = None
    for index, (hand, transition) in enumerate(zip(hands, transitions)):
        if not isinstance(hand, Mapping) or not isinstance(transition, Mapping):
            raise MatchRecordError("INVALID_HAND_ENTRY", {"index": index})
        if any(key in hand for key in ("participants", "rule_profile_id", "rule_snapshot_id")):
            raise MatchRecordError("DUPLICATED_MATCH_METADATA", {"index": index})
        hand_id = hand.get("hand_id")
        if not hand_id or hand_id in hand_ids:
            raise MatchRecordError("INVALID_HAND_ID", {"index": index})
        hand_ids.append(str(hand_id))
        if transition.get("completed_hand_id") != hand_id:
            raise MatchRecordError("TRANSITION_HAND_MISMATCH", {"index": index})
        initial = hand.get("initial_public_state", {})
        result = hand.get("result", {})
        assignments = hand.get("seat_assignments", {})
        if previous_participant_scores is not None:
            actual = {
                assignments.get(seat): initial.get("seats", {}).get(seat, {}).get("score")
                for seat in ("east", "south", "west", "north")
            }
            if actual != dict(previous_participant_scores):
                raise MatchRecordError("HAND_SCORE_CONTINUITY_MISMATCH", {"index": index})
        final_scores = result.get("final_scores") if isinstance(result, Mapping) else None
        if transition.get("scores") != final_scores:
            raise MatchRecordError("TRANSITION_SCORE_MISMATCH", {"index": index})
        previous_participant_scores = {
            assignments.get(seat): final_scores.get(seat)
            for seat in ("east", "south", "west", "north")
        }
        viewer_document(hydrated_hand_document(document, index))
    result = document.get("result")
    if not isinstance(result, Mapping) or result.get("completed_hand_id") != hand_ids[-1]:
        raise MatchRecordError("MATCH_RESULT_MISMATCH", {})
    if verify_integrity_hashes:
        verify_match_integrity(document)


def write_match_record(run: MatchRun, path: Path) -> None:
    document = match_record_document(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def match_viewer_document(record: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER) -> dict[str, Any]:
    validate_match_record(record)
    participants = {item["participant_id"]: item for item in record["participants"]}
    players = [
        deepcopy(dict(item)) | {"display_name": participants[item["participant_id"]]["display_name"]}
        for item in record["result"]["players"]
    ]
    observed_participant_id = (
        record["hands"][0]["seat_assignments"][observer.seat.value]
        if observer.kind is ObserverKind.HUMAN_SEAT and observer.seat is not None
        else None
    )
    hands = []
    for index in range(len(record["hands"])):
        hand_document = hydrated_hand_document(record, index)
        hand_observer = observer
        if observed_participant_id is not None:
            observed_seat = next(
                seat
                for seat, participant_id in hand_document["seat_assignments"].items()
                if participant_id == observed_participant_id
            )
            hand_observer = human_observer(next(seat for seat in SEATS if seat.value == observed_seat))
        hand = viewer_document(hand_document, hand_observer)
        hand["transition_after"] = deepcopy(record["transitions"][index])
        hands.append(hand)
    return {
        "match_id": record["match_id"],
        "schema_version": record["schema_version"],
        "observer": hands[0]["observer"],
        "participants": deepcopy(record["participants"]),
        "initial_seat_assignments": deepcopy(record["hands"][0]["seat_assignments"]),
        "hands": hands,
        "result": {
            "completed_hand_id": record["result"]["completed_hand_id"],
            "reason_code": record["result"]["reason_code"],
            "scores_before_deposit": deepcopy(record["result"]["scores_before_deposit"]),
            "remaining_riichi_deposits": record["result"]["remaining_riichi_deposits"],
            "players": players,
            "final_points_sum": record["result"]["final_points_sum"],
        },
    }
