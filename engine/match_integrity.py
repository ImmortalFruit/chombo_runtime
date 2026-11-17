"""Canonical state hashes and tamper detection for v0.2 match records."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from engine.record_replay import apply_state_delta


INTEGRITY_ALGORITHM = "canonical-json-sha256.v1"
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class MatchIntegrityError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in record.items() if key != "integrity"}


def _hand_integrity(hand: Mapping[str, Any], hand_index: int) -> dict[str, Any]:
    initial = hand.get("authoritative_initial_state")
    events = hand.get("events")
    if not isinstance(initial, Mapping) or not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise MatchIntegrityError("INVALID_INTEGRITY_INPUT", {"hand_index": hand_index})
    state = deepcopy(dict(initial))
    frames: list[dict[str, Any]] = []
    for expected, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise MatchIntegrityError("INVALID_INTEGRITY_EVENT", {"hand_index": hand_index, "sequence": expected})
        sequence = event.get("sequence")
        if sequence != expected:
            raise MatchIntegrityError(
                "EVENT_SEQUENCE_MISMATCH",
                {"hand_index": hand_index, "expected": expected, "actual": sequence},
            )
        authoritative = event.get("authoritative_data")
        delta = authoritative.get("state_delta", {}) if isinstance(authoritative, Mapping) else None
        if not isinstance(delta, Mapping):
            raise MatchIntegrityError(
                "INVALID_INTEGRITY_STATE_DELTA",
                {"hand_index": hand_index, "sequence": expected, "event_id": event.get("event_id")},
            )
        before = _hash(state)
        state = apply_state_delta(state, delta)
        frames.append(
            {
                "sequence": expected,
                "event_id": event.get("event_id"),
                "before_state_hash": before,
                "after_state_hash": _hash(state),
                "event_hash": _hash(event),
            }
        )
    return {
        "hand_id": hand.get("hand_id"),
        "initial_state_hash": _hash(initial),
        "events": frames,
        "final_state_hash": _hash(state),
    }


def build_match_integrity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Build reproducible hashes; replay checkpoints are intentionally excluded."""
    hands = record.get("hands")
    if not isinstance(hands, Sequence) or isinstance(hands, (str, bytes)):
        raise MatchIntegrityError("INVALID_INTEGRITY_INPUT", {})
    return {
        "algorithm": INTEGRITY_ALGORITHM,
        "hands": [_hand_integrity(hand, index) for index, hand in enumerate(hands)],
        "record_hash": _hash(_record_payload(record)),
    }


def verify_match_integrity(record: Mapping[str, Any]) -> None:
    """Verify event ordering, before/after states, event bytes, and the whole record."""
    actual = record.get("integrity")
    if not isinstance(actual, Mapping) or actual.get("algorithm") != INTEGRITY_ALGORITHM:
        raise MatchIntegrityError("MISSING_OR_UNSUPPORTED_INTEGRITY", {})
    actual_hands = actual.get("hands")
    hands = record.get("hands")
    if not isinstance(actual_hands, Sequence) or isinstance(actual_hands, (str, bytes)) or not isinstance(hands, Sequence) or len(actual_hands) != len(hands):
        raise MatchIntegrityError("INTEGRITY_HAND_COUNT_MISMATCH", {})

    expected_hands = [_hand_integrity(hand, index) for index, hand in enumerate(hands)]
    fields = ("initial_state_hash", "before_state_hash", "after_state_hash", "event_hash", "final_state_hash")
    for hand_index, (stored_hand, expected_hand) in enumerate(zip(actual_hands, expected_hands)):
        if not isinstance(stored_hand, Mapping) or stored_hand.get("hand_id") != expected_hand["hand_id"]:
            raise MatchIntegrityError("INTEGRITY_HAND_ID_MISMATCH", {"hand_index": hand_index})
        if stored_hand.get("initial_state_hash") != expected_hand["initial_state_hash"]:
            raise MatchIntegrityError("STATE_HASH_MISMATCH", {"hand_index": hand_index, "sequence": -1, "field": "initial_state_hash"})
        stored_events = stored_hand.get("events")
        expected_events = expected_hand["events"]
        if not isinstance(stored_events, Sequence) or isinstance(stored_events, (str, bytes)) or len(stored_events) != len(expected_events):
            raise MatchIntegrityError("INTEGRITY_EVENT_COUNT_MISMATCH", {"hand_index": hand_index})
        for sequence, (stored, expected) in enumerate(zip(stored_events, expected_events)):
            if not isinstance(stored, Mapping):
                raise MatchIntegrityError("INVALID_INTEGRITY_EVENT", {"hand_index": hand_index, "sequence": sequence})
            if stored.get("sequence") != sequence or stored.get("event_id") != expected["event_id"]:
                raise MatchIntegrityError("INTEGRITY_EVENT_IDENTITY_MISMATCH", {"hand_index": hand_index, "sequence": sequence, "event_id": expected["event_id"]})
            for field in fields[1:4]:
                if not _HASH_PATTERN.fullmatch(str(stored.get(field, ""))) or stored.get(field) != expected[field]:
                    raise MatchIntegrityError("EVENT_INTEGRITY_MISMATCH", {"hand_index": hand_index, "sequence": sequence, "event_id": expected["event_id"], "field": field})
        if stored_hand.get("final_state_hash") != expected_hand["final_state_hash"]:
            raise MatchIntegrityError("STATE_HASH_MISMATCH", {"hand_index": hand_index, "sequence": len(expected_events) - 1, "field": "final_state_hash"})

    expected_record_hash = _hash(_record_payload(record))
    if not _HASH_PATTERN.fullmatch(str(actual.get("record_hash", ""))) or actual.get("record_hash") != expected_record_hash:
        raise MatchIntegrityError("MATCH_RECORD_HASH_MISMATCH", {})
