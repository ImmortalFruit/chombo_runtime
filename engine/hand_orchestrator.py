"""Deterministic v0.1 single-hand orchestration and event recording."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from engine.actions import Action, ActionKind, ActionStatus
from engine.agent_policy import POLICY_ID
from engine.game_state import FuritenState, GameState, MeldKind, RiichiState, SEATS, Seat, TileInstance
from engine.hand_completion import analyze_hand
from engine.hand_log import HandEndReason, HandEvent, HandEventKind, HandLog, Participant
from engine.legal_actions import ActionPhase, LegalActionContext, generate_legal_actions
from engine.local_calculations import analyze_shape
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile
from engine.score_integration import integrate_win_score
from engine.seat_controllers import ControllerRequest, SeatController, build_seat_controllers
from engine.settlement import settle_hand, settle_multiple_ron
from engine.setup import SetupResult, create_initial_state
from engine.state_transition import apply_action, apply_response_resolution, resolve_responses
from engine.record_replay import state_delta


class HandOrchestrationError(RuntimeError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


MAX_AUTONOMOUS_DECISIONS = 1024


@dataclass(frozen=True)
class HandRun:
    log: HandLog
    final_state: GameState
    decision_count: int


def _tile_document(tile: TileInstance) -> dict[str, Any]:
    return {"tile_id": tile.tile_id, "tile_type": tile.tile_type, "is_red": tile.is_red}


def _notation(tiles: list[TileInstance]) -> str:
    grouped = {suit: [] for suit in "mpsz"}
    for tile in tiles:
        rank = "0" if tile.is_red else tile.tile_type[0]
        grouped[tile.tile_type[1]].append(rank)
    return "".join("".join(sorted(grouped[suit])) + suit for suit in "mpsz" if grouped[suit])


def _equivalent_tiles(state: GameState, seat: Seat, *, exclude_id: str | None = None) -> list[TileInstance]:
    player = state.players_by_seat[seat]
    tiles = [tile for tile in player.concealed_hand if tile.tile_id != exclude_id]
    for meld in player.melds:
        tiles.extend(meld.tiles[:3])
    return tiles


def _has_declarable_tenpai(state: GameState, seat: Seat) -> bool:
    """Return whether the hand has at least one wait not exhausted by its owner."""
    analysis = analyze_hand(_notation(_equivalent_tiles(state, seat)))
    if not analysis.get("tenpai"):
        return False
    player = state.players_by_seat[seat]
    owned_types = [tile.tile_type for tile in player.concealed_hand]
    owned_types.extend(tile.tile_type for meld in player.melds for tile in meld.tiles)
    return any(owned_types.count(wait["tile"]) < 4 for wait in analysis["waits"])


def _score_context(
    state: GameState, seat: Seat, profile: ResolvedRuleProfile, *, winning_event: str = "ordinary"
) -> dict[str, Any]:
    player = state.players_by_seat[seat]
    tile_by_id = {tile.tile_id: tile for tile in state.dead_wall}
    seat_offset = (SEATS.index(seat) - SEATS.index(state.dealer)) % 4
    seat_wind = ("1z", "2z", "3z", "4z")[seat_offset]
    fixed_melds = [
        {
            "tiles": [tile.tile_type for tile in meld.tiles],
            "open": meld.kind is not MeldKind.CONCEALED_KAN,
            "included_in_hand": True,
        }
        for meld in player.melds
    ]
    dora_indexes = [index for index, tile in enumerate(state.dead_wall) if tile.tile_id in state.dora_indicator_ids]
    dora_rule = profile.value("dora.profile")
    ura_indexes = dora_indexes if dora_rule.get("kan_ura") else dora_indexes[:1]
    ura_indicators = (
        [state.dead_wall[index + 1].tile_type for index in ura_indexes]
        if dora_rule.get("ura") else []
    )
    return {
        "player": seat.value,
        "concealed": all(meld.kind is MeldKind.CONCEALED_KAN for meld in player.melds),
        "prevailing_wind": f"{SEATS.index(state.round_wind) + 1}z",
        "seat_wind": seat_wind,
        "winning_event": winning_event,
        "own_discards": [discard.tile.tile_type for discard in player.river],
        "same_turn_missed_win": player.furiten is FuritenState.TEMPORARY,
        "riichi_missed_win": player.furiten is FuritenState.RIICHI,
        "riichi_established": player.riichi is RiichiState.ESTABLISHED,
        "double_riichi_established": False,
        "ippatsu_right_active": player.ippatsu_active,
        "quad_count": sum("kan" in meld.kind.value for meld in player.melds),
        "concealed_triplet_or_quad_count": sum(meld.kind is MeldKind.CONCEALED_KAN for meld in player.melds),
        "fixed_melds": fixed_melds,
        "dora_indicators": [tile_by_id[item].tile_type for item in state.dora_indicator_ids],
        "ura_dora_indicators": ura_indicators if player.riichi is RiichiState.ESTABLISHED else [],
        "declared": True,
    }


def _score_win(
    state: GameState,
    profile: ResolvedRuleProfile,
    seat: Seat,
    tile: TileInstance,
    method: str,
    *,
    winning_event: str | None = None,
) -> dict[str, Any]:
    require_profile_match(profile, state)
    exclude = tile.tile_id if method == "tsumo" else None
    hand_before = _notation(_equivalent_tiles(state, seat, exclude_id=exclude))
    winning_tile = f"0{tile.tile_type[1]}" if tile.is_red else tile.tile_type
    if winning_event is None:
        winning_event = "ordinary"
        if not state.live_wall:
            winning_event = "last_draw_of_hand" if method == "tsumo" else "last_discard_of_hand"
    context = _score_context(state, seat, profile, winning_event=winning_event)
    return integrate_win_score(
        hand_before,
        winning_tile,
        method,
        context=context,
        profile_id=profile.value("scoring.engine_profile"),
        dealer=seat is state.dealer,
        honba_count=state.honba,
        rule_profile=profile,
    )


def _win_scores(state: GameState, profile: ResolvedRuleProfile, context: LegalActionContext) -> dict[Seat, dict[str, Any]]:
    require_profile_match(profile, state)
    if context.phase is ActionPhase.AWAITING_DISCARD and context.acquired_tile_id:
        tile = next(tile for tile in state.players_by_seat[state.current_turn].concealed_hand if tile.tile_id == context.acquired_tile_id)
        event = None if context.winning_event == "ordinary" else context.winning_event
        score = _score_win(state, profile, state.current_turn, tile, "tsumo", winning_event=event)
        return {state.current_turn: score} if score["judgment_status"] == "SCORED" else {}
    if context.phase is ActionPhase.AWAITING_RESPONSES and context.discarder and context.discarded_tile_id:
        source = state.players_by_seat[context.discarder]
        tile = (
            next(tile for tile in source.concealed_hand if tile.tile_id == context.discarded_tile_id)
            if context.pending_added_kan is not None
            else source.river[-1].tile
        )
        scores = {}
        for seat in SEATS:
            if seat is context.discarder:
                continue
            event = None if context.winning_event == "ordinary" else context.winning_event
            score = _score_win(state, profile, seat, tile, "ron", winning_event=event)
            if score["judgment_status"] == "SCORED":
                scores[seat] = score
        return scores
    return {}


def _event(events: list[HandEvent], kind: HandEventKind, *, actor: Seat | None = None, cause: str | None = None, public: Mapping[str, Any] = {}, authoritative: Mapping[str, Any] = {}, concepts: tuple[str, ...] = ()) -> HandEvent:
    sequence = len(events)
    item = HandEvent(sequence, f"e{sequence:06d}", kind, actor, (cause,) if cause else (), public, authoritative, concepts)
    events.append(item)
    return item


def _action_data(action: Action) -> dict[str, Any]:
    return {"action_id": action.action_id, "kind": action.kind.value, "actor": action.actor.value, "tile_ids": list(action.tile_ids), "claimed_tile_id": action.claimed_tile_id, "target_seat": action.target_seat.value if action.target_seat else None, "target_meld_index": action.target_meld_index}


def _public_state(state: GameState, *, reveal_ura: bool = False) -> dict[str, Any]:
    """Return the observer-neutral public table state used by v0.1 viewers."""
    players = state.players_by_seat
    dora_indexes = [index for index, tile in enumerate(state.dead_wall) if tile.tile_id in state.dora_indicator_ids]
    return {
        "current_turn": state.current_turn.value,
        "dealer": state.dealer.value,
        "round_wind": state.round_wind.value,
        "hand_number": state.hand_number,
        "live_wall_count": len(state.live_wall),
        "honba": state.honba,
        "riichi_deposits": state.riichi_deposits,
        "dora_indicators": [
            _tile_document(tile)
            for tile in state.dead_wall
            if tile.tile_id in state.dora_indicator_ids
        ],
        "ura_dora_indicators": [
            _tile_document(state.dead_wall[index + 1]) for index in dora_indexes
        ] if reveal_ura else [],
        "seats": {
            seat.value: {
                "concealed_tile_count": len(players[seat].concealed_hand),
                "melds": [
                    {
                        "kind": meld.kind.value,
                        "tiles": [_tile_document(tile) for tile in meld.tiles],
                        "called_tile_id": meld.tiles[-1].tile_id if meld.called_from is not None and meld.kind is not MeldKind.ADDED_KAN else (meld.tiles[-2].tile_id if meld.called_from is not None else None),
                        "called_from": meld.called_from.value if meld.called_from is not None else None,
                    }
                    for meld in players[seat].melds
                ],
                "river": [
                    {
                        **_tile_document(discard.tile),
                        "tsumogiri": discard.tsumogiri,
                        "riichi_declaration": discard.riichi_declaration,
                    }
                    for discard in players[seat].river
                ],
                "riichi": players[seat].riichi.value,
                "score": players[seat].score,
            }
            for seat in SEATS
        },
    }


def _win_detail(
    state: GameState,
    winner: Seat,
    discarder: Seat | None,
    reason: HandEndReason,
    integrated: Mapping[str, Any],
    profile: ResolvedRuleProfile,
) -> dict[str, Any]:
    """Build the language-neutral result card shown before settlement."""
    selected_id = integrated["selected_candidate_ids"][0]
    selected = next(
        item for item in integrated["candidates"] if item["candidate_id"] == selected_id
    )
    player = state.players_by_seat[winner]
    dora_indexes = [
        index
        for index, tile in enumerate(state.dead_wall)
        if tile.tile_id in state.dora_indicator_ids
    ]
    winning_tile = (
        state.players_by_seat[discarder].river[-1].tile
        if reason is HandEndReason.RON and discarder is not None
        else player.concealed_hand[-1]
    )
    fu = selected.get("fu")
    score = selected["score"]
    dora_rule = profile.value("dora.profile")
    ura_indexes = dora_indexes if dora_rule.get("kan_ura") else dora_indexes[:1]
    return {
        "winner": winner.value,
        "method": reason.value,
        "source_seat": discarder.value if discarder is not None else winner.value,
        "winning_tile_id": winning_tile.tile_id,
        "concealed_tiles": [
            *[_tile_document(tile) for tile in player.concealed_hand],
            *([_tile_document(winning_tile)] if reason is HandEndReason.RON else []),
        ],
        "melds": [
            {
                "kind": meld.kind.value,
                "tiles": [_tile_document(tile) for tile in meld.tiles],
                "called_tile_id": (
                    meld.tiles[-2 if meld.kind is MeldKind.ADDED_KAN else -1].tile_id
                    if meld.called_from is not None
                    else None
                ),
                "called_from": meld.called_from.value if meld.called_from is not None else None,
            }
            for meld in player.melds
        ],
        "ordinary_yaku": list(selected.get("ordinary_yaku", [])),
        "yakuman": list(selected.get("yakuman", [])),
        "dora_han": selected.get("dora", {}).get("han", 0),
        "total_han": selected.get("total_han", 0),
        "fu": fu.get("fu") if isinstance(fu, Mapping) else None,
        "fu_detail": {
            "raw_fu": fu.get("raw_fu"),
            "components": list(fu.get("components", [])),
            "exception": fu.get("exception"),
        } if isinstance(fu, Mapping) else None,
        "base_points": score.get("winner_base_receipt", 0),
        "honba_points": score.get("winner_honba_receipt", 0),
        "total_points": score.get("winner_total_receipt", 0),
        "dora_indicators": [
            _tile_document(state.dead_wall[index]) for index in dora_indexes
        ],
        "ura_dora_enabled": bool(dora_rule.get("ura")),
        "ura_dora_indicators": [
            _tile_document(state.dead_wall[index + 1]) for index in ura_indexes
        ] if dora_rule.get("ura") and player.riichi is RiichiState.ESTABLISHED else [],
    }


def _authoritative_state(state: GameState) -> dict[str, Any]:
    """Return the canonical JSON state used by lossless record replay."""
    return {
        "live_wall": [_tile_document(tile) for tile in state.live_wall],
        "dead_wall": [_tile_document(tile) for tile in state.dead_wall],
        "players": [
            {
                "seat": player.seat.value,
                "concealed_hand": [_tile_document(tile) for tile in player.concealed_hand],
                "melds": [
                    {
                        "kind": meld.kind.value,
                        "tiles": [_tile_document(tile) for tile in meld.tiles],
                        "called_from": meld.called_from.value if meld.called_from else None,
                    }
                    for meld in player.melds
                ],
                "river": [
                    {
                        **_tile_document(discard.tile),
                        "tsumogiri": discard.tsumogiri,
                        "riichi_declaration": discard.riichi_declaration,
                    }
                    for discard in player.river
                ],
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
    }


def _event_deltas(before: GameState, after: GameState) -> tuple[dict[str, Any], dict[str, Any]]:
    public = state_delta(_public_state(before), _public_state(after))
    authoritative = state_delta(_authoritative_state(before), _authoritative_state(after))
    return (
        {"state_delta": public},
        {"state_delta": authoritative},
    )


def _participants(controllers: Mapping[Seat, SeatController]) -> tuple[tuple[Participant, ...], dict[Seat, str]]:
    counts: dict[str, int] = {}
    items: list[Participant] = []
    for seat in SEATS:
        controller_id = controllers[seat].controller_id
        prefix = "chombo-agent" if controller_id == POLICY_ID else "human"
        counts[prefix] = counts.get(prefix, 0) + 1
        ordinal = counts[prefix]
        items.append(
            Participant(
                f"{prefix}-{ordinal}",
                f"Chombo {ordinal}" if prefix == "chombo-agent" else f"Human {ordinal}",
                controller_id,
            )
        )
    participants = tuple(items)
    return participants, {
        seat: participant.participant_id
        for seat, participant in zip(SEATS, participants)
    }


def _discard_metrics(state: GameState, seat: Seat, actions: tuple[Action, ...]) -> tuple[dict[str, int], dict[str, int]]:
    player = state.players_by_seat[seat]
    shanten: dict[str, int] = {}
    ukeire: dict[str, int] = {}
    fixed_tiles = _notation([tile for meld in player.melds for tile in meld.tiles]) if player.melds else ""
    for action in actions:
        if action.kind is not ActionKind.DISCARD:
            continue
        remaining = [tile for tile in player.concealed_hand if tile.tile_id != action.tile_ids[0]]
        notation = _notation(remaining)
        analysis = analyze_shape(
            notation,
            rule_snapshot_id=state.rule_snapshot_id,
            fixed_meld_count=len(player.melds),
            fixed_tiles=fixed_tiles,
        )
        shanten[action.action_id] = analysis["overall_shanten"]
        ukeire[action.action_id] = analysis.get("total_in_hand_based_ukeire_copy_count", analysis.get("total_in_hand_based_wait_copy_count", 0))
    return shanten, ukeire


def _yaku_directed_calls(
    state: GameState, seat: Seat, actions: tuple[Action, ...]
) -> tuple[str, ...]:
    """Return calls with a conservative visible route to an open-hand yaku."""
    players = state.players_by_seat
    player = players[seat]
    owned = [*player.concealed_hand]
    owned.extend(tile for meld in player.melds for tile in meld.tiles)
    tile_by_id = {tile.tile_id: tile for tile in owned}
    for other in SEATS:
        tile_by_id.update(
            {discard.tile.tile_id: discard.tile for discard in players[other].river}
        )
    seat_offset = (SEATS.index(seat) - SEATS.index(state.dealer)) % 4
    valued_honors = {
        "5z", "6z", "7z", f"{seat_offset + 1}z",
        f"{SEATS.index(state.round_wind) + 1}z",
    }
    selected: list[str] = []
    for action in actions:
        if action.kind not in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}:
            continue
        called = tile_by_id.get(action.claimed_tile_id or "")
        supplied = [tile_by_id[tile_id] for tile_id in action.tile_ids]
        remaining = [tile for tile in owned if tile.tile_id not in action.tile_ids]
        types = [tile.tile_type for tile in [*remaining, *supplied, *([called] if called else [])]]
        yakuhai = (
            action.kind in {ActionKind.PON, ActionKind.OPEN_KAN}
            and called is not None
            and called.tile_type in valued_honors
        )
        tanyao = bool(types) and all(
            tile_type[1] != "z" and tile_type[0] not in "19" for tile_type in types
        )
        numbered_suits = {tile_type[1] for tile_type in types if tile_type[1] != "z"}
        flush = len(numbered_suits) == 1
        if yakuhai or tanyao or flush:
            selected.append(action.action_id)
    return tuple(selected)


def _decide(controllers: Mapping[Seat, SeatController], legal: Mapping[Seat, tuple[Action, ...]], state: GameState, seed: str, index: int) -> tuple[dict[Seat, Action], int]:
    actions: dict[Seat, Action] = {}
    for seat in SEATS:
        if seat not in legal:
            continue
        shanten, ukeire = _discard_metrics(state, seat, legal[seat])
        decision = controllers[seat].decide(ControllerRequest(
            seat, legal[seat], seed, index,
            discard_shanten_by_action_id=shanten,
            discard_ukeire_by_action_id=ukeire,
            yaku_directed_call_action_ids=_yaku_directed_calls(state, seat, legal[seat]),
        ))
        if decision.status is not ActionStatus.LEGAL or decision.action is None:
            raise HandOrchestrationError("CONTROLLER_INPUT_REQUIRED", {"seat": seat.value, "status": decision.status.value, "reason_code": decision.reason_code})
        actions[seat] = decision.action
        index += 1
    return actions, index


def run_autonomous_hand(seed: int, profile: ResolvedRuleProfile, *, hand_id: str | None = None, controllers: Mapping[Seat, SeatController] | None = None, max_decisions: int = MAX_AUTONOMOUS_DECISIONS) -> HandRun:
    """Run one hand without manual state repair and return its authoritative log."""
    if not isinstance(max_decisions, int) or isinstance(max_decisions, bool) or max_decisions < 1:
        raise HandOrchestrationError(
            "INVALID_DECISION_LIMIT", {"max_decisions": max_decisions}
        )
    seed_text = str(seed)
    setup: SetupResult = create_initial_state(seed_text, profile)
    state = setup.state
    controllers = controllers or build_seat_controllers(())
    events: list[HandEvent] = []
    initial = setup.to_dict()["initial_state"]
    initial_public_state = _public_state(state)
    start = _event(events, HandEventKind.HAND_STARTED)
    context = LegalActionContext(ActionPhase.AWAITING_DRAW)
    decision_index = 0
    last_event = start
    terminal: tuple[HandEndReason, tuple[Seat, ...], Seat | None, dict[Seat, dict[str, Any]]] | None = None

    while terminal is None:
        if context.phase is ActionPhase.AWAITING_DRAW and not state.live_wall:
            terminal = (HandEndReason.EXHAUSTIVE_DRAW, (), None, {})
            break
        scores = _win_scores(state, profile, context)
        if context.phase is ActionPhase.AWAITING_DISCARD:
            context = replace(context, last_draw=not state.live_wall, qualified_tsumo=state.current_turn in scores)
        elif context.phase is ActionPhase.AWAITING_RESPONSES:
            context = replace(context, last_discard=not state.live_wall, qualified_ron_by_seat={seat: seat in scores for seat in SEATS})
        legal = generate_legal_actions(state, profile, context)
        chosen, decision_index = _decide(controllers, legal, state, seed_text, decision_index)
        if decision_index > max_decisions:
            raise HandOrchestrationError(
                "HAND_DECISION_LIMIT_EXCEEDED",
                {
                    "seed": seed,
                    "decision_count": decision_index,
                    "max_decisions": max_decisions,
                    "phase": context.phase.value,
                },
            )

        if context.phase is ActionPhase.AWAITING_RESPONSES:
            resolution = resolve_responses(state, profile, context, chosen)
            transition = apply_response_resolution(
                state,
                context,
                resolution,
                profile=profile,
            )
            public_actions = [_action_data(action) for action in transition.applied_actions]
            public_delta, authoritative_delta = _event_deltas(state, transition.state)
            last_event = _event(events, HandEventKind.RESPONSE_RESOLVED, cause=last_event.event_id, public={"adopted_actions": public_actions, **public_delta}, authoritative=authoritative_delta)
            if transition.applied_actions and transition.applied_actions[0].kind is ActionKind.RON:
                winners = tuple(action.actor for action in transition.applied_actions)
                terminal = (HandEndReason.RON, winners, context.discarder, {winner: scores[winner] for winner in winners})
                break
        else:
            action = next(iter(chosen.values()))
            transition = apply_action(
                state,
                action,
                acquired_tile_id=context.acquired_tile_id,
                profile=profile,
            )
            public_delta, authoritative_delta = _event_deltas(state, transition.state)
            last_event = _event(events, HandEventKind.ACTION_APPLIED, actor=action.actor, cause=last_event.event_id, public={**_action_data(action), **public_delta}, authoritative=authoritative_delta, concepts=(f"game.hand.tile_flow.{action.kind.value}",))
            if action.kind is ActionKind.TSUMO:
                terminal = (HandEndReason.TSUMO, (action.actor,), None, {action.actor: scores[action.actor]})
                break
        state = transition.state
        if transition.next_phase is ActionPhase.AWAITING_RESPONSES:
            action = transition.applied_actions[0]
            context = LegalActionContext(
                transition.next_phase,
                last_event.event_id,
                action.tile_ids[0],
                action.actor,
                winning_event=("tile_used_by_other_player_for_added_kan" if action.kind is ActionKind.ADDED_KAN else "ordinary"),
                pending_added_kan=transition.pending_added_kan,
            )
        else:
            context = LegalActionContext(transition.next_phase, preceding_call=transition.preceding_call, acquired_tile_id=transition.acquired_tile_id, winning_event=transition.winning_event)

    reason, winners, discarder, integrated_by_winner = terminal
    tenpai = tuple(
        seat
        for seat in SEATS
        if not winners
        and _has_declarable_tenpai(state, seat)
    )
    if reason is HandEndReason.EXHAUSTIVE_DRAW:
        for seat in SEATS:
            declared = seat in tenpai
            last_event = _event(
                events,
                HandEventKind.TENPAI_DECLARED,
                actor=seat,
                cause=last_event.event_id,
                public={
                    "declaration": "tenpai" if declared else "noten",
                },
                concepts=("game.hand.end.draw_tenpai_declaration",),
            )
    details = [_win_detail(state, winner, discarder, reason, integrated_by_winner[winner], profile) for winner in winners]
    last_event = _event(events, HandEventKind.HAND_ENDED, cause=last_event.event_id, public={"reason": reason.value, "winners": [winner.value for winner in winners], "declared_tenpai_player_ids": [seat.value for seat in tenpai], **({"win_detail": details[0]} if details else {}), **({"win_details": details} if len(details) > 1 else {})}, concepts=("game.hand.end.win" if winners else "game.hand.end.exhaustive_draw",))
    if len(winners) > 1:
        winner_scores = []
        for winner in winners:
            integrated = integrated_by_winner[winner]
            selected_id = integrated["selected_candidate_ids"][0]
            selected = next(item for item in integrated["candidates"] if item["candidate_id"] == selected_id)
            winner_scores.append((winner, selected["score"]))
        settlement = settle_multiple_ron(state, profile, winners_and_scores=winner_scores, discarder=discarder)
    elif winners:
        winner = winners[0]
        integrated = integrated_by_winner[winner]
        selected_id = integrated["selected_candidate_ids"][0]
        selected = next(item for item in integrated["candidates"] if item["candidate_id"] == selected_id)
        settlement = settle_hand(state, profile, end_reason=reason, winner=winner, method=reason.value, score=selected["score"], discarder=discarder)
    else:
        settlement = settle_hand(state, profile, end_reason=reason, declared_tenpai=tenpai)
    result = settlement["hand_result"]
    before_settlement = state
    state = replace(
        state,
        players=tuple(replace(player, score=result.final_scores[player.seat]) for player in state.players),
        riichi_deposits=settlement["riichi_deposit_pool_after"],
    )
    public_delta, authoritative_delta = _event_deltas(before_settlement, state)
    _event(events, HandEventKind.SETTLEMENT_APPLIED, cause=last_event.event_id, public={"point_deltas": settlement["point_deltas"], "final_scores": settlement["final_scores"], **public_delta}, authoritative=authoritative_delta, concepts=result.applied_concept_ids)
    participants, assignments = _participants(controllers)
    log = HandLog(hand_id or f"hand-{seed}", "chombo.hand_log.v0.1", seed, setup.rng_id, POLICY_ID, profile.profile_id, profile.snapshot_id, initial_public_state, initial, tuple(events), result, participants, assignments)
    return HandRun(log, state, decision_index)
