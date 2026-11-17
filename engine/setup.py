"""Deterministic v0.1 wall, deal, and initial-state generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, MutableSequence

from engine.game_state import GameState, PlayerState, SEATS, Seat, TileInstance
from engine.rule_profiles import ResolvedRuleProfile


RNG_ID = "chombo.sha256_counter.v1"
SHUFFLE_SPEC_ID = "fisher_yates_rejection.v1"
DORA_INDICATOR_INDEXES = (4, 6, 8, 10, 12)


class SetupError(ValueError):
    """A resolved profile cannot supply a valid v0.1 starting state."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class _Sha256CounterRandom:
    """Versioned byte stream used only by the v0.1 shuffle specification."""

    def __init__(self, seed: str):
        if not isinstance(seed, str) or not seed:
            raise SetupError("INVALID_SEED", {"expected": "non-empty UTF-8 string"})
        self._key = hashlib.sha256(
            b"chombo.setup.v0.1\x00" + seed.encode("utf-8")
        ).digest()
        self._counter = 0
        self._buffer = b""

    def _bytes(self, count: int) -> bytes:
        while len(self._buffer) < count:
            block = hashlib.sha256(
                self._key + self._counter.to_bytes(16, "big")
            ).digest()
            self._counter += 1
            self._buffer += block
        result, self._buffer = self._buffer[:count], self._buffer[count:]
        return result

    def below(self, bound: int) -> int:
        if bound <= 0:
            raise ValueError("bound must be positive")
        byte_count = max(1, (bound.bit_length() + 7) // 8)
        sample_range = 1 << (byte_count * 8)
        acceptance_limit = sample_range - (sample_range % bound)
        while True:
            candidate = int.from_bytes(self._bytes(byte_count), "big")
            if candidate < acceptance_limit:
                return candidate % bound


def _shuffle(items: MutableSequence[TileInstance], random: _Sha256CounterRandom) -> None:
    for index in range(len(items) - 1, 0, -1):
        swap_index = random.below(index + 1)
        items[index], items[swap_index] = items[swap_index], items[index]


def _dimension_value(profile: ResolvedRuleProfile, dimension_id: str) -> Any:
    try:
        return profile.value(dimension_id)
    except ValueError as error:
        raise SetupError(
            "RULE_PROFILE_INCOMPLETE", {"dimension_id": dimension_id}
        ) from error


def _tile_set(red_five_counts: Mapping[str, Any]) -> list[TileInstance]:
    keys = {"5m", "5p", "5s"}
    actual = {tile_type: red_five_counts.get(tile_type) for tile_type in keys}
    if (
        set(red_five_counts) != {*keys, "total"}
        or any(type(count) is not int or count not in (0, 1) for count in actual.values())
        or red_five_counts.get("total") != sum(actual.values())
    ):
        raise SetupError(
            "UNSUPPORTED_RED_FIVE_COMPOSITION",
            {"supported": "zero or one red copy per numbered suit", "actual": dict(red_five_counts)},
        )

    tiles: list[TileInstance] = []
    for suit in "mps":
        for rank in range(1, 10):
            tile_type = f"{rank}{suit}"
            for copy in range(4):
                tiles.append(
                    TileInstance(
                        tile_id=f"{tile_type}-{copy}",
                        tile_type=tile_type,
                        is_red=rank == 5 and copy < actual[tile_type],
                    )
                )
    for rank in range(1, 8):
        tile_type = f"{rank}z"
        for copy in range(4):
            tiles.append(TileInstance(f"{tile_type}-{copy}", tile_type))
    return tiles


@dataclass(frozen=True)
class SetupResult:
    seed: str
    rng_id: str
    shuffle_spec_id: str
    state: GameState

    def reproduction_metadata(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "seed": self.seed,
                "rng_id": self.rng_id,
                "shuffle_spec_id": self.shuffle_spec_id,
                "rule_profile_id": self.state.rule_profile_id,
                "rule_snapshot_id": self.state.rule_snapshot_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the authoritative, JSON-safe v0.1 starting-state document."""
        state = self.state

        def tile_document(tile: TileInstance) -> dict[str, Any]:
            return {
                "tile_id": tile.tile_id,
                "tile_type": tile.tile_type,
                "is_red": tile.is_red,
            }

        return {
            "schema_version": 1,
            "reproduction": dict(self.reproduction_metadata()),
            "initial_state": {
                "live_wall": [tile_document(tile) for tile in state.live_wall],
                "dead_wall": [tile_document(tile) for tile in state.dead_wall],
                "players": [
                    {
                        "seat": player.seat.value,
                        "concealed_hand": [
                            tile_document(tile) for tile in player.concealed_hand
                        ],
                        "melds": [],
                        "river": [],
                        "score": player.score,
                        "riichi": player.riichi.value,
                        "furiten": player.furiten.value,
                        "ippatsu_active": player.ippatsu_active,
                    }
                    for player in state.players
                ],
                "current_turn": state.current_turn.value,
                "dealer": state.dealer.value,
                "round_wind": state.round_wind.value,
                "hand_number": state.hand_number,
                "honba": state.honba,
                "riichi_deposits": state.riichi_deposits,
                "kan_count": state.kan_count,
                "dora_indicator_ids": list(state.dora_indicator_ids),
                "rule_profile_id": state.rule_profile_id,
                "rule_snapshot_id": state.rule_snapshot_id,
            },
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def create_initial_state(
    seed: str,
    profile: ResolvedRuleProfile,
    *,
    dealer: Seat = Seat.EAST,
    round_wind: Seat = Seat.EAST,
    hand_number: int = 1,
) -> SetupResult:
    """Create the deal-complete state immediately before the dealer's first draw.

    The shuffled sequence is consumed from index zero. Thirteen rounds of
    East/South/West/North dealing consume the first 52 tiles, ``live_wall[0]``
    is therefore the dealer's opening draw, and the final 14 tiles are the
    dead wall. Dead-wall indexes 4, 6, 8, 10, and 12 are the ordered indicator
    slots; only index 4 is initially revealed.
    """
    procedure = _dimension_value(profile, "match.start.procedure")
    if procedure != "virtual_seeded_seat_dealer_wall_and_deal_generation":
        raise SetupError("UNSUPPORTED_START_PROCEDURE", {"value": procedure})

    start = _dimension_value(profile, "match.start.points")
    if not isinstance(start, Mapping):
        raise SetupError("INVALID_START_POINTS", {"value": start})
    red_fives = _dimension_value(profile, "tile.red_five.composition")
    if not isinstance(red_fives, Mapping):
        raise SetupError("UNSUPPORTED_RED_FIVE_COMPOSITION", {"actual": red_fives})

    tiles = _tile_set(red_fives)
    _shuffle(tiles, _Sha256CounterRandom(seed))
    drawable, dead_wall = tiles[:-14], tuple(tiles[-14:])

    hands: dict[Seat, list[TileInstance]] = {seat: [] for seat in SEATS}
    for index, tile in enumerate(drawable[:52]):
        hands[SEATS[index % 4]].append(tile)

    try:
        score = int(start["points_per_player"])
        honba = int(start["honba"])
        deposit_points = int(start["riichi_deposit_points"])
    except (KeyError, TypeError, ValueError) as error:
        raise SetupError("INVALID_START_POINTS", {"value": dict(start)}) from error
    if deposit_points % 1000:
        raise SetupError(
            "INVALID_START_POINTS", {"riichi_deposit_points": deposit_points}
        )

    state = GameState(
        live_wall=tuple(drawable[52:]),
        dead_wall=dead_wall,
        players=tuple(
            PlayerState(seat=seat, concealed_hand=tuple(hands[seat]), score=score)
            for seat in SEATS
        ),
        current_turn=dealer,
        dealer=dealer,
        round_wind=round_wind,
        hand_number=hand_number,
        honba=honba,
        riichi_deposits=deposit_points // 1000,
        kan_count=0,
        dora_indicator_ids=(dead_wall[DORA_INDICATOR_INDEXES[0]].tile_id,),
        rule_profile_id=profile.profile_id,
        rule_snapshot_id=profile.snapshot_id,
    )
    return SetupResult(seed, RNG_ID, SHUFFLE_SPEC_ID, state)
