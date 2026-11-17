"""Pause-and-resume hand orchestration for human and Chombo seats."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import time
from typing import Any, Callable, Mapping, Sequence

from engine.actions import Action, ActionKind, ActionStatus
from engine.abortive_draw import (
    DiscardResponseObservation,
    detect_four_kans,
    detect_four_riichi,
    detect_four_winds_first_discard,
    detect_nine_terminals,
    detect_three_ron,
    settle_abortive_draw,
)
from engine.agent_policy import POLICY_ID, VerificationStrategy
from engine.game_state import GameState, RiichiState, SEATS, Seat
from engine.hand_log import HandEndReason, HandEvent, HandEventKind, HandLog, HandResult, Participant
from engine.hand_orchestrator import (
    _action_data,
    _discard_metrics,
    _event,
    _event_deltas,
    _equivalent_tiles,
    _has_declarable_tenpai,
    _notation,
    _authoritative_state,
    _public_state,
    _win_detail,
    _score_win,
    _win_scores,
    _yaku_directed_calls,
)
from engine.legal_actions import ActionPhase, LegalActionContext, generate_legal_actions
from engine.hand_completion import analyze_hand
from engine.nagashi_mangan import detect_nagashi_mangan
from engine.nagashi_mangan_settlement import settle_nagashi_mangan
from engine.rule_profiles import require_profile_match, ResolvedRuleProfile
from engine.seat_controllers import AgentSeatController, ControllerRequest, HumanSeatController
from engine.settlement import settle_hand, settle_multiple_ron
from engine.setup import SetupResult, create_initial_state
from engine.state_transition import apply_action, apply_response_resolution, resolve_responses
from engine.visible_waits import visible_waits_for_seat


class InteractiveHandError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


LIVE_SOUND_EVENT_WINDOW = 64


@dataclass(frozen=True)
class PendingDecision:
    seat: Seat
    actions: tuple[Action, ...]


def _default_participants(
    human_seats: frozenset[Seat],
) -> tuple[tuple[Participant, ...], dict[Seat, str]]:
    counts = {"human": 0, "chombo-agent": 0}
    participants = []
    for seat in SEATS:
        prefix = "human" if seat in human_seats else "chombo-agent"
        counts[prefix] += 1
        ordinal = counts[prefix]
        participants.append(
            Participant(
                f"{prefix}-{ordinal}",
                f"Human {ordinal}" if prefix == "human" else f"Chombo {ordinal}",
                "chombo.human_input.v1" if prefix == "human" else POLICY_ID,
            )
        )
    participant_tuple = tuple(participants)
    return participant_tuple, {
        seat: participant.participant_id
        for seat, participant in zip(SEATS, participant_tuple)
    }


def random_human_seat(seed: int | str) -> Seat:
    digest = hashlib.sha256(f"chombo.human-seat.v1\0{seed}".encode()).digest()
    return SEATS[int.from_bytes(digest[:8], "big") % len(SEATS)]


class InteractiveHandSession:
    """One deterministic hand that pauses whenever a human controller must act."""

    def __init__(
        self,
        seed: int,
        profile: ResolvedRuleProfile,
        human_seats: Sequence[Seat],
        *,
        hand_id: str | None = None,
        agent_delay_seconds: float = 0,
        on_event: Callable[[], None] | None = None,
        setup: SetupResult | None = None,
        participants: tuple[Participant, ...] | None = None,
        seat_assignments: Mapping[Seat, str] | None = None,
        auto_advance: bool = True,
        verification_strategy: VerificationStrategy = VerificationStrategy.NORMAL,
    ):
        humans = tuple(human_seats)
        if len(humans) != len(set(humans)) or any(s not in SEATS for s in humans):
            raise InteractiveHandError("INVALID_HUMAN_SEATS", {"seats": [str(s) for s in humans]})
        setup = setup or create_initial_state(str(seed), profile)
        require_profile_match(profile, setup.state, error_type=InteractiveHandError)
        if (participants is None) is not (seat_assignments is None):
            raise InteractiveHandError("INCOMPLETE_MATCH_PARTICIPANTS", {})
        self.seed = seed
        self.profile = profile
        self.human_seats = frozenset(humans)
        self.hand_id = hand_id or f"hand-{seed}"
        # Autonomous play never waits for presentation, even with a configured delay.
        self.agent_delay_seconds = agent_delay_seconds if humans else 0
        self.on_event = on_event
        self.verification_strategy = verification_strategy
        self.participants = participants
        self.seat_assignments = seat_assignments
        self.state = setup.state
        self.context = LegalActionContext(ActionPhase.AWAITING_DRAW)
        self.events: list[HandEvent] = []
        self.authoritative_initial_state = setup.to_dict()["initial_state"]
        self.initial_public_state = _public_state(self.state)
        self.last_event = _event(self.events, HandEventKind.HAND_STARTED)
        self.rng_id = setup.rng_id
        self.decision_count = 0
        self.controller_decisions: list[dict[str, Any]] = []
        self._legal: Mapping[Seat, tuple[Action, ...]] = {}
        self._agent_actions: dict[Seat, Action] = {}
        self._human_actions: dict[Seat, Action] = {}
        self.agent_controllers = {
            seat: AgentSeatController() for seat in SEATS if seat not in self.human_seats
        }
        self.result = None
        self.win_detail = None
        self.win_details = ()
        self.declared_tenpai: tuple[Seat, ...] = ()
        self._actual_tenpai: frozenset[Seat] = frozenset()
        self._declaration_index: int | None = None
        self._draw_reveal = False
        self._visible_waits_by_seat: dict[Seat, list[dict[str, Any]]] = {
            seat: [] for seat in SEATS
        }
        self._waits_started: set[Seat] = set()
        self.log: HandLog | None = None
        if auto_advance:
            self.advance()

    @property
    def complete(self) -> bool:
        return self.log is not None

    @property
    def pending(self) -> tuple[PendingDecision, ...]:
        return tuple(
            PendingDecision(seat, self._legal[seat])
            for seat in SEATS
            if seat in self._legal and seat in self.human_seats and seat not in self._human_actions
        )

    def _prepare_context(self) -> dict[Seat, dict[str, Any]]:
        if self.context.phase is ActionPhase.AWAITING_DRAW and not self.state.live_wall:
            if "hand.special.nagashi_mangan" in self.profile.dimensions:
                detection = detect_nagashi_mangan(
                    self.state, self.profile, trigger_event_id=self.last_event.event_id
                )
                if detection.candidate is not None:
                    settled = settle_nagashi_mangan(
                        self.state, self.profile, detection.candidate
                    )
                    self._finish_special(
                        HandEndReason.NAGASHI_MANGAN,
                        settled,
                        winners=detection.candidate.qualifier_seats,
                    )
                    return {}
            self._actual_tenpai = frozenset(
                seat
                for seat in SEATS
                if _has_declarable_tenpai(self.state, seat)
            )
            self._declaration_index = 0
            return {}
        scores = (
            {}
            if self.verification_strategy
            is VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI
            else _win_scores(self.state, self.profile, self.context)
        )
        if self.context.phase is ActionPhase.AWAITING_DISCARD:
            self.context = replace(
                self.context,
                last_draw=not self.state.live_wall,
                qualified_tsumo=self.state.current_turn in scores,
            )
            if (
                self.verification_strategy is not VerificationStrategy.NORMAL
                and self.context.acquired_tile_id is not None
            ):
                detection = detect_nine_terminals(
                    self.state,
                    self.profile,
                    declarer=self.state.current_turn,
                    trigger_event_id=self.last_event.event_id,
                    first_self_draw_tile_id=self.context.acquired_tile_id or "",
                )
                if detection.candidate is not None:
                    self._finish_abortive(detection.candidate)
                    return {}
        elif self.context.phase is ActionPhase.AWAITING_RESPONSES:
            self.context = replace(
                self.context,
                last_discard=not self.state.live_wall,
                qualified_ron_by_seat={seat: seat in scores for seat in SEATS},
            )
        return scores

    def _agent_decision(self, seat: Seat, actions: tuple[Action, ...]) -> Action:
        preferred = self._verification_preferred_actions(seat, actions)
        if self.verification_strategy is not VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI:
            shanten, ukeire = _discard_metrics(self.state, seat, actions)
        else:
            shanten, ukeire = {}, {}
        decision_index = self.decision_count
        controller = self.agent_controllers[seat]
        decision = controller.decide(
            ControllerRequest(
                seat,
                actions,
                str(self.seed),
                self.decision_count,
                discard_shanten_by_action_id=shanten,
                discard_ukeire_by_action_id=ukeire,
                yaku_directed_call_action_ids=_yaku_directed_calls(
                    self.state, seat, actions
                ),
                preferred_action_ids=preferred,
                verification_strategy=self.verification_strategy,
            )
        )
        self.controller_decisions.append({
            "seat": seat.value, "controller_id": controller.controller_id,
            "decision_index": decision_index,
            "legal_action_ids": [action.action_id for action in actions],
            "selected_action_id": decision.action.action_id if decision.action else None,
        })
        self.decision_count += 1
        assert decision.action is not None
        return decision.action

    def _verification_preferred_actions(
        self, seat: Seat, actions: tuple[Action, ...]
    ) -> tuple[str, ...]:
        if self.verification_strategy is VerificationStrategy.NORMAL:
            return ()
        if (
            self.verification_strategy is VerificationStrategy.OPEN_HAND_THREE_RON
            and self.context.phase is ActionPhase.AWAITING_RESPONSES
        ):
            qualified = _win_scores(self.state, self.profile, self.context)
            desired = ActionKind.RON if len(qualified) == 3 and seat in qualified else ActionKind.PASS
            return tuple(action.action_id for action in actions if action.kind is desired)
        tiles = {
            tile.tile_id: tile
            for tile in self.state.players_by_seat[seat].concealed_hand
        }
        discards = tuple(
            action
            for action in actions
            if action.kind in {ActionKind.DISCARD, ActionKind.RIICHI}
        )
        if not discards:
            return ()
        if self.verification_strategy is VerificationStrategy.OPEN_HAND_THREE_RON:
            opponents = tuple(other for other in SEATS if other is not seat)
            established_waits: list[set[str]] = []
            immediate = []
            for action in discards:
                tile = tiles[action.tile_ids[0]]
                qualified = []
                for other in opponents:
                    try:
                        score = _score_win(self.state, self.profile, other, tile, "ron")
                    except ValueError:
                        score = {}
                    qualified.append(score.get("judgment_status") == "SCORED")
                if all(qualified):
                    immediate.append(action.action_id)
            if immediate:
                return tuple(immediate)

            # Verification/study-only perfect information: move this hand toward
            # waits already shared by the other three concealed hands.  This data
            # affects selection only and is never added to the public projection.
            opponent_wait_counts: dict[str, int] = {}
            for other in opponents:
                analysis = analyze_hand(_notation(_equivalent_tiles(self.state, other)))
                prospects = analysis.get("waits", analysis.get("ukeire", ()))
                if self.state.players_by_seat[other].riichi is RiichiState.ESTABLISHED:
                    established_waits.append({item["tile"] for item in analysis.get("waits", ())})
                for prospect in prospects:
                    tile_type = prospect["tile"]
                    opponent_wait_counts[tile_type] = opponent_wait_counts.get(tile_type, 0) + 1
            ranked: list[tuple[tuple[int, int, int, int, int], str]] = []
            for action in discards:
                analysis = analyze_hand(
                    _notation(
                        _equivalent_tiles(
                            self.state, seat, exclude_id=action.tile_ids[0]
                        )
                    )
                )
                prospects = analysis.get("waits", analysis.get("ukeire", ()))
                waits = tuple(item["tile"] for item in prospects)
                maximum_overlap = max(
                    (opponent_wait_counts.get(item, 0) for item in waits), default=0
                )
                shared_established = (
                    set.intersection(*established_waits) if established_waits else set()
                )
                riichi_allowed = len(established_waits) < 3 and (
                    not established_waits or bool(set(waits) & shared_established)
                )
                score = (
                    -int(analysis["overall_shanten"]),
                    maximum_overlap,
                    sum(opponent_wait_counts.get(item, 0) for item in waits),
                    (
                        1
                        if action.kind is ActionKind.RIICHI and riichi_allowed
                        else -1 if action.kind is ActionKind.RIICHI else 0
                    ),
                    len(waits),
                )
                ranked.append((score, action.action_id))
            best = max(score for score, _ in ranked)
            return tuple(action_id for score, action_id in ranked if score == best)
        player = self.state.players_by_seat[seat]
        if (
            self.verification_strategy is VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI
            and not player.river
        ):
            prior_first_winds = [
                other.river[0].tile.tile_type
                for other in self.state.players
                if other.river
            ]
            target = prior_first_winds[0] if prior_first_winds else None
            winds = tuple(
                action.action_id
                for action in discards
                if tiles[action.tile_ids[0]].tile_type in {"1z", "2z", "3z", "4z"}
                and (target is None or tiles[action.tile_ids[0]].tile_type == target)
            )
            if winds:
                return winds
        if self.verification_strategy is not VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI:
            return ()
        terminals = tuple(
            action.action_id
            for action in discards
            if tiles[action.tile_ids[0]].tile_type.endswith("z")
            or tiles[action.tile_ids[0]].tile_type[0] in {"1", "9"}
        )
        return terminals

    def submit(self, seat: Seat, action_id: str) -> None:
        if self.complete:
            raise InteractiveHandError("HAND_ALREADY_COMPLETE", {})
        pending = {item.seat: item.actions for item in self.pending}
        if seat not in pending:
            raise InteractiveHandError("SEAT_NOT_AWAITING_INPUT", {"seat": seat.value})
        submitted = next((action for action in pending[seat] if action.action_id == action_id), None)
        if submitted is None:
            raise InteractiveHandError("ACTION_NOT_IN_LEGAL_SET", {"seat": seat.value, "action_id": action_id})
        decision = HumanSeatController().decide(
            ControllerRequest(seat, pending[seat], str(self.seed), self.decision_count, submitted)
        )
        if decision.status is not ActionStatus.LEGAL or decision.action is None:
            raise InteractiveHandError(decision.reason_code or "ILLEGAL_ACTION", decision.details)
        self.decision_count += 1
        self._human_actions[seat] = decision.action
        self.advance()

    def advance(self) -> None:
        while not self.complete:
            if self._declaration_index is not None:
                if self._advance_tenpai_declarations():
                    return
                continue
            scores = self._prepare_context()
            if self.complete:
                return
            if self._declaration_index is not None:
                continue
            policy_context = self.context
            if self.verification_strategy is VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI:
                policy_context = replace(
                    self.context, suppress_riichi=True, suppress_calls=True
                )
            self._legal = generate_legal_actions(
                self.state, self.profile, policy_context
            )
            self._legal = {
                seat: actions for seat, actions in self._legal.items() if actions
            }
            self._agent_actions = {
                seat: self._agent_decision(seat, actions)
                for seat, actions in self._legal.items()
                if seat not in self.human_seats
            }
            for seat, actions in self._legal.items():
                if seat in self.human_seats and len(actions) == 1:
                    decision = HumanSeatController().decide(
                        ControllerRequest(
                            seat, actions, str(self.seed), self.decision_count, actions[0]
                        )
                    )
                    assert decision.action is not None
                    self._human_actions[seat] = decision.action
                    self.decision_count += 1
            if self.pending:
                return
            chosen = {**self._agent_actions, **self._human_actions}
            self._human_actions = {}
            wait_update_seat = None
            if self.context.phase is ActionPhase.AWAITING_RESPONSES:
                if self._agent_actions and self.agent_delay_seconds:
                    time.sleep(self.agent_delay_seconds)
                ron_claimants = tuple(
                    seat for seat, action in chosen.items() if action.kind is ActionKind.RON
                )
                observation = DiscardResponseObservation(
                    self.context.source_event_id or self.last_event.event_id,
                    self.context.discarded_tile_id or "",
                    True,
                    ron_claimants,
                    any(
                        action.kind in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}
                        for action in chosen.values()
                    ),
                )
                three_ron = detect_three_ron(self.state, self.profile, observation)
                if three_ron.candidate is not None:
                    self._finish_abortive(three_ron.candidate)
                    return
                resolution = resolve_responses(self.state, self.profile, self.context, chosen)
                transition = apply_response_resolution(
                    self.state,
                    self.context,
                    resolution,
                    profile=self.profile,
                )
                public_delta, authoritative_delta = _event_deltas(self.state, transition.state)
                self.last_event = _event(
                    self.events,
                    HandEventKind.RESPONSE_RESOLVED,
                    cause=self.last_event.event_id,
                    public={
                        "adopted_actions": [_action_data(action) for action in transition.applied_actions],
                        **public_delta,
                    },
                    authoritative=authoritative_delta,
                )
                if transition.applied_actions and transition.applied_actions[0].kind is ActionKind.RON:
                    winners = tuple(action.actor for action in transition.applied_actions)
                    self._finish(HandEndReason.RON, winners, self.context.discarder, {winner: scores[winner] for winner in winners})
                    return
                resolved_observation = replace(
                    observation,
                    accepted_call=any(
                        action.kind in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}
                        for action in transition.applied_actions
                    ),
                )
                for detector in (
                    detect_four_winds_first_discard,
                    detect_four_riichi,
                    detect_four_kans,
                ):
                    detection = detector(
                        transition.state, self.profile, resolved_observation
                    )
                    if detection.candidate is not None:
                        self.state = transition.state
                        self._finish_abortive(detection.candidate)
                        return
            else:
                action = next(iter(chosen.values()))
                if (
                    action.actor not in self.human_seats
                    and action.kind in {ActionKind.DISCARD, ActionKind.RIICHI}
                    and self.agent_delay_seconds
                ):
                    time.sleep(self.agent_delay_seconds)
                transition = apply_action(
                    self.state,
                    action,
                    acquired_tile_id=self.context.acquired_tile_id,
                    profile=self.profile,
                )
                public_delta, authoritative_delta = _event_deltas(self.state, transition.state)
                self.last_event = _event(
                    self.events,
                    HandEventKind.ACTION_APPLIED,
                    actor=action.actor,
                    cause=self.last_event.event_id,
                    public={**_action_data(action), **public_delta},
                    authoritative=authoritative_delta,
                    concepts=(f"game.hand.tile_flow.{action.kind.value}",),
                )
                if action.kind in {ActionKind.DISCARD, ActionKind.RIICHI}:
                    wait_update_seat = action.actor
                if action.kind is ActionKind.TSUMO:
                    self._finish(HandEndReason.TSUMO, action.actor, None, scores[action.actor])
                    return
            self.state = transition.state
            if wait_update_seat is not None:
                public_state = _public_state(self.state)
                self._waits_started.add(wait_update_seat)
                authoritative_state = _authoritative_state(self.state)
                for wait_seat in self._waits_started:
                    self._visible_waits_by_seat[wait_seat] = visible_waits_for_seat(
                        public_state, authoritative_state, wait_seat.value,
                        observer_seat=wait_seat.value,
                        rule_profile_id=self.profile.profile_id,
                        rule_snapshot_id=self.profile.snapshot_id,
                    )
            if transition.next_phase is ActionPhase.AWAITING_RESPONSES:
                action = transition.applied_actions[0]
                self.context = LegalActionContext(
                    transition.next_phase,
                    self.last_event.event_id,
                    action.tile_ids[0],
                    action.actor,
                    winning_event=("tile_used_by_other_player_for_added_kan" if action.kind is ActionKind.ADDED_KAN else "ordinary"),
                    pending_added_kan=transition.pending_added_kan,
                )
            else:
                self.context = LegalActionContext(
                    transition.next_phase,
                    preceding_call=transition.preceding_call,
                    acquired_tile_id=transition.acquired_tile_id,
                    winning_event=transition.winning_event,
                )
            if self.on_event is not None:
                self.on_event()

    def _advance_tenpai_declarations(self) -> bool:
        """Advance the east-to-north exhaustive-draw declaration ceremony."""
        assert self._declaration_index is not None
        if self._declaration_index >= len(SEATS):
            self._legal = {}
            if self.agent_delay_seconds:
                time.sleep(self.agent_delay_seconds)
            self._declaration_index = None
            self._finish(HandEndReason.EXHAUSTIVE_DRAW, None, None, None)
            return False

        seat = SEATS[self._declaration_index]
        noten = Action(
            f"draw-declaration-{seat.value}-noten",
            ActionKind.NOTEN_DECLARATION,
            seat,
        )
        actions = (noten,)
        if seat in self._actual_tenpai:
            tenpai = Action(
                f"draw-declaration-{seat.value}-tenpai",
                ActionKind.TENPAI_DECLARATION,
                seat,
            )
            actions = (tenpai, noten)
        self._legal = {seat: actions}

        if seat in self.human_seats:
            action = self._human_actions.pop(seat, None)
            if action is None:
                return True
        else:
            action = actions[0]
            self.decision_count += 1
            if self.agent_delay_seconds:
                time.sleep(self.agent_delay_seconds)

        declared = action.kind is ActionKind.TENPAI_DECLARATION
        if declared:
            self.declared_tenpai = (*self.declared_tenpai, seat)
        self.last_event = _event(
            self.events,
            HandEventKind.TENPAI_DECLARED,
            actor=seat,
            cause=self.last_event.event_id,
            public={
                "declaration": "tenpai" if declared else "noten",
            },
            concepts=("game.hand.end.draw_tenpai_declaration",),
        )
        self._declaration_index += 1
        if self.on_event is not None:
            self.on_event()
        return False

    def _finish(self, reason, winner, discarder, integrated) -> None:
        winners = winner if isinstance(winner, tuple) else (() if winner is None else (winner,))
        integrated_by_winner = integrated if isinstance(integrated, dict) and winners and all(seat in integrated for seat in winners) else ({winners[0]: integrated} if winners else {})
        if winners:
            self.declared_tenpai = ()
        details = [_win_detail(self.state, item, discarder, reason, integrated_by_winner[item], self.profile) for item in winners]
        detail = details[0] if details else None
        self.win_detail = detail
        self.win_details = tuple(details)
        self.last_event = _event(
            self.events,
            HandEventKind.HAND_ENDED,
            cause=self.last_event.event_id,
            public={"reason": reason.value, "winners": [item.value for item in winners], "declared_tenpai_player_ids": [seat.value for seat in self.declared_tenpai], **({"win_detail": detail} if detail else {}), **({"win_details": details} if len(details) > 1 else {})},
            concepts=("game.hand.end.win" if winners else "game.hand.end.exhaustive_draw",),
        )
        if reason is HandEndReason.EXHAUSTIVE_DRAW:
            self._draw_reveal = True
            if self.on_event is not None:
                self.on_event()
            if self.agent_delay_seconds:
                time.sleep(self.agent_delay_seconds)
        if len(winners) > 1:
            winner_scores = []
            for item in winners:
                value = integrated_by_winner[item]
                selected = next(candidate for candidate in value["candidates"] if candidate["candidate_id"] == value["selected_candidate_ids"][0])
                winner_scores.append((item, selected["score"]))
            settlement = settle_multiple_ron(self.state, self.profile, winners_and_scores=winner_scores, discarder=discarder)
        elif winners:
            item = winners[0]
            value = integrated_by_winner[item]
            selected = next(candidate for candidate in value["candidates"] if candidate["candidate_id"] == value["selected_candidate_ids"][0])
            settlement = settle_hand(self.state, self.profile, end_reason=reason, winner=item, method=reason.value, score=selected["score"], discarder=discarder)
        else:
            settlement = settle_hand(self.state, self.profile, end_reason=reason, declared_tenpai=self.declared_tenpai)
        self.result = settlement["hand_result"]
        before_settlement = self.state
        self.state = replace(
            self.state,
            players=tuple(replace(player, score=self.result.final_scores[player.seat]) for player in self.state.players),
            riichi_deposits=settlement["riichi_deposit_pool_after"],
        )
        public_delta, authoritative_delta = _event_deltas(before_settlement, self.state)
        _event(
            self.events,
            HandEventKind.SETTLEMENT_APPLIED,
            cause=self.last_event.event_id,
            public={"point_deltas": settlement["point_deltas"], "final_scores": settlement["final_scores"], **public_delta},
            authoritative=authoritative_delta,
            concepts=self.result.applied_concept_ids,
        )
        self._finalize_log()

    def _finish_abortive(self, candidate) -> None:
        settled = settle_abortive_draw(self.state, self.profile, candidate)
        reason = HandEndReason(candidate.reason_code.value.lower())
        self._finish_special(reason, settled)

    def _finish_special(self, reason, settled, *, winners=()) -> None:
        self.declared_tenpai = ()
        self.last_event = _event(
            self.events,
            HandEventKind.HAND_ENDED,
            cause=self.last_event.event_id,
            public={
                "reason": reason.value,
                "winners": [seat.value for seat in winners],
                "declared_tenpai_player_ids": [],
                "special_result_family": settled.candidate.result_family.value,
                "qualifier_seats": [
                    seat.value for seat in settled.candidate.qualifier_seats
                ],
            },
            concepts=settled.applied_concept_ids,
        )
        self.result = HandResult(
            reason,
            tuple(winners),
            settled.point_deltas,
            settled.final_scores,
            settled.applied_concept_ids,
        )
        before_settlement = self.state
        self.state = replace(
            self.state,
            players=tuple(
                replace(player, score=self.result.final_scores[player.seat])
                for player in self.state.players
            ),
            riichi_deposits=settled.riichi_deposit_pool_after,
        )
        public_delta, authoritative_delta = _event_deltas(
            before_settlement, self.state
        )
        _event(
            self.events,
            HandEventKind.SETTLEMENT_APPLIED,
            cause=self.last_event.event_id,
            public={
                "point_deltas": {
                    seat.value: value for seat, value in settled.point_deltas.items()
                },
                "final_scores": {
                    seat.value: value for seat, value in settled.final_scores.items()
                },
                **public_delta,
            },
            authoritative=authoritative_delta,
            concepts=self.result.applied_concept_ids,
        )
        self._finalize_log()

    def _finalize_log(self) -> None:
        if self.participants is None:
            participants, seat_assignments = _default_participants(self.human_seats)
        else:
            participants = self.participants
            seat_assignments = self.seat_assignments
            assert seat_assignments is not None
        self.log = HandLog(
            self.hand_id,
            "chombo.hand_log.v0.1",
            self.seed,
            self.rng_id,
            POLICY_ID,
            self.profile.profile_id,
            self.profile.snapshot_id,
            self.initial_public_state,
            self.authoritative_initial_state,
            tuple(self.events),
            self.result,
            participants,
            seat_assignments,
        )
        if self.on_event is not None:
            self.on_event()

    def action_document(self, action: Action) -> dict[str, Any]:
        tiles = {tile.tile_id: tile for tile in self.state.players_by_seat[action.actor].concealed_hand}
        return {
            **_action_data(action),
            "tiles": [
                {"tile_id": tiles[tile_id].tile_id, "tile_type": tiles[tile_id].tile_type, "is_red": tiles[tile_id].is_red}
                for tile_id in action.tile_ids
                if tile_id in tiles
            ],
        }

    def snapshot(self, observer: Seat) -> dict[str, Any]:
        if observer not in self.human_seats:
            raise InteractiveHandError("OBSERVER_NOT_HUMAN_SEAT", {"seat": observer.value})
        return self._snapshot(observer)

    def spectator_snapshot(self, *, reveal_all: bool = False) -> dict[str, Any]:
        """Project all-Agent play; never grant spectator access to human sessions."""
        if self.human_seats:
            raise InteractiveHandError("SPECTATOR_REQUIRES_ALL_AGENTS", {})
        value = self._snapshot(Seat.EAST)
        value["human_seat"] = None
        value["perspective"] = Seat.EAST.value
        value["legal_actions"] = []
        value["visible_waits_by_seat"] = {}
        for seat in SEATS:
            player = value["state"]["seats"][seat.value]
            if reveal_all:
                player["concealed_tiles"] = [
                    {"tile_id": tile.tile_id, "tile_type": tile.tile_type, "is_red": tile.is_red}
                    for tile in self.state.players_by_seat[seat].concealed_hand
                ]
            elif seat.value not in value["revealed_seats"]:
                player.pop("concealed_tiles", None)
        value["drawn_tile_id"] = self.context.acquired_tile_id if reveal_all else None
        return value

    def _snapshot(self, observer: Seat) -> dict[str, Any]:
        state = _public_state(self.state)
        revealed_seats = (
            (*self.result.winners, *self.declared_tenpai)
            if self.result
            else self.declared_tenpai
        )
        hide_noten_human = (
            self._draw_reveal
            and observer not in self.declared_tenpai
        )
        if not hide_noten_human:
            state["seats"][observer.value]["concealed_tiles"] = [
                {"tile_id": tile.tile_id, "tile_type": tile.tile_type, "is_red": tile.is_red}
                for tile in self.state.players_by_seat[observer].concealed_hand
            ]
        for revealed_seat in revealed_seats:
            state["seats"][revealed_seat.value]["concealed_tiles"] = [
                {
                    "tile_id": tile.tile_id,
                    "tile_type": tile.tile_type,
                    "is_red": tile.is_red,
                }
                for tile in self.state.players_by_seat[revealed_seat].concealed_hand
            ]
        pending = next((item for item in self.pending if item.seat is observer), None)
        def sound_event(event: HandEvent) -> dict[str, Any]:
            public = event.public_data
            return {
                "event_id": event.event_id,
                "kind": event.kind.value,
                "actor": event.actor.value if event.actor else None,
                "action_kind": public.get("kind"),
                "adopted_actions": [
                    dict(action) for action in public.get("adopted_actions", [])
                ],
                "reason": public.get("reason"),
                "winners": public.get("winners", []),
                "declared_tenpai_player_ids": public.get("declared_tenpai_player_ids", []),
                "declaration": public.get("declaration"),
            }

        participants, seat_assignments = (
            _default_participants(self.human_seats)
            if self.participants is None
            else (self.participants, self.seat_assignments)
        )
        assert seat_assignments is not None
        result = None
        if self.result:
            ended = self.events[-2].public_data
            result = {
                "end_reason": self.result.end_reason.value,
                "winners": [seat.value for seat in self.result.winners],
                "declared_tenpai_player_ids": [
                    seat.value for seat in self.declared_tenpai
                ],
                "point_deltas": {seat.value: value for seat, value in self.result.point_deltas.items()},
                "final_scores": {seat.value: value for seat, value in self.result.final_scores.items()},
            }
            if ended.get("special_result_family"):
                result["special_hand_end"] = {
                    "family": ended["special_result_family"],
                    "reason": self.result.end_reason.value,
                    "qualifier_seats": list(ended.get("qualifier_seats", ())),
                    "point_deltas": result["point_deltas"],
                    "final_scores": result["final_scores"],
                    "rule_profile_id": self.profile.profile_id,
                    "rule_snapshot_id": self.profile.snapshot_id,
                }
        return {
            "hand_id": self.hand_id,
            "human_seat": observer.value,
            "participants": [
                {
                    "participant_id": participant.participant_id,
                    "display_name": participant.display_name,
                    "controller_id": participant.controller_id,
                }
                for participant in participants
            ],
            "seat_assignments": {
                seat.value: participant_id
                for seat, participant_id in seat_assignments.items()
            },
            "complete": self.complete,
            "event_count": len(self.events),
            "drawn_tile_id": (
                self.context.acquired_tile_id
                if self.context.phase is ActionPhase.AWAITING_DISCARD
                and self.state.current_turn is observer
                else None
            ),
            "drawn_tile_seat": (
                self.state.current_turn.value
                if self.context.phase is ActionPhase.AWAITING_DISCARD
                and self.context.acquired_tile_id is not None
                else None
            ),
            "sound_events": [
                sound_event(event) for event in self.events[-LIVE_SOUND_EVENT_WINDOW:]
            ],
            "state": state,
            "visible_waits_by_seat": {
                observer.value: self._visible_waits_by_seat[observer]
            } if not hide_noten_human else {},
            "legal_actions": [self.action_document(action) for action in pending.actions] if pending else [],
            "result": result,
            "win_detail": self.win_detail,
            "win_details": list(self.win_details),
            "revealed_seats": [seat.value for seat in revealed_seats],
        }
