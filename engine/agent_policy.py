"""Deterministic win-first, shanten-preserving policy over legal actions.

The policy deliberately contains no mahjong legality or strategy.  It always
accepts a qualified win and otherwise selects one of the supplied legal
actions from a portable seed-derived digest.
"""

from __future__ import annotations

from hashlib import sha256
from enum import Enum
from typing import Mapping, Sequence

from engine.actions import Action, ActionKind


POLICY_ID = "chombo.win_first_yaku_call_min_shanten_max_ukeire_seeded.v5"


class VerificationStrategy(str, Enum):
    NORMAL = "normal"
    ABORTIVE_OPENING_AND_NAGASHI = "abortive_opening_and_nagashi"
    CALL_AND_KAN = "call_and_kan"
    FOUR_RIICHI = "four_riichi"
    OPEN_HAND_THREE_RON = "open_hand_three_ron"


class AgentPolicyError(ValueError):
    def __init__(self, code: str, details: Mapping[str, object]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _selection_index(seed: str, decision_index: int, actions: tuple[Action, ...]) -> int:
    action_ids = "\x00".join(action.action_id for action in actions)
    payload = (
        b"chombo.minimal_agent_policy.v1\x00"
        + seed.encode("utf-8")
        + b"\x00"
        + str(decision_index).encode("ascii")
        + b"\x00"
        + action_ids.encode("utf-8")
    )
    return int.from_bytes(sha256(payload).digest(), "big") % len(actions)


def choose_action(
    legal_actions: Sequence[Action], *, seed: str, decision_index: int,
    discard_shanten_by_action_id: Mapping[str, int] | None = None,
    discard_ukeire_by_action_id: Mapping[str, int] | None = None,
    yaku_directed_call_action_ids: Sequence[str] = (),
    preferred_action_ids: Sequence[str] = (),
    verification_strategy: VerificationStrategy = VerificationStrategy.NORMAL,
) -> Action:
    """Choose one supplied legal action reproducibly.

    ``decision_index`` is the zero-based global decision number maintained by
    the future hand orchestrator.  Including the stable candidate action IDs
    makes the selection depend on the exact legal set rather than container
    iteration order.
    """

    if not isinstance(seed, str) or not seed:
        raise AgentPolicyError("INVALID_POLICY_SEED", {})
    if not isinstance(decision_index, int) or isinstance(decision_index, bool) or decision_index < 0:
        raise AgentPolicyError(
            "INVALID_DECISION_INDEX", {"decision_index": decision_index}
        )
    if not legal_actions:
        raise AgentPolicyError("EMPTY_LEGAL_ACTION_SET", {})

    ordered = tuple(sorted(legal_actions, key=lambda action: action.action_id))
    action_ids = tuple(action.action_id for action in ordered)
    if len(action_ids) != len(set(action_ids)):
        raise AgentPolicyError("DUPLICATE_LEGAL_ACTION_ID", {"action_ids": action_ids})
    actors = {action.actor for action in ordered}
    if len(actors) != 1:
        raise AgentPolicyError(
            "MIXED_ACTION_ACTORS", {"actors": tuple(sorted(seat.value for seat in actors))}
        )

    wins = tuple(
        action for action in ordered if action.kind in {ActionKind.RON, ActionKind.TSUMO}
    )
    kans = tuple(
        action
        for action in ordered
        if action.kind
        in {ActionKind.CONCEALED_KAN, ActionKind.ADDED_KAN}
    )
    riichi = tuple(action for action in ordered if action.kind is ActionKind.RIICHI)
    preferred_call_ids = frozenset(yaku_directed_call_action_ids)
    calls = tuple(
        action for action in ordered
        if action.kind in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}
        and action.action_id in preferred_call_ids
    )
    passes = tuple(action for action in ordered if action.kind is ActionKind.PASS)
    preferred_ids = frozenset(preferred_action_ids)
    preferred = tuple(action for action in ordered if action.action_id in preferred_ids)
    all_calls = tuple(
        action for action in ordered
        if action.kind in {ActionKind.CHI, ActionKind.PON, ActionKind.OPEN_KAN}
    )
    open_kans = tuple(
        action for action in ordered if action.kind is ActionKind.OPEN_KAN
    )
    if verification_strategy is VerificationStrategy.ABORTIVE_OPENING_AND_NAGASHI:
        candidates = preferred or passes or wins or kans or riichi or calls or ordered
    elif verification_strategy is VerificationStrategy.CALL_AND_KAN:
        candidates = kans or wins or open_kans or calls or passes or preferred or riichi or ordered
    elif verification_strategy is VerificationStrategy.FOUR_RIICHI:
        candidates = riichi or wins or passes or preferred or kans or calls or ordered
    elif verification_strategy is VerificationStrategy.OPEN_HAND_THREE_RON:
        candidates = preferred or riichi or wins or passes or calls or kans or ordered
    else:
        candidates = wins or kans or riichi or calls or passes or ordered
    if not wins and not kans and not riichi and discard_shanten_by_action_id:
        scored_discards = tuple(
            action for action in candidates
            if action.kind is ActionKind.DISCARD and action.action_id in discard_shanten_by_action_id
        )
        if scored_discards:
            best = min(discard_shanten_by_action_id[action.action_id] for action in scored_discards)
            candidates = tuple(
                action for action in scored_discards
                if discard_shanten_by_action_id[action.action_id] == best
            )
            if discard_ukeire_by_action_id:
                best_ukeire = max(discard_ukeire_by_action_id.get(action.action_id, -1) for action in candidates)
                candidates = tuple(
                    action for action in candidates
                    if discard_ukeire_by_action_id.get(action.action_id, -1) == best_ukeire
                )
    return candidates[_selection_index(seed, decision_index, candidates)]
