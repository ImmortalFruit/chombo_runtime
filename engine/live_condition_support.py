"""U40-02 live self-condition authorization and immutable projection boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from engine.condition_documents import (
    FORMAT_VERSION,
    INTEGRITY_ALGORITHM,
    SCHEMA_IDS,
    canonical_bytes,
    document_hash,
)
from engine.condition_skill import calculate_condition_documents
from engine.game_state import SEATS
from engine.match_orchestrator import InteractiveMatchSession
from engine.rule_profiles import ResolvedRuleProfile


LIVE_SUPPORT_VERSION = "chombo.live-condition-support.v0.4.0"
DEFAULT_CONCEPT_REFS = (
    "game.condition.single_match_rank_condition",
    "game.condition.reachability",
    "game.condition.minimal_condition",
    "game.condition.candidate_dominance",
)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    document["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "document_hash": "sha256:" + "0" * 64,
    }
    document["integrity"]["document_hash"] = document_hash(document)
    return document


def _envelope(kind: str, *, created_at: str, build_revision: str) -> dict[str, Any]:
    return {
        "document_kind": kind,
        "schema_id": SCHEMA_IDS[kind],
        "format_version": FORMAT_VERSION,
        "created_at": created_at,
        "producer": {
            "app_id": "chombo-live-condition-support",
            "app_version": "0.4.0",
            "build_revision": build_revision,
            "engine_version": LIVE_SUPPORT_VERSION,
        },
        "integrity": {},
    }


@dataclass(frozen=True)
class LiveConditionResult:
    status: str
    reason_code: str | None = None
    condition_ref: Mapping[str, Any] | None = None
    state: Mapping[str, Any] | None = None
    request: Mapping[str, Any] | None = None
    response: Mapping[str, Any] | None = None


class LiveConditionSupport:
    """Authorize one live user's own target before projecting or calculating."""

    def __init__(
        self,
        *,
        build_revision: str,
        now: Callable[[], datetime] | None = None,
        calculator: Callable[..., Mapping[str, Any]] = calculate_condition_documents,
    ) -> None:
        if len(build_revision) != 40 or any(char not in "0123456789abcdef" for char in build_revision):
            raise ValueError("INVALID_BUILD_REVISION")
        self.build_revision = build_revision
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calculator = calculator
        self._results: dict[str, LiveConditionResult] = {}
        self._result_sessions: dict[str, int] = {}
        self.projection_count = 0
        self.calculation_count = 0

    @staticmethod
    def _authorize(
        session: InteractiveMatchSession,
        owned_participant_ids: Iterable[str],
        requested_participant_id: str,
    ) -> str | None:
        participants = {item.participant_id for item in session.participants}
        owned = tuple(dict.fromkeys(owned_participant_ids))
        resolved = tuple(item for item in owned if item in participants)
        if len(owned) != 1 or len(resolved) != 1:
            return "OWNED_PARTICIPANT_UNRESOLVED"
        if requested_participant_id != resolved[0]:
            return "OTHER_PARTICIPANT_DURING_ACTIVE_PLAY"
        return None

    def calculate(
        self,
        session: InteractiveMatchSession,
        *,
        user_id: str,
        owned_participant_ids: Iterable[str],
        requested_participant_id: str,
        target_rank: int,
        match_rule_ref: Mapping[str, Any],
        qualified_concepts: frozenset[str],
        concept_refs: Iterable[str] = DEFAULT_CONCEPT_REFS,
        comeback_tie_policy: str = "REQUIRE_STRICT_LEAD",
        allowed_result_kinds: Iterable[str] = (
            "TARGET_RON", "TARGET_DEALT_IN", "TARGET_TSUMO", "OTHER_WIN", "DRAW",
        ),
        max_depth: int = 1,
        max_candidates: int = 10_000,
        max_transitions: int = 4_096,
        max_wall_time_ms: int = 60_000,
        condition_ref_id: str | None = None,
        _mode: str = "PLAYING",
        _source_kind: str = "LIVE_SESSION",
    ) -> LiveConditionResult:
        """Return a candidate-free denial, or calculate from one fixed live event."""
        if (_mode, _source_kind) not in {
            ("PLAYING", "LIVE_SESSION"),
            ("ACTIVE_REPLAY", "ACTIVE_SESSION_REPLAY"),
        }:
            return LiveConditionResult(status="REFERENCE_INVALID", reason_code="MODE_INVALID")
        reason = self._authorize(session, owned_participant_ids, requested_participant_id)
        if reason is not None:
            return LiveConditionResult(status="DENIED", reason_code=reason)
        if not user_id:
            return LiveConditionResult(status="REFERENCE_INVALID", reason_code="USER_UNRESOLVED")
        if (
            match_rule_ref.get("profile_id"), match_rule_ref.get("snapshot_id")
        ) != (session.profile.profile_id, session.profile.snapshot_id):
            return LiveConditionResult(status="DENIED", reason_code="RULE_SNAPSHOT_MISMATCH")

        intent = {
            "user_id": user_id,
            "target": requested_participant_id,
            "target_rank": target_rank,
            "tie_policy": comeback_tie_policy,
            "allowed_result_kinds": list(allowed_result_kinds),
            "concept_refs": list(concept_refs),
            "limits": [max_depth, max_candidates, max_transitions, max_wall_time_ms],
        }
        if condition_ref_id is not None:
            cached = self._results.get(condition_ref_id)
            if cached is None:
                return LiveConditionResult(
                    status="REFERENCE_INVALID", reason_code="CONDITION_REFERENCE_INVALID",
                )
            ref = cached.condition_ref or {}
            source = ref.get("source", {})
            authority = ref.get("authority", {})
            if (
                source.get("match_id") != session.match_id
                or self._result_sessions.get(condition_ref_id) != id(session)
                or authority.get("mode") != _mode
                or authority.get("user_id") != user_id
                or authority.get("requested_participant_id") != requested_participant_id
                or ref.get("request_digest") != _digest(intent)
                or ref.get("rules_ref") != dict(match_rule_ref)
            ):
                return LiveConditionResult(
                    status="REFERENCE_INVALID", reason_code="CONDITION_REFERENCE_MISMATCH",
                )
            return deepcopy(cached)

        if session.complete:
            return LiveConditionResult(status="POSITION_UNAVAILABLE", reason_code="LIVE_MATCH_COMPLETED")

        self.projection_count += 1
        created_at = self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        event = session.current_hand.events[-1]
        hand = session.current_hand.state
        state_id = f"condition:{session.match_id}:{session.state.current_hand_id}:{event.event_id}"
        position_payload = {
            "match_id": session.match_id,
            "hand_id": session.state.current_hand_id,
            "event_id": event.event_id,
            "event_sequence": event.sequence,
        }
        state_digest = _digest({
            "position": position_payload,
            "seat_assignments": {
                seat.value: session.state.seat_assignments[seat] for seat in SEATS
            },
            "scores": {seat.value: hand.players_by_seat[seat].score for seat in SEATS},
            "round": [hand.round_wind.value, hand.hand_number, hand.dealer.value,
                      hand.honba, hand.riichi_deposits],
            "rules": dict(match_rule_ref),
        })
        state = _seal({
            **_envelope("CONDITION_STATE", created_at=created_at, build_revision=self.build_revision),
            "state_id": state_id,
            "position": {**position_payload, "state_digest": state_digest},
            "participants": [
                {
                    "participant_id": session.state.seat_assignments[seat],
                    "seat": seat.value.upper(),
                }
                for seat in SEATS
            ],
            "scores": [
                {
                    "participant_id": session.state.seat_assignments[seat],
                    "value": hand.players_by_seat[seat].score,
                }
                for seat in SEATS
            ],
            "round_state": {
                "round_wind": hand.round_wind.value.upper(),
                "hand_number": hand.hand_number,
                "dealer_participant_id": session.state.seat_assignments[hand.dealer],
                "honba": hand.honba,
                "riichi_deposits": hand.riichi_deposits // 1000,
                "match_completion_state": "IN_PROGRESS",
                "initial_score_total": (
                    sum(player.score for player in hand.players) + hand.riichi_deposits
                ),
            },
        })
        condition_ref = _seal({
            **_envelope("CONDITION_SUPPORT_REF", created_at=created_at, build_revision=self.build_revision),
            "condition_ref_id": f"condition-ref:{uuid4()}",
            "source": {**position_payload, "source_kind": _source_kind},
            "authority": {
                "mode": _mode, "user_id": user_id,
                "owned_participant_id": requested_participant_id,
                "requested_participant_id": requested_participant_id,
            },
            "state_ref": {"state_id": state_id, "state_digest": state_digest},
            "rules_ref": deepcopy(dict(match_rule_ref)),
            "request_digest": _digest(intent),
            "grant_ref": None,
        })
        result_kinds = intent["allowed_result_kinds"]
        request = _seal({
            **_envelope("CONDITION_REQUEST", created_at=created_at, build_revision=self.build_revision),
            "request_id": str(uuid4()),
            "calculation_basis": "CURRENT_HAND_COMEBACK",
            "ranking_scope": "SINGLE_MATCH_RANK",
            "target": {
                "participant_id": requested_participant_id,
                "target_rank": target_rank,
                "comeback_tie_policy": comeback_tie_policy,
            },
            "position_source": {
                "kind": "SUPPORT_REF",
                "condition_ref_id": condition_ref["condition_ref_id"],
                "condition_ref_digest": document_hash(condition_ref),
                "evaluation_point": "HAND_END",
            },
            "match_rule_ref": deepcopy(dict(match_rule_ref)),
            "concept_refs": intent["concept_refs"],
            "provenance": {
                "source": _source_kind, "condition_ref_id": condition_ref["condition_ref_id"]
            },
            "search_scope": {
                "allowed_result_kinds": result_kinds,
                "max_depth": max_depth,
                "max_candidates": max_candidates,
                "max_transitions": max_transitions,
                "max_wall_time_ms": max_wall_time_ms,
            },
        })

        self.calculation_count += 1
        response = self.calculator(
            request, session.profile, qualified_concepts=qualified_concepts,
            state=state, support_ref=condition_ref,
        )
        result = LiveConditionResult(
            status="ALLOWED", condition_ref=condition_ref, state=state,
            request=request, response=response,
        )
        self._results[condition_ref["condition_ref_id"]] = deepcopy(result)
        self._result_sessions[condition_ref["condition_ref_id"]] = id(session)
        return result
