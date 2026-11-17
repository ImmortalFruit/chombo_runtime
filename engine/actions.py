"""Immutable v0.1 action and action-result contracts.

These types describe controller intents and validation outcomes.  They do not
decide mahjong legality or mutate ``GameState``; those responsibilities belong
to the later legal-action generator and state-transition engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from engine.game_state import Seat


class ActionKind(str, Enum):
    DRAW = "draw"
    DISCARD = "discard"
    CHI = "chi"
    PON = "pon"
    OPEN_KAN = "open_kan"
    CONCEALED_KAN = "concealed_kan"
    ADDED_KAN = "added_kan"
    RIICHI = "riichi"
    PASS = "pass"
    RON = "ron"
    TSUMO = "tsumo"
    TENPAI_DECLARATION = "tenpai_declaration"
    NOTEN_DECLARATION = "noten_declaration"


class ActionStatus(str, Enum):
    LEGAL = "LEGAL"
    ILLEGAL_ACTION = "ILLEGAL_ACTION"
    INSUFFICIENT_INPUT = "INSUFFICIENT_INPUT"
    RULESET_UNRESOLVED = "RULESET_UNRESOLVED"


class ActionValidationError(ValueError):
    """A malformed action-contract value, distinct from an illegal action."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class Action:
    """A structurally complete action or response intent.

    ``tile_ids`` are tiles supplied or controlled by the actor.  A tile exposed
    by another player is held separately in ``claimed_tile_id`` so later
    validation cannot confuse ownership.  Added kan references the existing pon
    by ``target_meld_index`` and supplies only the added tile in ``tile_ids``.
    """

    action_id: str
    kind: ActionKind
    actor: Seat
    tile_ids: tuple[str, ...] = ()
    claimed_tile_id: str | None = None
    target_seat: Seat | None = None
    source_event_id: str | None = None
    target_meld_index: int | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ActionValidationError("MISSING_ACTION_ID", {})
        if any(not tile_id for tile_id in self.tile_ids):
            raise ActionValidationError("INVALID_TILE_ID", {})
        referenced = self.tile_ids + ((self.claimed_tile_id,) if self.claimed_tile_id else ())
        if len(referenced) != len(set(referenced)):
            raise ActionValidationError("DUPLICATE_ACTION_TILE", {"tile_ids": referenced})
        if self.target_seat is self.actor:
            raise ActionValidationError("INVALID_ACTION_TARGET", {"seat": self.actor.value})

        expected_tiles = {
            ActionKind.DRAW: 1,
            ActionKind.DISCARD: 1,
            ActionKind.CHI: 2,
            ActionKind.PON: 2,
            ActionKind.OPEN_KAN: 3,
            ActionKind.CONCEALED_KAN: 4,
            ActionKind.ADDED_KAN: 1,
            ActionKind.RIICHI: 1,
            ActionKind.PASS: 0,
            ActionKind.RON: 0,
            ActionKind.TSUMO: 1,
            ActionKind.TENPAI_DECLARATION: 0,
            ActionKind.NOTEN_DECLARATION: 0,
        }[self.kind]
        if len(self.tile_ids) != expected_tiles:
            raise ActionValidationError(
                "INVALID_ACTION_TILE_COUNT",
                {"kind": self.kind.value, "expected": expected_tiles, "actual": len(self.tile_ids)},
            )

        response_kinds = {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN, ActionKind.PASS, ActionKind.RON}
        claimed_kinds = {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN, ActionKind.RON}
        if (self.claimed_tile_id is not None) != (self.kind in claimed_kinds):
            raise ActionValidationError("INVALID_CLAIMED_TILE", {"kind": self.kind.value})
        if (self.target_seat is not None) != (self.kind in claimed_kinds):
            raise ActionValidationError("INVALID_ACTION_TARGET", {"kind": self.kind.value})
        if (self.source_event_id is not None) != (self.kind in response_kinds):
            raise ActionValidationError("INVALID_SOURCE_EVENT", {"kind": self.kind.value})
        if self.kind in response_kinds and not self.source_event_id:
            raise ActionValidationError("INVALID_SOURCE_EVENT", {"kind": self.kind.value})
        if (self.target_meld_index is not None) != (self.kind is ActionKind.ADDED_KAN):
            raise ActionValidationError("INVALID_TARGET_MELD", {"kind": self.kind.value})
        if self.target_meld_index is not None and self.target_meld_index < 0:
            raise ActionValidationError("INVALID_TARGET_MELD", {"index": self.target_meld_index})


@dataclass(frozen=True)
class ActionResult:
    """The result of validating an action without applying a transition."""

    action: Action
    status: ActionStatus
    reason_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is ActionStatus.LEGAL and self.reason_code is not None:
            raise ActionValidationError("LEGAL_RESULT_HAS_REASON", {})
        if self.status is not ActionStatus.LEGAL and not self.reason_code:
            raise ActionValidationError("NONLEGAL_RESULT_MISSING_REASON", {"status": self.status.value})
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
