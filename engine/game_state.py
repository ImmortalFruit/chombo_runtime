"""Immutable v0.1 single-hand state contract.

The state stores the current physical location of every tile instance. Event
history and response intents deliberately belong to the later hand-log
contract, not to ``GameState``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class Seat(str, Enum):
    EAST = "east"
    SOUTH = "south"
    WEST = "west"
    NORTH = "north"


SEATS = tuple(Seat)
VALID_TILE_TYPES = frozenset(
    [f"{rank}{suit}" for suit in "mps" for rank in range(1, 10)]
    + [f"{rank}z" for rank in range(1, 8)]
)


class MeldKind(str, Enum):
    CHI = "chi"
    PON = "pon"
    OPEN_KAN = "open_kan"
    CONCEALED_KAN = "concealed_kan"
    ADDED_KAN = "added_kan"


class FuritenState(str, Enum):
    NONE = "none"
    DISCARD = "discard"
    TEMPORARY = "temporary"
    RIICHI = "riichi"


class RiichiState(str, Enum):
    NONE = "none"
    DECLARED = "declared"
    ESTABLISHED = "established"


class StateValidationError(ValueError):
    """A structured state-contract violation."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class TileInstance:
    tile_id: str
    tile_type: str
    is_red: bool = False

    def __post_init__(self) -> None:
        if not self.tile_id:
            raise StateValidationError("INVALID_TILE", {"field": "tile_id"})
        if self.tile_type not in VALID_TILE_TYPES:
            raise StateValidationError(
                "INVALID_TILE", {"tile_id": self.tile_id, "tile_type": self.tile_type}
            )
        if self.is_red and self.tile_type not in {"5m", "5p", "5s"}:
            raise StateValidationError(
                "INVALID_RED_TILE", {"tile_id": self.tile_id, "tile_type": self.tile_type}
            )


@dataclass(frozen=True)
class Discard:
    tile: TileInstance
    tsumogiri: bool = False
    riichi_declaration: bool = False


@dataclass(frozen=True)
class Meld:
    kind: MeldKind
    tiles: tuple[TileInstance, ...]
    called_from: Seat | None = None

    def __post_init__(self) -> None:
        expected = 4 if "kan" in self.kind.value else 3
        if len(self.tiles) != expected:
            raise StateValidationError(
                "INVALID_MELD_SIZE", {"kind": self.kind.value, "expected": expected}
            )
        if self.kind is MeldKind.CONCEALED_KAN and self.called_from is not None:
            raise StateValidationError(
                "INVALID_MELD_SOURCE", {"kind": self.kind.value}
            )
        if self.kind is not MeldKind.CONCEALED_KAN and self.called_from is None:
            raise StateValidationError(
                "MISSING_MELD_SOURCE", {"kind": self.kind.value}
            )


@dataclass(frozen=True)
class PlayerState:
    seat: Seat
    concealed_hand: tuple[TileInstance, ...]
    score: int
    melds: tuple[Meld, ...] = ()
    river: tuple[Discard, ...] = ()
    riichi: RiichiState = RiichiState.NONE
    furiten: FuritenState = FuritenState.NONE
    ippatsu_active: bool = False

    def __post_init__(self) -> None:
        if self.ippatsu_active and self.riichi is not RiichiState.ESTABLISHED:
            raise StateValidationError(
                "INVALID_IPPATSU_STATE", {"seat": self.seat.value}
            )


@dataclass(frozen=True)
class GameState:
    """Complete immutable state of one v0.1 four-player hand."""

    live_wall: tuple[TileInstance, ...]
    dead_wall: tuple[TileInstance, ...]
    players: tuple[PlayerState, ...]
    current_turn: Seat
    dealer: Seat
    round_wind: Seat
    hand_number: int
    honba: int
    riichi_deposits: int
    kan_count: int
    dora_indicator_ids: tuple[str, ...]
    rule_profile_id: str
    rule_snapshot_id: str

    def __post_init__(self) -> None:
        _validate_game_state(self)

    @property
    def players_by_seat(self) -> Mapping[Seat, PlayerState]:
        return MappingProxyType({player.seat: player for player in self.players})

    def tile_locations(self) -> Mapping[str, str]:
        locations: dict[str, str] = {}
        for location, tile in _located_tiles(self):
            locations[tile.tile_id] = location
        return MappingProxyType(locations)


def _located_tiles(state: GameState) -> Iterable[tuple[str, TileInstance]]:
    for index, tile in enumerate(state.live_wall):
        yield f"live_wall[{index}]", tile
    for index, tile in enumerate(state.dead_wall):
        yield f"dead_wall[{index}]", tile
    for player in state.players:
        for index, tile in enumerate(player.concealed_hand):
            yield f"players.{player.seat.value}.concealed_hand[{index}]", tile
        for meld_index, meld in enumerate(player.melds):
            for index, tile in enumerate(meld.tiles):
                yield f"players.{player.seat.value}.melds[{meld_index}].tiles[{index}]", tile
        for index, discard in enumerate(player.river):
            yield f"players.{player.seat.value}.river[{index}]", discard.tile


def _validate_game_state(state: GameState) -> None:
    seats = [player.seat for player in state.players]
    if len(state.players) != 4 or set(seats) != set(SEATS):
        raise StateValidationError(
            "INVALID_PLAYER_SEATS", {"seats": [seat.value for seat in seats]}
        )
    if len(state.dead_wall) != 14:
        raise StateValidationError(
            "INVALID_DEAD_WALL_SIZE", {"actual": len(state.dead_wall), "expected": 14}
        )

    located = tuple(_located_tiles(state))
    ids = [tile.tile_id for _, tile in located]
    duplicates = sorted(tile_id for tile_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise StateValidationError("DUPLICATE_TILE_LOCATION", {"tile_ids": duplicates})
    if len(ids) != 136:
        raise StateValidationError(
            "INVALID_TILE_COUNT", {"actual": len(ids), "expected": 136}
        )

    type_counts = Counter(tile.tile_type for _, tile in located)
    invalid_counts = {
        tile_type: type_counts.get(tile_type, 0)
        for tile_type in sorted(VALID_TILE_TYPES)
        if type_counts.get(tile_type, 0) != 4
    }
    if invalid_counts:
        raise StateValidationError("INVALID_TILE_COMPOSITION", invalid_counts)

    dead_wall_ids = {tile.tile_id for tile in state.dead_wall}
    if not state.dora_indicator_ids or not set(state.dora_indicator_ids) <= dead_wall_ids:
        raise StateValidationError(
            "INVALID_DORA_INDICATORS", {"tile_ids": list(state.dora_indicator_ids)}
        )
    if len(set(state.dora_indicator_ids)) != len(state.dora_indicator_ids):
        raise StateValidationError("DUPLICATE_DORA_INDICATOR", {})

    if state.hand_number < 1 or state.hand_number > 4:
        raise StateValidationError("INVALID_HAND_NUMBER", {"value": state.hand_number})
    if state.honba < 0 or state.riichi_deposits < 0 or state.kan_count < 0:
        raise StateValidationError(
            "INVALID_COUNTER",
            {
                "honba": state.honba,
                "riichi_deposits": state.riichi_deposits,
                "kan_count": state.kan_count,
            },
        )
    actual_kan_count = sum(
        "kan" in meld.kind.value for player in state.players for meld in player.melds
    )
    if state.kan_count != actual_kan_count:
        raise StateValidationError(
            "KAN_COUNT_MISMATCH", {"declared": state.kan_count, "actual": actual_kan_count}
        )
    if not state.rule_profile_id or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", state.rule_snapshot_id
    ):
        raise StateValidationError("INVALID_RULE_SNAPSHOT", {})
