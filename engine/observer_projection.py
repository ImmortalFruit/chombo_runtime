"""Observer-scoped projections of an authoritative v0.1 hand log."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from engine.game_state import SEATS, Seat
from engine.record_replay import apply_state_delta
from engine.visible_waits import all_open_remaining_counts, visible_waits_for_seat


_PUBLIC_EVENT_FIELDS = {
    "hand_started": frozenset(),
    "action_applied": frozenset(
        {"action_id", "kind", "actor", "tile_ids", "claimed_tile_id", "target_seat", "target_meld_index"}
    ),
    "response_resolved": frozenset({"adopted_actions"}),
    "tenpai_declared": frozenset({"declaration"}),
    "hand_ended": frozenset(
        {
            "reason", "winners", "declared_tenpai_player_ids", "win_detail",
            "win_details", "special_result_family", "qualifier_seats",
        }
    ),
    "settlement_applied": frozenset({"point_deltas", "final_scores"}),
}


class ObserverProjectionError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class ObserverKind(str, Enum):
    PUBLIC = "public"
    HUMAN_SEAT = "human_seat"
    AUTHORIZED_REPLAY = "authorized_replay"


@dataclass(frozen=True)
class Observer:
    kind: ObserverKind
    seat: Seat | None = None

    def __post_init__(self) -> None:
        if (self.kind is ObserverKind.HUMAN_SEAT) != (self.seat is not None):
            raise ObserverProjectionError(
                "INVALID_OBSERVER", {"kind": self.kind.value, "seat": self.seat.value if self.seat else None}
            )


PUBLIC_OBSERVER = Observer(ObserverKind.PUBLIC)
AUTHORIZED_REPLAY_OBSERVER = Observer(ObserverKind.AUTHORIZED_REPLAY)


def human_observer(seat: Seat) -> Observer:
    return Observer(ObserverKind.HUMAN_SEAT, seat)


def _initial_hands(authoritative: Mapping[str, Any]) -> dict[str, Any]:
    players = authoritative.get("players", [])
    if not isinstance(players, Sequence) or isinstance(players, (str, bytes)):
        return {}
    return {
        str(player["seat"]): deepcopy(player.get("concealed_hand", []))
        for player in players
        if isinstance(player, Mapping) and "seat" in player
    }


def _allowed_hands(hands: Mapping[str, Any], observer: Observer) -> dict[str, Any]:
    if observer.kind is ObserverKind.PUBLIC:
        return {}
    if observer.kind is ObserverKind.HUMAN_SEAT:
        seat = observer.seat.value
        return {seat: deepcopy(hands[seat])} if seat in hands else {}
    return {seat.value: deepcopy(hands.get(seat.value, [])) for seat in SEATS}


def _add_hands(state: Mapping[str, Any], hands: Mapping[str, Any]) -> dict[str, Any]:
    projected = _public_only_state(state)
    seats = projected.get("seats")
    if not isinstance(seats, Mapping):
        raise ObserverProjectionError("INVALID_PUBLIC_STATE", {})
    for seat, tiles in hands.items():
        if seat in seats:
            seats[seat]["concealed_tiles"] = deepcopy(tiles)
    return projected


def _public_only_state(state: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(state))
    for forbidden in ("live_wall", "dead_wall", "players"):
        projected.pop(forbidden, None)
    seats = projected.get("seats")
    if isinstance(seats, Mapping):
        for details in seats.values():
            if isinstance(details, dict):
                details.pop("concealed_hand", None)
                details.pop("concealed_tiles", None)
    return projected


def project_hand_log(document: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER) -> dict[str, Any]:
    """Remove authoritative data and expose only information allowed to observer."""
    if document.get("record_format") not in {"semantic-delta.v1", "semantic-delta.v2"}:
        raise ObserverProjectionError(
            "UNSUPPORTED_RECORD_FORMAT", {"record_format": document.get("record_format")}
        )
    initial_public = document.get("initial_public_state")
    events = document.get("events")
    if not isinstance(initial_public, Mapping):
        raise ObserverProjectionError("INVALID_PUBLIC_STATE", {"location": "initial"})
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ObserverProjectionError("INVALID_EVENT_LOG", {})

    initial_hands = _allowed_hands(
        _initial_hands(document.get("authoritative_initial_state", {})), observer
    )
    all_hands = _initial_hands(document.get("authoritative_initial_state", {}))
    authoritative_state = deepcopy(dict(document.get("authoritative_initial_state", {})))
    current_state = _public_only_state(initial_public)
    waits_by_seat: dict[str, list[dict[str, Any]]] = {
        seat.value: [] for seat in SEATS
    }
    waits_started: set[str] = set()
    projected_events = []
    revealed_seats: set[str] = set()
    exhaustive_draw = False
    for event in events:
        if not isinstance(event, Mapping) or not isinstance(event.get("public_data"), Mapping):
            raise ObserverProjectionError("INVALID_PUBLIC_EVENT", {})
        kind = event.get("kind")
        if kind not in _PUBLIC_EVENT_FIELDS:
            raise ObserverProjectionError("INVALID_EVENT_KIND", {"kind": kind})
        public_data = {
            key: deepcopy(value)
            for key, value in event["public_data"].items()
            if key in _PUBLIC_EVENT_FIELDS[kind]
        }
        delta = event["public_data"].get("state_delta", {})
        if not isinstance(delta, Mapping):
            raise ObserverProjectionError("INVALID_STATE_DELTA", {})
        current_state = _public_only_state(apply_state_delta(current_state, delta))
        authoritative = event.get("authoritative_data", {})
        if not isinstance(authoritative, Mapping):
            raise ObserverProjectionError("INVALID_AUTHORITATIVE_EVENT", {})
        authoritative_delta = authoritative.get("state_delta", {})
        if not isinstance(authoritative_delta, Mapping):
            raise ObserverProjectionError("INVALID_AUTHORITATIVE_STATE_DELTA", {})
        authoritative_state = apply_state_delta(authoritative_state, authoritative_delta)
        all_hands = _initial_hands(authoritative_state)
        if (
            kind == "action_applied"
            and public_data.get("kind") in {"discard", "riichi"}
            and event.get("actor") in waits_by_seat
        ):
            waits_started.add(str(event["actor"]))
        if (
            kind == "tenpai_declared"
            and public_data.get("declaration") == "tenpai"
            and event.get("actor") in {candidate.value for candidate in SEATS}
        ):
            revealed_seats.add(event["actor"])
        if kind == "hand_ended":
            exhaustive_draw = public_data.get("reason") == "exhaustive_draw"
            revealed = [
                *public_data.get("winners", []),
                *public_data.get("declared_tenpai_player_ids", []),
            ]
            if isinstance(revealed, Sequence) and not isinstance(revealed, (str, bytes)):
                revealed_seats.update(
                    seat for seat in revealed if seat in {candidate.value for candidate in SEATS}
                )
        allowed_hands = _allowed_hands(all_hands, observer)
        if (
            observer.kind is ObserverKind.HUMAN_SEAT
            and kind in {"hand_ended", "settlement_applied"}
            and exhaustive_draw
            and observer.seat.value not in revealed_seats
        ):
            allowed_hands.pop(observer.seat.value, None)
        allowed_hands.update(
            {
                seat: deepcopy(all_hands[seat])
                for seat in revealed_seats
                if seat in all_hands
            }
        )
        eligible_wait_seats = waits_started.intersection(allowed_hands)
        if eligible_wait_seats:
            all_open = (
                all_open_remaining_counts(current_state, authoritative_state)
                if observer.kind is ObserverKind.AUTHORIZED_REPLAY
                else {}
            )
            for wait_seat in eligible_wait_seats:
                waits_by_seat[wait_seat] = visible_waits_for_seat(
                    current_state,
                    authoritative_state,
                    wait_seat,
                    observer_seat=wait_seat,
                    rule_profile_id=str(document.get("rule_profile_id", "")),
                    rule_snapshot_id=str(document.get("rule_snapshot_id", "")),
                )
                if observer.kind is ObserverKind.AUTHORIZED_REPLAY:
                    for wait in waits_by_seat[wait_seat]:
                        wait["all_open_remaining_count"] = all_open.get(
                            wait["tile_type"], 4
                        )
        public_data["state"] = _add_hands(current_state, allowed_hands)
        public_data["visible_waits_by_seat"] = {
            seat: deepcopy(waits_by_seat[seat])
            for seat in allowed_hands
        }
        projected_events.append(
            {
                key: deepcopy(event.get(key))
                for key in (
                    "sequence", "event_id", "kind", "actor",
                    "caused_by_event_ids", "applied_concept_ids",
                )
            } | {"public_data": public_data}
        )

    return {
        key: deepcopy(document.get(key))
        for key in (
            "hand_id", "schema_version", "seed", "rng_id", "policy_id",
            "record_format", "rule_profile_id", "rule_snapshot_id", "result",
            "participants", "seat_assignments",
        )
    } | {
        "observer": {
            "kind": observer.kind.value,
            "seat": observer.seat.value if observer.seat else None,
            "may_reveal_all_hands": observer.kind is ObserverKind.AUTHORIZED_REPLAY,
        },
        "initial_public_state": _add_hands(initial_public, initial_hands),
        "events": projected_events,
    }
