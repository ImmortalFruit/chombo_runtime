"""Response resolution and immutable v0.1 state transitions.

The resolver first validates every submitted intent independently.  Priority is
considered only when all three non-discarding controllers supplied a legal
intent (including pass), and only the adopted result is applied to state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from engine.actions import Action, ActionKind, ActionResult, ActionStatus
from engine.game_state import (
    Discard,
    FuritenState,
    GameState,
    Meld,
    MeldKind,
    PlayerState,
    RiichiState,
    SEATS,
    Seat,
)
from engine.legal_actions import ActionPhase, LegalActionContext, generate_legal_actions
from engine.rule_profiles import ResolvedRuleProfile
from engine.setup import DORA_INDICATOR_INDEXES


class TransitionError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


class ResponseResolutionStatus(str, Enum):
    INPUT_REJECTED = "INPUT_REJECTED"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class ResponseResolution:
    status: ResponseResolutionStatus
    intent_results: Mapping[Seat, ActionResult]
    adopted_actions: tuple[Action, ...] = ()
    next_phase: ActionPhase | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_results", MappingProxyType(dict(self.intent_results)))
        if self.status is ResponseResolutionStatus.INPUT_REJECTED and self.adopted_actions:
            raise TransitionError("REJECTED_RESPONSE_HAS_ADOPTION", {})


@dataclass(frozen=True)
class TransitionResult:
    state: GameState
    applied_actions: tuple[Action, ...]
    next_phase: ActionPhase | None
    preceding_call: Action | None = None
    acquired_tile_id: str | None = None
    winning_event: str = "ordinary"
    pending_added_kan: Action | None = None


def _replace_player(state: GameState, changed: PlayerState, **state_changes: Any) -> GameState:
    players = tuple(changed if player.seat is changed.seat else player for player in state.players)
    return replace(state, players=players, **state_changes)


def _next_seat(seat: Seat) -> Seat:
    return SEATS[(SEATS.index(seat) + 1) % len(SEATS)]


def _validate_submitted_intent(action: Action, legal: tuple[Action, ...]) -> ActionResult:
    if action in legal:
        return ActionResult(action, ActionStatus.LEGAL)
    return ActionResult(
        action,
        ActionStatus.ILLEGAL_ACTION,
        "ACTION_NOT_IN_LEGAL_SET",
        {"legal_action_ids": tuple(candidate.action_id for candidate in legal)},
    )


def _claimant_order(discarder: Seat, profile: ResolvedRuleProfile) -> tuple[Seat, ...]:
    semantic_order = profile.value("win.multiple_ron_resolution")["claimant_order"]
    offsets = {"next_player": 1, "opposite_player": 2, "previous_player": 3}
    try:
        return tuple(SEATS[(SEATS.index(discarder) + offsets[item]) % 4] for item in semantic_order)
    except KeyError as error:
        raise TransitionError("UNSUPPORTED_CLAIMANT_ORDER", {"value": str(error)}) from error


def resolve_responses(
    state: GameState,
    profile: ResolvedRuleProfile,
    context: LegalActionContext,
    intents: Mapping[Seat, Action],
) -> ResponseResolution:
    """Validate a complete discard-response window and select its result."""
    if context.phase is not ActionPhase.AWAITING_RESPONSES or context.discarder is None:
        raise TransitionError("INVALID_RESPONSE_CONTEXT", {})
    expected = set(SEATS) - {context.discarder}
    if set(intents) != expected:
        raise TransitionError(
            "INCOMPLETE_RESPONSE_WINDOW",
            {"expected": sorted(seat.value for seat in expected), "actual": sorted(seat.value for seat in intents)},
        )

    legal_by_seat = generate_legal_actions(state, profile, context)
    results = {
        seat: _validate_submitted_intent(action, legal_by_seat[seat])
        for seat, action in intents.items()
    }
    if any(result.status is not ActionStatus.LEGAL for result in results.values()):
        return ResponseResolution(ResponseResolutionStatus.INPUT_REJECTED, results)

    ron_by_seat = {action.actor: action for action in intents.values() if action.kind is ActionKind.RON}
    if ron_by_seat:
        rule = profile.value("win.multiple_ron_resolution")
        ordered = [ron_by_seat[seat] for seat in _claimant_order(context.discarder, profile) if seat in ron_by_seat]
        adopted = tuple(ordered[: rule["maximum_winners"]])
        return ResponseResolution(ResponseResolutionStatus.RESOLVED, results, adopted)

    tiers = profile.value("response.call.priority")
    kind_by_profile_name = {
        "pon": ActionKind.PON,
        "kan_open": ActionKind.OPEN_KAN,
        "chi": ActionKind.CHI,
    }
    non_pass = tuple(action for action in intents.values() if action.kind is not ActionKind.PASS)
    for tier in tiers:
        tier_actions = tuple(
            action for action in non_pass if action.kind in {kind_by_profile_name[name] for name in tier if name != "ron"}
        )
        if len(tier_actions) > 1:
            raise TransitionError(
                "STATE_INVARIANT_VIOLATION",
                {"action_ids": tuple(sorted(action.action_id for action in tier_actions))},
            )
        if tier_actions:
            action = tier_actions[0]
            phase = ActionPhase.AWAITING_REPLACEMENT_DRAW if action.kind is ActionKind.OPEN_KAN else ActionPhase.AWAITING_DISCARD
            return ResponseResolution(ResponseResolutionStatus.RESOLVED, results, (action,), phase)
    return ResponseResolution(ResponseResolutionStatus.RESOLVED, results, (), ActionPhase.AWAITING_DRAW)


def _interrupt_ippatsu(players: tuple[PlayerState, ...]) -> tuple[PlayerState, ...]:
    return tuple(replace(player, ippatsu_active=False) if player.ippatsu_active else player for player in players)


def _add_kan_dora_indicator(
    state: GameState,
    previous_kan_count: int,
    profile: ResolvedRuleProfile | None = None,
) -> GameState:
    """Expose the next indicator at kan establishment, before the rinshan draw."""
    if profile is not None and not profile.value("dora.profile")["kan"]:
        return state
    indicator_slot = DORA_INDICATOR_INDEXES[previous_kan_count + 1] - previous_kan_count
    indicator_id = state.dead_wall[indicator_slot].tile_id
    return replace(
        state,
        dora_indicator_ids=state.dora_indicator_ids + (indicator_id,),
    )


def _apply_call(
    state: GameState,
    action: Action,
    profile: ResolvedRuleProfile | None = None,
) -> GameState:
    assert action.claimed_tile_id is not None and action.target_seat is not None
    actor = state.players_by_seat[action.actor]
    target = state.players_by_seat[action.target_seat]
    matches = [discard for discard in target.river if discard.tile.tile_id == action.claimed_tile_id]
    if len(matches) != 1 or target.river[-1].tile.tile_id != action.claimed_tile_id:
        raise TransitionError("CLAIMED_TILE_NOT_LATEST_DISCARD", {"tile_id": action.claimed_tile_id})
    owned = {tile.tile_id: tile for tile in actor.concealed_hand}
    if any(tile_id not in owned for tile_id in action.tile_ids):
        raise TransitionError("ACTION_TILE_NOT_OWNED", {"action_id": action.action_id})
    meld_kind = {
        ActionKind.CHI: MeldKind.CHI,
        ActionKind.PON: MeldKind.PON,
        ActionKind.OPEN_KAN: MeldKind.OPEN_KAN,
    }[action.kind]
    meld = Meld(meld_kind, tuple(owned[tile_id] for tile_id in action.tile_ids) + (matches[0].tile,), action.target_seat)
    changed_actor = replace(
        actor,
        concealed_hand=tuple(tile for tile in actor.concealed_hand if tile.tile_id not in action.tile_ids),
        melds=actor.melds + (meld,),
    )
    changed_target = replace(target, river=target.river[:-1])
    players = tuple(changed_actor if p.seat is action.actor else changed_target if p.seat is action.target_seat else p for p in state.players)
    players = _interrupt_ippatsu(players)
    changed_state = replace(
        state,
        players=players,
        current_turn=action.actor,
        kan_count=state.kan_count + (action.kind is ActionKind.OPEN_KAN),
    )
    return (
        _add_kan_dora_indicator(changed_state, state.kan_count, profile)
        if action.kind is ActionKind.OPEN_KAN
        else changed_state
    )


def apply_action(
    state: GameState,
    action: Action,
    *,
    acquired_tile_id: str | None = None,
    profile: ResolvedRuleProfile | None = None,
) -> TransitionResult:
    """Apply one already-validated, adopted action to an immutable state."""
    player = state.players_by_seat[action.actor]
    if action.kind is ActionKind.DRAW:
        tile_id = action.tile_ids[0]
        if action.actor is not state.current_turn:
            raise TransitionError("DRAW_SOURCE_MISMATCH", {"action_id": action.action_id})
        if state.live_wall and state.live_wall[0].tile_id == tile_id:
            drawn = state.live_wall[0]
            wall_changes = {"live_wall": state.live_wall[1:]}
        elif state.dead_wall and state.dead_wall[0].tile_id == tile_id and state.live_wall:
            drawn = state.dead_wall[0]
            # A rinshan draw keeps the dead wall at fourteen tiles by moving the
            # live-wall tail into it; the exposed replacement source stays index 0.
            wall_changes = {
                "live_wall": state.live_wall[:-1],
                "dead_wall": state.dead_wall[1:] + (state.live_wall[-1],),
            }
        else:
            raise TransitionError("DRAW_SOURCE_MISMATCH", {"action_id": action.action_id})
        changed = replace(player, concealed_hand=player.concealed_hand + (drawn,))
        changed_state = _replace_player(state, changed, **wall_changes)
        event = "replacement_tile_after_kan" if tile_id == state.dead_wall[0].tile_id else "ordinary"
        return TransitionResult(changed_state, (action,), ActionPhase.AWAITING_DISCARD, acquired_tile_id=drawn.tile_id, winning_event=event)

    if action.kind in {ActionKind.DISCARD, ActionKind.RIICHI}:
        tile = next((tile for tile in player.concealed_hand if tile.tile_id == action.tile_ids[0]), None)
        if tile is None or action.actor is not state.current_turn:
            raise TransitionError("ACTION_TILE_NOT_OWNED", {"action_id": action.action_id})
        declaring = action.kind is ActionKind.RIICHI
        # If the original declaration tile was called away, the first later
        # discard becomes the visible river marker for the established riichi.
        replacing_called_marker = (
            not declaring
            and player.riichi is RiichiState.ESTABLISHED
            and not any(discard.riichi_declaration for discard in player.river)
        )
        changed = replace(
            player,
            concealed_hand=tuple(item for item in player.concealed_hand if item.tile_id != tile.tile_id),
            river=player.river + (
                Discard(
                    tile,
                    tsumogiri=tile.tile_id == acquired_tile_id,
                    riichi_declaration=declaring or replacing_called_marker,
                ),
            ),
            riichi=RiichiState.DECLARED if declaring else player.riichi,
            furiten=FuritenState.NONE if player.furiten is FuritenState.TEMPORARY else player.furiten,
            ippatsu_active=False if player.ippatsu_active else player.ippatsu_active,
        )
        changed_state = _replace_player(state, changed)
        return TransitionResult(changed_state, (action,), ActionPhase.AWAITING_RESPONSES)

    if action.kind in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}:
        changed_state = _apply_call(state, action, profile)
        next_phase = ActionPhase.AWAITING_REPLACEMENT_DRAW if action.kind is ActionKind.OPEN_KAN else ActionPhase.AWAITING_DISCARD
        return TransitionResult(
            changed_state,
            (action,),
            next_phase,
            preceding_call=action if action.kind in {ActionKind.CHI, ActionKind.PON} else None,
        )

    if action.kind is ActionKind.CONCEALED_KAN:
        owned = {tile.tile_id: tile for tile in player.concealed_hand}
        if any(tile_id not in owned for tile_id in action.tile_ids):
            raise TransitionError("ACTION_TILE_NOT_OWNED", {"action_id": action.action_id})
        meld = Meld(MeldKind.CONCEALED_KAN, tuple(owned[tile_id] for tile_id in action.tile_ids))
        changed = replace(player, concealed_hand=tuple(tile for tile in player.concealed_hand if tile.tile_id not in action.tile_ids), melds=player.melds + (meld,))
        players = tuple(changed if item.seat is action.actor else item for item in state.players)
        changed_state = replace(state, players=_interrupt_ippatsu(players), kan_count=state.kan_count + 1)
        changed_state = _add_kan_dora_indicator(changed_state, state.kan_count, profile)
        return TransitionResult(changed_state, (action,), ActionPhase.AWAITING_REPLACEMENT_DRAW)

    if action.kind is ActionKind.ADDED_KAN:
        # The tile remains in hand until every opponent has had the chance to
        # claim it by ron (chankan).  The kan is established only after passes.
        return TransitionResult(state, (action,), ActionPhase.AWAITING_RESPONSES, pending_added_kan=action)

    if action.kind in {ActionKind.RON, ActionKind.TSUMO}:
        # The winning integration and hand-end normalizer own the terminal
        # result; no ordinary action phase follows an adopted win.
        return TransitionResult(state, (action,), None)
    raise TransitionError("ACTION_REQUIRES_RESPONSE_RESOLUTION", {"kind": action.kind.value})


def _establish_added_kan(
    state: GameState,
    action: Action,
    profile: ResolvedRuleProfile | None = None,
) -> GameState:
    player = state.players_by_seat[action.actor]
    if action.target_meld_index is None or action.target_meld_index >= len(player.melds):
        raise TransitionError("INVALID_TARGET_MELD", {"action_id": action.action_id})
    meld = player.melds[action.target_meld_index]
    tile = next((item for item in player.concealed_hand if item.tile_id == action.tile_ids[0]), None)
    if meld.kind is not MeldKind.PON or tile is None or tile.tile_type != meld.tiles[0].tile_type:
        raise TransitionError("INVALID_ADDED_KAN", {"action_id": action.action_id})
    upgraded = Meld(MeldKind.ADDED_KAN, meld.tiles + (tile,), meld.called_from)
    melds = tuple(upgraded if index == action.target_meld_index else item for index, item in enumerate(player.melds))
    changed = replace(player, concealed_hand=tuple(item for item in player.concealed_hand if item.tile_id != tile.tile_id), melds=melds)
    players = tuple(changed if item.seat is action.actor else item for item in state.players)
    changed_state = replace(state, players=_interrupt_ippatsu(players), kan_count=state.kan_count + 1)
    return _add_kan_dora_indicator(changed_state, state.kan_count, profile)


def apply_response_resolution(
    state: GameState,
    context: LegalActionContext,
    resolution: ResponseResolution,
    *,
    profile: ResolvedRuleProfile | None = None,
) -> TransitionResult:
    """Apply at most one adopted non-win response, or advance after all pass."""
    if resolution.status is not ResponseResolutionStatus.RESOLVED:
        raise TransitionError("UNRESOLVED_RESPONSE_WINDOW", {})
    if resolution.adopted_actions:
        if all(action.kind is ActionKind.RON for action in resolution.adopted_actions):
            return TransitionResult(state, resolution.adopted_actions, None)
        if len(resolution.adopted_actions) != 1:
            raise TransitionError("MULTIPLE_NON_WIN_ADOPTIONS", {})

    players = list(state.players)
    for index, player in enumerate(players):
        result = resolution.intent_results[player.seat] if player.seat in resolution.intent_results else None
        if result and result.action.kind is ActionKind.PASS and (context.qualified_ron_by_seat or {}).get(player.seat, False):
            furiten = FuritenState.RIICHI if player.riichi is RiichiState.ESTABLISHED else FuritenState.TEMPORARY
            players[index] = replace(player, furiten=furiten)
    if context.discarder is None:
        raise TransitionError("INVALID_RESPONSE_CONTEXT", {})
    discarder = next(player for player in players if player.seat is context.discarder)
    if discarder.riichi is RiichiState.DECLARED:
        discarder = replace(discarder, riichi=RiichiState.ESTABLISHED, score=discarder.score - 1000, ippatsu_active=True)
        players[next(index for index, player in enumerate(players) if player.seat is discarder.seat)] = discarder
        deposits = state.riichi_deposits + 1000
    else:
        deposits = state.riichi_deposits
    prepared = replace(state, players=tuple(players), riichi_deposits=deposits)
    if resolution.adopted_actions:
        return apply_action(prepared, resolution.adopted_actions[0], profile=profile)
    if context.pending_added_kan is not None:
        return TransitionResult(
            _establish_added_kan(prepared, context.pending_added_kan, profile),
            (),
            ActionPhase.AWAITING_REPLACEMENT_DRAW,
        )
    next_turn = _next_seat(context.discarder)
    return TransitionResult(replace(prepared, current_turn=next_turn), (), ActionPhase.AWAITING_DRAW)
