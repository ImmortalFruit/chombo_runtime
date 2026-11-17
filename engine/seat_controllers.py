"""Uniform v0.1 controller boundary for Agent and human-controlled seats.

Controllers select or validate an intent only.  They never generate legality,
apply a state transition, or retain a pending human submission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from engine.actions import Action, ActionStatus
from engine.agent_policy import POLICY_ID, VerificationStrategy, choose_action
from engine.game_state import SEATS, Seat


class ControllerKind(str, Enum):
    AGENT = "agent"
    HUMAN = "human"


class SeatControllerError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class ControllerDecision:
    """One controller's immutable selection or validation outcome."""

    seat: Seat
    controller_kind: ControllerKind
    controller_id: str
    status: ActionStatus
    action: Action | None = None
    reason_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is ActionStatus.LEGAL:
            if self.action is None or self.reason_code is not None:
                raise SeatControllerError("INVALID_LEGAL_DECISION", {})
        elif not self.reason_code:
            raise SeatControllerError(
                "NONLEGAL_DECISION_MISSING_REASON", {"status": self.status.value}
            )
        if (
            self.status is ActionStatus.LEGAL
            and self.action is not None
            and self.action.actor is not self.seat
        ):
            raise SeatControllerError(
                "CONTROLLER_ACTION_SEAT_MISMATCH",
                {"seat": self.seat.value, "actor": self.action.actor.value},
            )
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True)
class ControllerRequest:
    seat: Seat
    legal_actions: tuple[Action, ...]
    seed: str
    decision_index: int
    submitted_action: Action | None = None
    discard_shanten_by_action_id: Mapping[str, int] = field(default_factory=dict)
    discard_ukeire_by_action_id: Mapping[str, int] = field(default_factory=dict)
    yaku_directed_call_action_ids: tuple[str, ...] = ()
    preferred_action_ids: tuple[str, ...] = ()
    verification_strategy: VerificationStrategy = VerificationStrategy.NORMAL

    def __post_init__(self) -> None:
        if not self.legal_actions:
            raise SeatControllerError("EMPTY_LEGAL_ACTION_SET", {"seat": self.seat.value})
        if any(action.actor is not self.seat for action in self.legal_actions):
            raise SeatControllerError("LEGAL_ACTION_SEAT_MISMATCH", {"seat": self.seat.value})
        action_ids = tuple(action.action_id for action in self.legal_actions)
        if len(action_ids) != len(set(action_ids)):
            raise SeatControllerError(
                "DUPLICATE_LEGAL_ACTION_ID", {"seat": self.seat.value}
            )


class SeatController(Protocol):
    kind: ControllerKind
    controller_id: str

    def decide(self, request: ControllerRequest) -> ControllerDecision: ...


@dataclass(frozen=True)
class AgentSeatController:
    kind: ControllerKind = field(default=ControllerKind.AGENT, init=False)
    controller_id: str = field(default=POLICY_ID, init=False)

    def decide(self, request: ControllerRequest) -> ControllerDecision:
        action = choose_action(
            request.legal_actions,
            seed=request.seed,
            decision_index=request.decision_index,
            discard_shanten_by_action_id=request.discard_shanten_by_action_id,
            discard_ukeire_by_action_id=request.discard_ukeire_by_action_id,
            yaku_directed_call_action_ids=request.yaku_directed_call_action_ids,
            preferred_action_ids=request.preferred_action_ids,
            verification_strategy=request.verification_strategy,
        )
        return ControllerDecision(
            request.seat, self.kind, self.controller_id, ActionStatus.LEGAL, action
        )


@dataclass(frozen=True)
class HumanSeatController:
    controller_id: str = "chombo.human_input.v1"
    kind: ControllerKind = field(default=ControllerKind.HUMAN, init=False)

    def decide(self, request: ControllerRequest) -> ControllerDecision:
        submitted = request.submitted_action
        if submitted is None:
            return ControllerDecision(
                request.seat,
                self.kind,
                self.controller_id,
                ActionStatus.INSUFFICIENT_INPUT,
                reason_code="HUMAN_ACTION_REQUIRED",
            )
        if submitted not in request.legal_actions:
            return ControllerDecision(
                request.seat,
                self.kind,
                self.controller_id,
                ActionStatus.ILLEGAL_ACTION,
                submitted,
                "ACTION_NOT_IN_LEGAL_SET",
                {"legal_action_ids": tuple(a.action_id for a in request.legal_actions)},
            )
        return ControllerDecision(
            request.seat,
            self.kind,
            self.controller_id,
            ActionStatus.LEGAL,
            submitted,
        )


def build_seat_controllers(
    human_seats: Sequence[Seat],
) -> Mapping[Seat, SeatController]:
    """Assign all four seats without changing the later orchestration contract."""

    humans = tuple(human_seats)
    if len(humans) != len(set(humans)):
        raise SeatControllerError("DUPLICATE_HUMAN_SEAT", {})
    unknown = tuple(seat for seat in humans if seat not in SEATS)
    if unknown:
        raise SeatControllerError("INVALID_HUMAN_SEAT", {"seats": unknown})
    human_set = frozenset(humans)
    controllers: dict[Seat, SeatController] = {
        seat: HumanSeatController() if seat in human_set else AgentSeatController()
        for seat in SEATS
    }
    return MappingProxyType(controllers)
