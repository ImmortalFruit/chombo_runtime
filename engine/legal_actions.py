"""Deterministic legal-action enumeration from state and resolved rule values."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from itertools import combinations, product
from typing import Any, Mapping

from engine.actions import Action, ActionKind
from engine.game_state import GameState, MeldKind, PlayerState, RiichiState, SEATS, Seat, TileInstance
from engine.hand_completion import analyze_hand
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile


class LegalActionError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class ActionPhase(str, Enum):
    AWAITING_DRAW = "awaiting_draw"
    AWAITING_DISCARD = "awaiting_discard"
    AWAITING_RESPONSES = "awaiting_responses"
    AWAITING_REPLACEMENT_DRAW = "awaiting_replacement_draw"


@dataclass(frozen=True)
class LegalActionContext:
    phase: ActionPhase
    source_event_id: str | None = None
    discarded_tile_id: str | None = None
    discarder: Seat | None = None
    last_draw: bool = False
    last_discard: bool = False
    qualified_tsumo: bool = False
    qualified_ron_by_seat: Mapping[Seat, bool] | None = None
    preceding_call: Action | None = None
    acquired_tile_id: str | None = None
    qualified_riichi_kan_tile_groups: frozenset[tuple[str, ...]] = frozenset()
    winning_event: str = "ordinary"
    pending_added_kan: Action | None = None
    suppress_riichi: bool = False
    suppress_calls: bool = False

    def __post_init__(self) -> None:
        response = self.phase is ActionPhase.AWAITING_RESPONSES
        fields_present = (
            self.source_event_id is not None
            and self.discarded_tile_id is not None
            and self.discarder is not None
        )
        if response != fields_present:
            raise LegalActionError("INVALID_ACTION_CONTEXT", {"phase": self.phase.value})
        if self.qualified_tsumo and self.acquired_tile_id is None:
            raise LegalActionError("MISSING_ACQUIRED_TILE", {})
        if self.preceding_call is not None and self.preceding_call.kind not in {
            ActionKind.CHI,
            ActionKind.PON,
        }:
            raise LegalActionError("INVALID_PRECEDING_CALL", {})
        if self.pending_added_kan is not None:
            if not response or self.pending_added_kan.kind is not ActionKind.ADDED_KAN:
                raise LegalActionError("INVALID_PENDING_ADDED_KAN", {})


def _action_id(kind: ActionKind, actor: Seat, *parts: object) -> str:
    suffix = ":".join(str(part) for part in parts if part is not None)
    return f"legal:{actor.value}:{kind.value}" + (f":{suffix}" if suffix else "")


def _player(state: GameState, seat: Seat) -> PlayerState:
    return state.players_by_seat[seat]


def _by_type(tiles: tuple[TileInstance, ...]) -> dict[str, list[TileInstance]]:
    result: dict[str, list[TileInstance]] = defaultdict(list)
    for tile in tiles:
        result[tile.tile_type].append(tile)
    for values in result.values():
        values.sort(key=lambda tile: tile.tile_id)
    return result


def _notation(tiles: tuple[TileInstance, ...]) -> str:
    grouped: dict[str, list[str]] = {suit: [] for suit in "mpsz"}
    for tile in tiles:
        grouped[tile.tile_type[1]].append(tile.tile_type[0])
    return "".join("".join(sorted(grouped[suit])) + suit for suit in "mpsz" if grouped[suit])


def _next_seat(seat: Seat) -> Seat:
    return SEATS[(SEATS.index(seat) + 1) % len(SEATS)]


def _draw_actions(state: GameState, context: LegalActionContext) -> list[Action]:
    wall = state.dead_wall if context.phase is ActionPhase.AWAITING_REPLACEMENT_DRAW else state.live_wall
    if not wall:
        return []
    # Replacement-tile positioning is applied by State Transition; the context
    # carries the already selected source at index zero.
    tile = wall[0]
    return [Action(_action_id(ActionKind.DRAW, state.current_turn, tile.tile_id), ActionKind.DRAW, state.current_turn, (tile.tile_id,))]


def _tile_by_id(state: GameState) -> dict[str, TileInstance]:
    tiles = list(state.live_wall) + list(state.dead_wall)
    for player in state.players:
        tiles.extend(player.concealed_hand)
        tiles.extend(discard.tile for discard in player.river)
        for meld in player.melds:
            tiles.extend(meld.tiles)
    return {tile.tile_id: tile for tile in tiles}


def _post_call_prohibited_types(
    state: GameState, profile: ResolvedRuleProfile, context: LegalActionContext
) -> frozenset[str]:
    call = context.preceding_call
    rule = profile.value("response.post_call_discard_restriction")
    legacy_full_prohibition = rule.get(
        "prohibit_discard_completing_called_meld_with_used_group", False
    )
    prohibit_same = legacy_full_prohibition or rule.get("same_tile", False)
    prohibit_other_end = legacy_full_prohibition or rule.get(
        "other_end_of_called_sequence", False
    )
    if call is None or not (prohibit_same or prohibit_other_end):
        return frozenset()
    tiles = _tile_by_id(state)
    try:
        used_types = [tiles[tile_id].tile_type for tile_id in call.tile_ids]
    except KeyError as error:
        raise LegalActionError("CALL_TILE_NOT_IN_STATE", {"tile_id": str(error)}) from error
    claimed_type = tiles[call.claimed_tile_id].tile_type if call.claimed_tile_id else None
    if call.kind is ActionKind.PON:
        return frozenset({used_types[0]}) if prohibit_same else frozenset()
    suit = used_types[0][1]
    if suit == "z" or any(tile_type[1] != suit for tile_type in used_types):
        raise LegalActionError("INVALID_CHI_SHAPE", {"tile_types": used_types})
    ranks = [int(tile_type[0]) for tile_type in used_types]
    completing_types = frozenset(
        f"{candidate}{suit}"
        for candidate in range(1, 10)
        if sorted(ranks + [candidate]) in (
            [start, start + 1, start + 2] for start in range(1, 8)
        )
    )
    prohibited = set()
    if prohibit_same and claimed_type is not None:
        prohibited.add(claimed_type)
    if prohibit_other_end:
        prohibited.update(completing_types - ({claimed_type} if claimed_type else set()))
    return frozenset(prohibited)


def _discard_actions(
    state: GameState,
    player: PlayerState,
    profile: ResolvedRuleProfile,
    context: LegalActionContext,
) -> list[Action]:
    prohibited_types = _post_call_prohibited_types(state, profile, context)
    actions = []
    for tile in sorted(player.concealed_hand, key=lambda item: item.tile_id):
        if player.riichi is RiichiState.ESTABLISHED and tile.tile_id != context.acquired_tile_id:
            continue
        if tile.tile_type in prohibited_types:
            continue
        actions.append(Action(_action_id(ActionKind.DISCARD, player.seat, tile.tile_id), ActionKind.DISCARD, player.seat, (tile.tile_id,)))
    return actions


def _kan_actions(state: GameState, player: PlayerState, profile: ResolvedRuleProfile, context: LegalActionContext) -> list[Action]:
    limit = profile.value("kan.count_limit")
    boundary = profile.value("kan.last_draw_restrictions")
    if state.kan_count >= limit["established_kans_max"] or (context.last_draw and not boundary["kan_on_last_draw"]):
        return []
    by_type = _by_type(player.concealed_hand)
    actions: list[Action] = []
    for tiles in by_type.values():
        if len(tiles) == 4:
            ids = tuple(tile.tile_id for tile in tiles)
            if (
                player.riichi is not RiichiState.ESTABLISHED
                or ids in context.qualified_riichi_kan_tile_groups
            ):
                actions.append(Action(_action_id(ActionKind.CONCEALED_KAN, player.seat, *ids), ActionKind.CONCEALED_KAN, player.seat, ids))
    if player.riichi is not RiichiState.ESTABLISHED:
        for index, meld in enumerate(player.melds):
            if meld.kind is not MeldKind.PON:
                continue
            tile_type = meld.tiles[0].tile_type
            for tile in by_type.get(tile_type, []):
                actions.append(Action(_action_id(ActionKind.ADDED_KAN, player.seat, index, tile.tile_id), ActionKind.ADDED_KAN, player.seat, (tile.tile_id,), target_meld_index=index))
    return actions


def _riichi_actions(player: PlayerState, profile: ResolvedRuleProfile, context: LegalActionContext) -> list[Action]:
    restriction = profile.value("kan.last_draw_restrictions")
    eligibility = profile.value("riichi.score_eligibility")
    minimum_points = eligibility["minimum_points"]
    if (
        player.riichi is not RiichiState.NONE
        or any(meld.kind is not MeldKind.CONCEALED_KAN for meld in player.melds)
        or (minimum_points is not None and player.score < minimum_points)
        or (context.last_draw and not restriction["riichi_on_last_draw"])
    ):
        return []
    actions = []
    for tile in sorted(player.concealed_hand, key=lambda item: item.tile_id):
        remaining = list(player.concealed_hand)
        remaining.remove(tile)
        if player.melds:
            from engine.hand_completion import standard_wait_tiles_with_fixed_melds

            fixed_tiles = tuple(tile for meld in player.melds for tile in meld.tiles)
            tenpai = bool(
                standard_wait_tiles_with_fixed_melds(
                    _notation(tuple(remaining)),
                    fixed_meld_count=len(player.melds),
                    fixed_tiles=_notation(fixed_tiles),
                )
            )
        else:
            tenpai = analyze_hand(_notation(tuple(remaining)))["tenpai"]
        if tenpai:
            actions.append(Action(_action_id(ActionKind.RIICHI, player.seat, tile.tile_id), ActionKind.RIICHI, player.seat, (tile.tile_id,)))
    return actions


def _response_actions(state: GameState, profile: ResolvedRuleProfile, context: LegalActionContext) -> dict[Seat, tuple[Action, ...]]:
    assert context.discarder is not None and context.discarded_tile_id is not None and context.source_event_id is not None
    discarder = _player(state, context.discarder)
    if context.pending_added_kan is not None:
        matching = [tile for tile in discarder.concealed_hand if tile.tile_id == context.discarded_tile_id]
        if len(matching) != 1:
            raise LegalActionError("ADDED_KAN_TILE_NOT_IN_HAND", {"tile_id": context.discarded_tile_id})
        claimed = matching[0]
    else:
        matching = [discard.tile for discard in discarder.river if discard.tile.tile_id == context.discarded_tile_id]
        if len(matching) != 1:
            raise LegalActionError("DISCARDED_TILE_NOT_IN_RIVER", {"tile_id": context.discarded_tile_id})
        claimed = matching[0]
    last_rule = profile.value("kan.last_draw_restrictions")
    limit = profile.value("kan.count_limit")
    result: dict[Seat, tuple[Action, ...]] = {}
    for seat in SEATS:
        if seat is context.discarder:
            continue
        player = _player(state, seat)
        actions = [Action(_action_id(ActionKind.PASS, seat, context.source_event_id), ActionKind.PASS, seat, source_event_id=context.source_event_id)]
        if (context.qualified_ron_by_seat or {}).get(seat, False):
            actions.append(Action(_action_id(ActionKind.RON, seat, context.source_event_id), ActionKind.RON, seat, claimed_tile_id=claimed.tile_id, target_seat=context.discarder, source_event_id=context.source_event_id))
        if context.pending_added_kan is not None:
            result[seat] = tuple(sorted(actions, key=lambda action: action.action_id))
            continue
        calls_allowed = (
            not context.suppress_calls
            and (not context.last_discard or last_rule["calls_on_last_discard"])
        )
        if calls_allowed and player.riichi is not RiichiState.ESTABLISHED:
            by_type = _by_type(player.concealed_hand)
            same = by_type.get(claimed.tile_type, [])
            if len(same) >= 2:
                for selected in combinations(same, 2):
                    ids = tuple(tile.tile_id for tile in selected)
                    actions.append(Action(_action_id(ActionKind.PON, seat, *ids, claimed.tile_id), ActionKind.PON, seat, ids, claimed.tile_id, context.discarder, context.source_event_id))
            if len(same) >= 3 and state.kan_count < limit["established_kans_max"]:
                ids = tuple(tile.tile_id for tile in same[:3])
                actions.append(Action(_action_id(ActionKind.OPEN_KAN, seat, *ids, claimed.tile_id), ActionKind.OPEN_KAN, seat, ids, claimed.tile_id, context.discarder, context.source_event_id))
            if seat is _next_seat(context.discarder) and claimed.tile_type[1] in "mps":
                rank = int(claimed.tile_type[0])
                suit = claimed.tile_type[1]
                for ranks in ((rank - 2, rank - 1), (rank - 1, rank + 1), (rank + 1, rank + 2)):
                    if min(ranks) < 1 or max(ranks) > 9:
                        continue
                    pools = [by_type.get(f"{candidate}{suit}", []) for candidate in ranks]
                    if all(pools):
                        for pair in product(*pools):
                            ids = tuple(tile.tile_id for tile in pair)
                            actions.append(Action(_action_id(ActionKind.CHI, seat, *ids, claimed.tile_id), ActionKind.CHI, seat, ids, claimed.tile_id, context.discarder, context.source_event_id))
        result[seat] = tuple(sorted(actions, key=lambda action: action.action_id))
    return result


def generate_legal_actions(state: GameState, profile: ResolvedRuleProfile, context: LegalActionContext) -> Mapping[Seat, tuple[Action, ...]]:
    """Return every legal intent for the explicit phase in stable order."""
    require_profile_match(profile, state, error_type=LegalActionError)
    if context.phase in {ActionPhase.AWAITING_DRAW, ActionPhase.AWAITING_REPLACEMENT_DRAW}:
        return {state.current_turn: tuple(_draw_actions(state, context))}
    if context.phase is ActionPhase.AWAITING_RESPONSES:
        return _response_actions(state, profile, context)
    player = _player(state, state.current_turn)
    if context.preceding_call is not None and context.preceding_call.actor is not player.seat:
        raise LegalActionError(
            "PRECEDING_CALL_ACTOR_MISMATCH",
            {"actor": context.preceding_call.actor.value, "turn": player.seat.value},
        )
    actions = _discard_actions(state, player, profile, context)
    # Chi and pon are followed by a discard without an ordinary draw.  Kan
    # declarations (including upgrading an existing pon) belong to an own
    # draw turn, so they must not be offered inside this post-call window.
    if context.preceding_call is None:
        actions.extend(_kan_actions(state, player, profile, context))
        if not context.suppress_riichi:
            actions.extend(_riichi_actions(player, profile, context))
    if context.preceding_call is None and context.qualified_tsumo:
        tile = next(
            (item for item in player.concealed_hand if item.tile_id == context.acquired_tile_id),
            None,
        )
        if tile is None:
            raise LegalActionError(
                "ACQUIRED_TILE_NOT_IN_HAND", {"tile_id": context.acquired_tile_id}
            )
        actions.append(Action(_action_id(ActionKind.TSUMO, player.seat, tile.tile_id), ActionKind.TSUMO, player.seat, (tile.tile_id,)))
    return {player.seat: tuple(sorted(actions, key=lambda action: action.action_id))}
