"""Deterministic pause-and-resume orchestration for one east-south match."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Any, Mapping

from engine.agent_policy import POLICY_ID, VerificationStrategy
from engine.final_points import FinalPointsResult, calculate_final_points
from engine.game_state import SEATS, Seat
from engine.hand_log import HandEndReason, HandLog, HandResult, Participant
from engine.interactive_hand import InteractiveHandSession, PendingDecision
from engine.match_end import decide_match_end
from engine.match_state import MatchEndDecision, MatchEndResult, MatchState, TransitionResult
from engine.next_hand_state import initialize_next_hand_state
from engine.next_hand_transition import calculate_next_hand_transition
from engine.rule_profiles import ResolvedRuleProfile
from engine.setup import SetupResult, create_initial_state


class MatchOrchestrationError(RuntimeError):
    """A structured match-loop refusal."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


MAX_HANDS = 256
NEXT_HAND_ACTION_ID = "match-next-hand"


@dataclass(frozen=True)
class MatchRun:
    match_id: str
    seed: int
    hand_logs: tuple[HandLog, ...]
    transitions: tuple[TransitionResult, ...]
    match_end: MatchEndResult
    final_points: FinalPointsResult
    final_state: MatchState


def _derived_seed(seed: int, hand_index: int) -> int:
    digest = hashlib.sha256(
        f"chombo.match-hand-seed.v1\0{seed}\0{hand_index}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big")


def _participants(human_count: int) -> tuple[Participant, ...]:
    if not isinstance(human_count, int) or isinstance(human_count, bool) or human_count not in range(5):
        raise MatchOrchestrationError("INVALID_HUMAN_COUNT", {"human_count": human_count})
    return tuple(
        Participant(
            f"human-{index + 1}" if index < human_count else f"chombo-agent-{index - human_count + 1}",
            f"Human {index + 1}" if index < human_count else f"Chombo {index - human_count + 1}",
            "chombo.human_input.v1" if index < human_count else POLICY_ID,
        )
        for index in range(4)
    )


def _initial_assignments(seed: int, participants: tuple[Participant, ...]) -> dict[Seat, str]:
    ordered = sorted(
        participants,
        key=lambda participant: hashlib.sha256(
            f"chombo.match-seat-order.v1\0{seed}\0{participant.participant_id}".encode()
        ).digest(),
    )
    return {seat: participant.participant_id for seat, participant in zip(SEATS, ordered)}


class InteractiveMatchSession:
    """Run hands until completion, pausing only for a human controller decision."""

    def __init__(
        self,
        seed: int,
        profile: ResolvedRuleProfile,
        *,
        human_count: int = 0,
        match_id: str | None = None,
        max_hands: int = MAX_HANDS,
        agent_delay_seconds: float = 0,
        start_round_wind: Seat = Seat.EAST,
        start_hand_number: int = 1,
        start_honba: int = 0,
        start_riichi_deposits: int = 0,
        participant_scores: Mapping[str, int] | None = None,
        start_human_seat: Seat | None = None,
        start_seat_scores: Mapping[Seat, int] | None = None,
        auto_advance: bool = True,
        verification_strategy: VerificationStrategy = VerificationStrategy.NORMAL,
    ):
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise MatchOrchestrationError("INVALID_MATCH_SEED", {"seed": seed})
        if not isinstance(max_hands, int) or isinstance(max_hands, bool) or max_hands < 1:
            raise MatchOrchestrationError("INVALID_HAND_LIMIT", {"max_hands": max_hands})
        self.seed = seed
        self.profile = profile
        self.match_id = match_id or f"match-{seed}"
        if not self.match_id:
            raise MatchOrchestrationError("INVALID_MATCH_ID", {})
        self.max_hands = max_hands
        self.agent_delay_seconds = agent_delay_seconds
        self.auto_advance = auto_advance
        self.verification_strategy = verification_strategy
        self.participants = _participants(human_count)
        self.human_participant_ids = frozenset(
            participant.participant_id
            for participant in self.participants
            if participant.controller_id == "chombo.human_input.v1"
        )
        assignments = _initial_assignments(seed, self.participants)
        if start_human_seat is not None:
            if start_human_seat not in SEATS or len(self.human_participant_ids) != 1:
                raise MatchOrchestrationError("INVALID_START_HUMAN_SEAT", {"seat": str(start_human_seat)})
            human_id = next(iter(self.human_participant_ids))
            current_human_seat = next(seat for seat, participant_id in assignments.items() if participant_id == human_id)
            assignments[current_human_seat], assignments[start_human_seat] = assignments[start_human_seat], assignments[current_human_seat]
        hand_seed = _derived_seed(seed, 0)
        setup = create_initial_state(
            str(hand_seed),
            profile,
            dealer=Seat.EAST,
            round_wind=start_round_wind,
            hand_number=start_hand_number,
        )
        if participant_scores is not None and start_seat_scores is not None:
            raise MatchOrchestrationError("CONFLICTING_START_SCORES", {})
        if start_seat_scores is not None:
            if set(start_seat_scores) != set(SEATS) or any(
                not isinstance(score, int) or isinstance(score, bool)
                for score in start_seat_scores.values()
            ):
                raise MatchOrchestrationError("INVALID_START_SEAT_SCORES", {})
            scores = dict(start_seat_scores)
        elif participant_scores is None:
            scores = {seat: setup.state.players_by_seat[seat].score for seat in SEATS}
        else:
            expected_ids = {participant.participant_id for participant in self.participants}
            if set(participant_scores) != expected_ids or any(
                not isinstance(score, int) or isinstance(score, bool)
                for score in participant_scores.values()
            ):
                raise MatchOrchestrationError(
                    "INVALID_PARTICIPANT_SCORES",
                    {"participant_ids": sorted(participant_scores)},
                )
            scores = {
                seat: participant_scores[assignments[seat]] for seat in SEATS
            }
        players = tuple(
            replace(player, score=scores[player.seat]) for player in setup.state.players
        )
        setup = replace(
            setup,
            state=replace(
                setup.state,
                players=players,
                honba=start_honba,
                riichi_deposits=start_riichi_deposits,
            ),
        )
        hand_id = f"{self.match_id}-hand-0001"
        self.state = MatchState(
            self.match_id,
            self.participants,
            assignments,
            Seat.EAST,
            Seat.EAST,
            start_round_wind,
            start_hand_number,
            start_honba,
            start_riichi_deposits,
            scores,
            profile.profile_id,
            profile.snapshot_id,
            hand_id,
            setup.state,
        )
        self.hand_logs: list[HandLog] = []
        self.transitions: list[TransitionResult] = []
        self.match_end: MatchEndResult | None = None
        self.final_points: FinalPointsResult | None = None
        self._pending_next: tuple[TransitionResult, MatchEndResult] | None = None
        self.on_event = None
        self.current_hand = self._new_hand_session(setup, hand_seed)
        self.current_hand.on_event = self._publish
        if self.auto_advance:
            self.advance()

    @property
    def complete(self) -> bool:
        return self.final_points is not None

    @property
    def pending(self) -> tuple[PendingDecision, ...]:
        return () if self.complete else self.current_hand.pending

    @property
    def human_seat(self) -> Seat | None:
        seats = self._human_seats()
        return seats[0] if len(seats) == 1 else None

    @property
    def result(self) -> MatchRun | None:
        if not self.complete:
            return None
        assert self.match_end is not None and self.final_points is not None
        return MatchRun(
            self.match_id,
            self.seed,
            tuple(self.hand_logs),
            tuple(self.transitions),
            self.match_end,
            self.final_points,
            self.state,
        )

    def _human_seats(self) -> tuple[Seat, ...]:
        return tuple(
            seat for seat in SEATS
            if self.state.seat_assignments[seat] in self.human_participant_ids
        )

    def _new_hand_session(self, setup: SetupResult, hand_seed: int) -> InteractiveHandSession:
        return InteractiveHandSession(
            hand_seed,
            self.profile,
            self._human_seats(),
            hand_id=self.state.current_hand_id,
            setup=setup,
            participants=self.participants,
            seat_assignments=self.state.seat_assignments,
            agent_delay_seconds=self.agent_delay_seconds,
            auto_advance=False,
            verification_strategy=self.verification_strategy,
        )

    def _publish(self) -> None:
        if self.on_event is not None:
            self.on_event()

    def advance(self) -> None:
        """Advance agent-controlled play until human input or a result boundary."""
        if self.complete or self._pending_next is not None:
            return
        self.current_hand.advance()
        self._advance_match()

    def snapshot(self, observer: Seat) -> dict[str, Any]:
        return self._match_snapshot(self.current_hand.snapshot(observer), observer)

    def spectator_snapshot(self, *, reveal_all: bool = False) -> dict[str, Any]:
        return self._match_snapshot(
            self.current_hand.spectator_snapshot(reveal_all=reveal_all), None
        )

    def _match_snapshot(self, snapshot: dict[str, Any], observer: Seat | None) -> dict[str, Any]:
        snapshot["complete"] = self.complete
        if self._pending_next is not None:
            snapshot["legal_actions"] = [{
                "action_id": NEXT_HAND_ACTION_ID,
                "kind": "next_hand",
                "actor": observer.value,
                "tile_ids": [],
            }]
        snapshot["match"] = {
            "match_id": self.match_id,
            "format": "east_south",
            "completed_hand_count": len(self.hand_logs),
            "complete": self.complete,
            "final_points": (
                [
                    {
                        "participant_id": player.participant_id,
                        "display_name": next(
                            participant.display_name
                            for participant in self.participants
                            if participant.participant_id == player.participant_id
                        ),
                        "rank": player.rank,
                        "final_score": player.final_score,
                        "raw_points": player.raw_points,
                        "rank_points": (
                            player.oka_points
                            + player.uma_points
                            + player.other_rank_points
                        ),
                        "final_competition_points": player.final_competition_points,
                    }
                    for player in self.final_points.players
                ]
                if self.final_points is not None
                else None
            ),
            "remaining_riichi_deposits": (
                self.final_points.deposit_pool_after
                if self.final_points is not None
                else 0
            ),
        }
        if self._pending_next is not None:
            transition = self._pending_next[0]
            snapshot["match_transition"] = {
                "next_dealer": transition.next_dealer.value,
                "next_round_wind": transition.next_round_wind.value,
                "next_hand_number": transition.next_hand_number,
                "next_honba": transition.next_honba,
                "carried_riichi_deposits": transition.carried_riichi_deposits,
            }
        return snapshot

    def submit(self, seat: Seat, action_id: str) -> None:
        if self.complete:
            raise MatchOrchestrationError("MATCH_ALREADY_COMPLETE", {})
        if self._pending_next is not None:
            if action_id != NEXT_HAND_ACTION_ID or seat is not self.human_seat:
                raise MatchOrchestrationError("NEXT_HAND_ACTION_REQUIRED", {})
            transition, match_end = self._pending_next
            self._pending_next = None
            self._initialize_next_hand(transition, match_end)
            self._publish()
            return
        self.current_hand.submit(seat, action_id)
        self._advance_match()
        self._publish()

    def _advance_match(self) -> None:
        while not self.complete and self.current_hand.complete:
            if len(self.hand_logs) >= self.max_hands:
                raise MatchOrchestrationError(
                    "MATCH_HAND_LIMIT_EXCEEDED",
                    {"match_id": self.match_id, "max_hands": self.max_hands},
                )
            assert self.current_hand.log is not None and self.current_hand.result is not None
            self.hand_logs.append(self.current_hand.log)
            match_hand_result = HandResult(
                self.current_hand.result.end_reason,
                self.current_hand.result.winners,
                {
                    seat: self.current_hand.result.final_scores[seat] - self.state.scores[seat]
                    for seat in SEATS
                },
                self.current_hand.result.final_scores,
                self.current_hand.result.applied_concept_ids,
            )
            transition = calculate_next_hand_transition(
                self.state,
                match_hand_result,
                self.profile,
                riichi_deposits_after=self.current_hand.state.riichi_deposits,
                declared_tenpai=(
                    self.current_hand.declared_tenpai
                    if self.current_hand.result.end_reason
                    is HandEndReason.EXHAUSTIVE_DRAW
                    else None
                ),
            )
            self.transitions.append(transition)
            match_end = decide_match_end(self.state, transition, self.profile)
            self.match_end = match_end
            if match_end.decision is MatchEndDecision.END:
                self.final_points = calculate_final_points(self.state, match_end, self.profile)
                self.state = replace(
                    self.state,
                    scores=match_end.scores,
                    riichi_deposits=match_end.riichi_deposits,
                    current_hand=self.current_hand.state,
                )
                return

            if self.human_participant_ids:
                self._pending_next = (transition, match_end)
                return

            self._initialize_next_hand(transition, match_end)

    def _initialize_next_hand(
        self, transition: TransitionResult, match_end: MatchEndResult
    ) -> None:

        hand_index = len(self.hand_logs)
        hand_seed = _derived_seed(self.seed, hand_index)
        next_hand_id = f"{self.match_id}-hand-{hand_index + 1:04d}"
        initialized = initialize_next_hand_state(
            self.state, transition, match_end, self.profile,
            next_hand_id=next_hand_id, seed=str(hand_seed),
        )
        self.state = initialized.match_state
        setup = SetupResult(
            initialized.seed, initialized.rng_id, initialized.shuffle_spec_id,
            self.state.current_hand,
        )
        self.current_hand = self._new_hand_session(setup, hand_seed)
        self.current_hand.on_event = self._publish
        if self.auto_advance:
            self.current_hand.advance()


def run_autonomous_match(
    seed: int,
    profile: ResolvedRuleProfile,
    *,
    match_id: str | None = None,
    max_hands: int = MAX_HANDS,
    verification_strategy: VerificationStrategy = VerificationStrategy.NORMAL,
) -> MatchRun:
    session = InteractiveMatchSession(
        seed,
        profile,
        match_id=match_id,
        max_hands=max_hands,
        verification_strategy=verification_strategy,
    )
    if not session.complete or session.result is None:
        raise MatchOrchestrationError("AUTONOMOUS_MATCH_PAUSED", {})
    return session.result
