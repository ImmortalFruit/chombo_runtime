"""Lossless reconstruction and continuity validation for v0.2 match records."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from engine.game_state import SEATS
from engine.match_record import validate_match_record
from engine.record_replay import apply_state_delta


class MatchReconstructionError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class ReconstructedHand:
    hand_id: str
    initial_state: Mapping[str, Any]
    event_states: tuple[Mapping[str, Any], ...]

    @property
    def final_state(self) -> Mapping[str, Any]:
        return self.event_states[-1]


@dataclass(frozen=True)
class MatchReconstruction:
    match_id: str
    hands: tuple[ReconstructedHand, ...]

    @property
    def final_state(self) -> Mapping[str, Any]:
        return self.hands[-1].final_state


def _frozen_copy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(value)))


def _players_by_seat(state: Mapping[str, Any], *, hand_index: int, sequence: int) -> dict[str, Mapping[str, Any]]:
    players = state.get("players")
    if not isinstance(players, Sequence) or isinstance(players, (str, bytes)):
        raise MatchReconstructionError("INVALID_AUTHORITATIVE_STATE", {"hand_index": hand_index, "sequence": sequence})
    result = {
        str(player.get("seat")): player
        for player in players
        if isinstance(player, Mapping) and isinstance(player.get("seat"), str)
    }
    if set(result) != {seat.value for seat in SEATS} or len(players) != 4:
        raise MatchReconstructionError("INVALID_AUTHORITATIVE_PLAYERS", {"hand_index": hand_index, "sequence": sequence})
    return result


def _tile_ids(state: Mapping[str, Any], *, hand_index: int, sequence: int) -> list[str]:
    tiles: list[Mapping[str, Any]] = []
    for key in ("live_wall", "dead_wall"):
        value = state.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise MatchReconstructionError("INVALID_AUTHORITATIVE_STATE", {"hand_index": hand_index, "sequence": sequence, "field": key})
        tiles.extend(item for item in value if isinstance(item, Mapping))
    for player in _players_by_seat(state, hand_index=hand_index, sequence=sequence).values():
        hand = player.get("concealed_hand", [])
        melds = player.get("melds", [])
        river = player.get("river", [])
        if not all(isinstance(value, Sequence) and not isinstance(value, (str, bytes)) for value in (hand, melds, river)):
            raise MatchReconstructionError("INVALID_AUTHORITATIVE_STATE", {"hand_index": hand_index, "sequence": sequence})
        tiles.extend(item for item in hand if isinstance(item, Mapping))
        for meld in melds:
            if isinstance(meld, Mapping):
                tiles.extend(item for item in meld.get("tiles", []) if isinstance(item, Mapping))
        tiles.extend(item for item in river if isinstance(item, Mapping))
    ids = [str(tile.get("tile_id", "")) for tile in tiles]
    if len(ids) != 136 or not all(ids) or len(set(ids)) != 136:
        raise MatchReconstructionError("TILE_LOCATION_MISMATCH", {"hand_index": hand_index, "sequence": sequence, "tile_count": len(ids), "unique_tile_count": len(set(ids))})
    dead_wall = state.get("dead_wall", [])
    if len(dead_wall) != 14:
        raise MatchReconstructionError("DEAD_WALL_SIZE_MISMATCH", {"hand_index": hand_index, "sequence": sequence, "actual": len(dead_wall)})
    return ids


def _validate_state(state: Mapping[str, Any], record: Mapping[str, Any], hand: Mapping[str, Any], hand_index: int, sequence: int) -> None:
    _tile_ids(state, hand_index=hand_index, sequence=sequence)
    if (state.get("rule_profile_id"), state.get("rule_snapshot_id")) != (record.get("rule_profile_id"), record.get("rule_snapshot_id")):
        raise MatchReconstructionError("RULE_SNAPSHOT_CONTINUITY_MISMATCH", {"hand_index": hand_index, "sequence": sequence})
    players = _players_by_seat(state, hand_index=hand_index, sequence=sequence)
    public = hand.get("initial_public_state", {}) if sequence < 0 else None
    if public is not None and isinstance(public, Mapping):
        expected = public.get("live_wall_count")
        if expected != len(state.get("live_wall", [])):
            raise MatchReconstructionError("LIVE_WALL_COUNT_MISMATCH", {"hand_index": hand_index, "sequence": sequence})
    if any(not isinstance(player.get("score"), int) for player in players.values()):
        raise MatchReconstructionError("INVALID_AUTHORITATIVE_SCORE", {"hand_index": hand_index, "sequence": sequence})


def _public_state(state: Mapping[str, Any], *, hand_index: int, sequence: int) -> dict[str, Any]:
    players = _players_by_seat(state, hand_index=hand_index, sequence=sequence)
    dead_wall = state["dead_wall"]
    indicators = set(state.get("dora_indicator_ids", []))
    return {
        "current_turn": state.get("current_turn"),
        "dealer": state.get("dealer"),
        "round_wind": state.get("round_wind"),
        "hand_number": state.get("hand_number"),
        "live_wall_count": len(state["live_wall"]),
        "honba": state.get("honba"),
        "riichi_deposits": state.get("riichi_deposits"),
        "dora_indicators": [deepcopy(tile) for tile in dead_wall if tile.get("tile_id") in indicators],
        "ura_dora_indicators": [],
        "seats": {
            seat: {
                "concealed_tile_count": len(player.get("concealed_hand", [])),
                "melds": [
                    {
                        "kind": meld.get("kind"),
                        "tiles": deepcopy(meld.get("tiles", [])),
                        "called_tile_id": (
                            meld.get("tiles", [])[-2 if meld.get("kind") == "added_kan" else -1].get("tile_id")
                            if meld.get("called_from") is not None else None
                        ),
                        "called_from": meld.get("called_from"),
                    }
                    for meld in player.get("melds", [])
                ],
                "river": deepcopy(player.get("river", [])),
                "riichi": player.get("riichi"),
                "score": player.get("score"),
            }
            for seat, player in players.items()
        },
    }


def reconstruct_match_states(record: Mapping[str, Any]) -> MatchReconstruction:
    """Rebuild every authoritative post-event state and verify hand continuity."""
    # Reconstruction performs more specific state diagnostics. Integrity is
    # verified by the reader/normal validation boundary before replay.
    validate_match_record(record, verify_integrity_hashes=False)
    reconstructed: list[ReconstructedHand] = []
    previous_transition: Mapping[str, Any] | None = None
    previous_assignments: Mapping[str, Any] | None = None

    for hand_index, hand in enumerate(record["hands"]):
        initial = hand.get("authoritative_initial_state")
        events = hand.get("events")
        if not isinstance(initial, Mapping) or not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise MatchReconstructionError("INVALID_HAND_RECONSTRUCTION_INPUT", {"hand_index": hand_index})
        state = deepcopy(dict(initial))
        _validate_state(state, record, hand, hand_index, -1)
        public_state = deepcopy(dict(hand.get("initial_public_state", {})))
        if public_state != _public_state(state, hand_index=hand_index, sequence=-1):
            raise MatchReconstructionError("PUBLIC_STATE_MISMATCH", {"hand_index": hand_index, "sequence": -1})

        assignments = hand.get("seat_assignments", {})
        if previous_transition is not None:
            expected_fields = {
                "round_wind": previous_transition.get("next_round_wind"),
                "hand_number": previous_transition.get("next_hand_number"),
                "honba": previous_transition.get("next_honba"),
                "riichi_deposits": previous_transition.get("carried_riichi_deposits"),
            }
            mismatches = [key for key, value in expected_fields.items() if state.get(key) != value]
            if state.get("dealer") != "east":
                mismatches.append("dealer")
            next_dealer = previous_transition.get("next_dealer")
            if assignments.get("east") != previous_assignments.get(next_dealer):
                mismatches.append("dealer_participant")
            previous_scores = previous_transition.get("scores", {})
            players = _players_by_seat(state, hand_index=hand_index, sequence=-1)
            participant_scores = {
                assignments.get(seat): players[seat].get("score") for seat in players
            }
            expected_participant_scores = {
                previous_assignments.get(seat): previous_scores.get(seat) for seat in previous_scores
            }
            if mismatches or participant_scores != expected_participant_scores:
                raise MatchReconstructionError("INTER_HAND_STATE_MISMATCH", {"hand_index": hand_index, "fields": mismatches})

        frames: list[Mapping[str, Any]] = []
        for sequence, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise MatchReconstructionError("INVALID_EVENT", {"hand_index": hand_index, "sequence": sequence})
            authoritative = event.get("authoritative_data", {})
            if not isinstance(authoritative, Mapping):
                raise MatchReconstructionError("INVALID_AUTHORITATIVE_EVENT", {"hand_index": hand_index, "sequence": sequence})
            delta = authoritative.get("state_delta", {})
            if not isinstance(delta, Mapping):
                raise MatchReconstructionError("INVALID_AUTHORITATIVE_STATE_DELTA", {"hand_index": hand_index, "sequence": sequence})
            state = apply_state_delta(state, delta)
            _validate_state(state, record, hand, hand_index, sequence)
            public_data = event.get("public_data", {})
            public_delta = public_data.get("state_delta", {}) if isinstance(public_data, Mapping) else None
            if not isinstance(public_delta, Mapping):
                raise MatchReconstructionError("INVALID_PUBLIC_STATE_DELTA", {"hand_index": hand_index, "sequence": sequence})
            public_state = apply_state_delta(public_state, public_delta)
            if public_state != _public_state(state, hand_index=hand_index, sequence=sequence):
                raise MatchReconstructionError("PUBLIC_STATE_MISMATCH", {"hand_index": hand_index, "sequence": sequence})
            frames.append(_frozen_copy(state))

        final_scores = hand.get("result", {}).get("final_scores", {})
        players = _players_by_seat(state, hand_index=hand_index, sequence=len(events) - 1)
        actual_scores = {seat: players[seat].get("score") for seat in players}
        if actual_scores != final_scores:
            raise MatchReconstructionError("FINAL_STATE_SCORE_MISMATCH", {"hand_index": hand_index})
        transition = record["transitions"][hand_index]
        if state.get("riichi_deposits") != transition.get("carried_riichi_deposits"):
            raise MatchReconstructionError("FINAL_DEPOSIT_MISMATCH", {"hand_index": hand_index})

        reconstructed.append(ReconstructedHand(str(hand["hand_id"]), _frozen_copy(initial), tuple(frames)))
        previous_transition = transition
        previous_assignments = assignments

    result = record.get("result", {})
    if previous_transition.get("scores") != result.get("scores_before_deposit") or previous_transition.get("carried_riichi_deposits") != result.get("remaining_riichi_deposits"):
        raise MatchReconstructionError("MATCH_FINAL_STATE_MISMATCH", {})
    return MatchReconstruction(str(record["match_id"]), tuple(reconstructed))
