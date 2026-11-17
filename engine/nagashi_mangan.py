"""Profile-driven, side-effect-free nagashi-mangan qualification."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from engine.game_state import GameState, MeldKind, SEATS, Seat, TileInstance
from engine.rule_profiles import ResolvedRuleProfile, require_profile_match
from engine.special_hand_end import (
    EvaluationTiming,
    HandEndCandidate,
    HandEndTrigger,
    ReasonCode,
    ResultFamily,
    SpecialHandEndError,
)


class NagashiManganError(ValueError):
    """A structured refusal to evaluate an incomplete or inconsistent input."""

    def __init__(self, code: str, details: Mapping[str, Any] = {}):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class SeatQualification:
    seat: Seat
    qualifies: bool
    discard_count: int
    all_discards_terminal_or_honor: bool
    any_discard_called: bool
    has_open_meld: bool
    declared_tenpai: bool | None
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class NagashiManganDetection:
    adopted: bool
    evaluations: Mapping[Seat, SeatQualification]
    qualifier_seats: tuple[Seat, ...]
    candidate: HandEndCandidate | None

    def __post_init__(self) -> None:
        if set(self.evaluations) != (set(SEATS) if self.adopted else set()):
            raise NagashiManganError("INCOMPLETE_NAGASHI_EVALUATIONS")
        expected = tuple(
            seat for seat in SEATS
            if seat in self.evaluations and self.evaluations[seat].qualifies
        )
        if self.qualifier_seats != expected:
            raise NagashiManganError("NAGASHI_QUALIFIER_MISMATCH")
        if (self.candidate is None) is bool(self.qualifier_seats):
            raise NagashiManganError("NAGASHI_CANDIDATE_MISMATCH")
        object.__setattr__(self, "evaluations", MappingProxyType(dict(self.evaluations)))


_DIMENSION = "hand.special.nagashi_mangan"
_DEFINITION = "game.hand.end.special.nagashi_mangan_definition"
_PROFILE = "game.hand.end.special.nagashi_mangan_profile"
_QUALIFICATION_FIELDS = {
    "end_condition",
    "all_own_discards_are_terminals_or_honors",
    "any_own_discard_called_by_another_player",
    "own_open_meld_disqualifies",
    "tenpai_required",
}
_STANDARD_QUALIFICATION = {
    "end_condition": "exhaustive_draw",
    "all_own_discards_are_terminals_or_honors": True,
    "any_own_discard_called_by_another_player": False,
    "own_open_meld_disqualifies": False,
    "tenpai_required": False,
}


def _dimension(profile: ResolvedRuleProfile) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    try:
        entry = profile.dimensions[_DIMENSION]
        value = entry["value"]
        concepts = tuple(entry["concept_ids"])
    except (KeyError, TypeError) as error:
        raise NagashiManganError(
            "NAGASHI_MANGAN_PROFILE_INCOMPLETE",
            {"profile_id": profile.profile_id, "dimension_id": _DIMENSION},
        ) from error
    if not isinstance(value, Mapping) or type(value.get("adopted")) is not bool:
        raise NagashiManganError("NAGASHI_MANGAN_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id})
    if not all(isinstance(item, str) and item for item in concepts) or len(concepts) != len(set(concepts)):
        raise NagashiManganError("NAGASHI_MANGAN_PROFILE_INCOMPLETE", {"profile_id": profile.profile_id})
    return value, concepts


def _qualification(value: Mapping[str, Any], profile: ResolvedRuleProfile) -> Mapping[str, Any]:
    rule = value.get("qualification")
    if not isinstance(rule, Mapping) or set(rule) != _QUALIFICATION_FIELDS:
        raise NagashiManganError(
            "NAGASHI_MANGAN_PROFILE_INCOMPLETE",
            {"profile_id": profile.profile_id, "missing_boundary": "qualification"},
        )
    if dict(rule) != _STANDARD_QUALIFICATION:
        raise NagashiManganError(
            "UNSUPPORTED_NAGASHI_MANGAN_QUALIFICATION",
            {"profile_id": profile.profile_id, "value": dict(rule)},
        )
    return rule


def _is_terminal_or_honor(tile: TileInstance) -> bool:
    return tile.tile_type.endswith("z") or tile.tile_type[0] in {"1", "9"}


def _called_discards(state: GameState) -> Mapping[Seat, tuple[TileInstance, ...]]:
    called: dict[Seat, list[TileInstance]] = {seat: [] for seat in SEATS}
    for player in state.players:
        for meld in player.melds:
            if meld.called_from is None:
                continue
            index = -2 if meld.kind is MeldKind.ADDED_KAN else -1
            called[meld.called_from].append(meld.tiles[index])
    return MappingProxyType({seat: tuple(tiles) for seat, tiles in called.items()})


def detect_nagashi_mangan(
    state: GameState,
    profile: ResolvedRuleProfile,
    *,
    trigger_event_id: str,
) -> NagashiManganDetection:
    """Evaluate every seat exactly once at an exhaustive-draw boundary."""

    require_profile_match(profile, state, error_type=NagashiManganError)
    if not trigger_event_id:
        raise NagashiManganError("INVALID_NAGASHI_TRIGGER")
    if state.live_wall:
        raise NagashiManganError(
            "NAGASHI_MANGAN_EVALUATION_TOO_EARLY",
            {"live_wall_count": len(state.live_wall)},
        )

    value, concepts = _dimension(profile)
    if value["adopted"] is False:
        return NagashiManganDetection(False, {}, (), None)
    _qualification(value, profile)
    if not {_DEFINITION, _PROFILE} <= set(concepts):
        raise NagashiManganError(
            "NAGASHI_MANGAN_PROFILE_INCOMPLETE",
            {"profile_id": profile.profile_id, "missing_boundary": "qualified_concepts"},
        )

    called = _called_discards(state)
    evaluations: dict[Seat, SeatQualification] = {}
    for seat in SEATS:
        player = state.players_by_seat[seat]
        all_discards = tuple(discard.tile for discard in player.river) + called[seat]
        all_terminal_or_honor = bool(all_discards) and all(
            _is_terminal_or_honor(tile) for tile in all_discards
        )
        any_called = bool(called[seat])
        has_open_meld = any(meld.kind is not MeldKind.CONCEALED_KAN for meld in player.melds)
        failures = []
        if not all_discards:
            failures.append("NO_DISCARDS")
        elif not all_terminal_or_honor:
            failures.append("NON_TERMINAL_OR_HONOR_DISCARD")
        if any_called:
            failures.append("OWN_DISCARD_CALLED")
        evaluations[seat] = SeatQualification(
            seat=seat,
            qualifies=not failures,
            discard_count=len(all_discards),
            all_discards_terminal_or_honor=all_terminal_or_honor,
            any_discard_called=any_called,
            has_open_meld=has_open_meld,
            declared_tenpai=None,
            failure_reasons=tuple(failures),
        )

    qualifiers = tuple(seat for seat in SEATS if evaluations[seat].qualifies)
    candidate = None
    if qualifiers:
        try:
            candidate = HandEndCandidate(
                result_family=ResultFamily.NAGASHI_MANGAN,
                reason_code=ReasonCode.NAGASHI_MANGAN,
                ordinary_winner_seats=(),
                qualifier_seats=qualifiers,
                declared_tenpai_seats=(),
                trigger=HandEndTrigger(trigger_event_id, EvaluationTiming.WALL_EXHAUSTED),
                rule_profile_id=profile.profile_id,
                rule_snapshot_id=profile.snapshot_id,
                applied_concept_ids=concepts,
            )
        except SpecialHandEndError as error:
            raise NagashiManganError(error.code, error.details) from error
    return NagashiManganDetection(True, evaluations, qualifiers, candidate)
