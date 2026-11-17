"""U40-03 authorization and immutable projection for condition support in Replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from engine.condition_documents import canonical_bytes, document_hash
from engine.condition_skill import calculate_condition_documents
from engine.live_condition_support import (
    DEFAULT_CONCEPT_REFS,
    LiveConditionResult,
    LiveConditionSupport,
    _envelope,
    _seal,
)
from engine.match_orchestrator import InteractiveMatchSession
from engine.navigator import ReplaySession
from engine.rule_profiles import ResolvedRuleProfile


REPLAY_SUPPORT_VERSION = "chombo.replay-condition-support.v0.4.0"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ReplayGrant:
    document: Mapping[str, Any]
    revision: int


class ReplayConditionSupport:
    """Keep completed-Replay grants separate from display and conversation grants."""

    def __init__(
        self, *, build_revision: str,
        now: Callable[[], datetime] | None = None,
        calculator: Callable[..., Mapping[str, Any]] = calculate_condition_documents,
        grant_ttl_seconds: int = 900,
    ) -> None:
        if len(build_revision) != 40 or any(char not in "0123456789abcdef" for char in build_revision):
            raise ValueError("INVALID_BUILD_REVISION")
        self.build_revision = build_revision
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calculator = calculator
        self.grant_ttl_seconds = grant_ttl_seconds
        self._grants: dict[str, ReplayGrant] = {}
        self._cancelled: dict[str, int] = {}
        self._results: dict[str, LiveConditionResult] = {}
        self._active_support = LiveConditionSupport(
            build_revision=build_revision, now=self.now, calculator=calculator,
        )
        self.projection_count = 0
        self.calculation_count = 0

    def issue_grant(
        self, replay: ReplaySession, *, user_id: str, scope: str,
        allowed_participant_ids: Iterable[str],
    ) -> Mapping[str, Any]:
        participants = {item["participant_id"] for item in replay.record["participants"]}
        allowed = tuple(dict.fromkeys(allowed_participant_ids))
        if not user_id or scope not in {"SELF_SEAT", "ALL_HANDS"}:
            raise ValueError("INVALID_REPLAY_GRANT")
        if not allowed or not set(allowed) <= participants or (scope == "SELF_SEAT" and len(allowed) != 1):
            raise ValueError("INVALID_REPLAY_GRANT")
        created = self.now().astimezone(timezone.utc)
        grant_id = str(uuid4())
        document = _seal({
            **_envelope("CONDITION_REPLAY_GRANT_REF", created_at=created.isoformat().replace("+00:00", "Z"),
                        build_revision=self.build_revision),
            "grant_id": grant_id,
            "user_id": user_id,
            "record_hash": replay.record["integrity"]["document_hash"],
            "scope": scope,
            "allowed_participant_ids": list(allowed),
            "expires_at": (created + timedelta(seconds=self.grant_ttl_seconds)).isoformat().replace("+00:00", "Z"),
            "cancellation_revision": 0,
        })
        self._grants[grant_id] = ReplayGrant(deepcopy(document), 0)
        return deepcopy(document)

    def revoke_grant(self, grant_id: str) -> None:
        stored = self._grants.get(grant_id)
        if stored is not None:
            revision = stored.revision + 1
            self._cancelled[grant_id] = revision
            self._grants[grant_id] = ReplayGrant(stored.document, revision)

    def calculate_active(
        self, session: InteractiveMatchSession, *, selected_event_sequence: int,
        **arguments: Any,
    ) -> LiveConditionResult:
        """ACTIVE_REPLAY may use only the latest confirmed event and live self policy."""
        latest = len(session.current_hand.events) - 1
        if arguments.get("condition_ref_id") is None and selected_event_sequence != latest:
            return LiveConditionResult(status="DENIED", reason_code="ACTIVE_REPLAY_PAST_EVENT")
        projections_before = self._active_support.projection_count
        calculations_before = self._active_support.calculation_count
        result = self._active_support.calculate(
            session, _mode="ACTIVE_REPLAY", _source_kind="ACTIVE_SESSION_REPLAY", **arguments,
        )
        self.projection_count += self._active_support.projection_count - projections_before
        self.calculation_count += self._active_support.calculation_count - calculations_before
        return result

    def calculate_completed(
        self, replay: ReplaySession, *, user_id: str, grant_id: str | None,
        requested_participant_id: str, target_rank: int,
        profile: ResolvedRuleProfile, match_rule_ref: Mapping[str, Any],
        qualified_concepts: frozenset[str],
        concept_refs: Iterable[str] = DEFAULT_CONCEPT_REFS,
        comeback_tie_policy: str = "REQUIRE_STRICT_LEAD",
        allowed_result_kinds: Iterable[str] = (
            "TARGET_RON", "TARGET_DEALT_IN", "TARGET_TSUMO", "OTHER_WIN", "DRAW",
        ),
        max_depth: int = 1, max_candidates: int = 10_000,
        max_transitions: int = 4_096, max_wall_time_ms: int = 60_000,
        condition_ref_id: str | None = None,
    ) -> LiveConditionResult:
        if grant_id is None:
            return LiveConditionResult(status="DENIED", reason_code="REPLAY_GRANT_REQUIRED")
        stored = self._grants.get(grant_id)
        if stored is None:
            return LiveConditionResult(status="DENIED", reason_code="REPLAY_GRANT_INVALID")
        grant = stored.document
        if grant["user_id"] != user_id or grant["record_hash"] != replay.record["integrity"]["document_hash"]:
            return LiveConditionResult(status="DENIED", reason_code="REPLAY_GRANT_INVALID")
        if stored.revision != grant["cancellation_revision"] or grant_id in self._cancelled:
            return LiveConditionResult(status="REFERENCE_INVALID", reason_code="REPLAY_GRANT_REVOKED")
        expires = datetime.fromisoformat(str(grant["expires_at"]).replace("Z", "+00:00"))
        if self.now().astimezone(timezone.utc) > expires:
            return LiveConditionResult(status="REFERENCE_INVALID", reason_code="REFERENCE_EXPIRED")
        if requested_participant_id not in grant["allowed_participant_ids"]:
            return LiveConditionResult(status="DENIED", reason_code="SUBJECT_MISMATCH")
        selected = replay.record["rules"]["selected_ref"]
        if (selected["profile_id"], selected["snapshot_id"]) != (profile.profile_id, profile.snapshot_id):
            return LiveConditionResult(status="DENIED", reason_code="RULE_SNAPSHOT_MISMATCH")
        if (match_rule_ref.get("profile_id"), match_rule_ref.get("snapshot_id")) != (
            selected["profile_id"], selected["snapshot_id"],
        ):
            return LiveConditionResult(status="DENIED", reason_code="RULE_SNAPSHOT_MISMATCH")

        intent = {
            "user_id": user_id, "target": requested_participant_id, "target_rank": target_rank,
            "tie_policy": comeback_tie_policy, "allowed_result_kinds": list(allowed_result_kinds),
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
            authority = ref.get("authority", {})
            source = ref.get("source", {})
            if (
                authority.get("user_id") != user_id
                or authority.get("grant_id") != grant_id
                or authority.get("requested_participant_id") != requested_participant_id
                or source.get("record_hash") != replay.record["integrity"]["document_hash"]
                or ref.get("request_digest") != _digest(intent)
                or ref.get("rules_ref") != dict(match_rule_ref)
            ):
                return LiveConditionResult(
                    status="REFERENCE_INVALID", reason_code="CONDITION_REFERENCE_MISMATCH",
                )
            return deepcopy(cached)

        raw_hands = ([replay.record["payload"]["hand"]] if replay.record["document_kind"] == "HAND_RECORD"
                     else replay.record["payload"]["hands"])
        raw_hand = raw_hands[replay.hand_index]
        frame = replay.current_hand()["frames"][replay.event_index]
        public = frame["state"]
        assignments = raw_hand["seat_assignments"]
        if requested_participant_id not in assignments.values():
            return LiveConditionResult(status="DENIED", reason_code="SUBJECT_MISMATCH")

        self.projection_count += 1
        created_at = self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        payload = replay.record["payload"]
        match_id = payload.get("match_id") or replay.record["record_id"]
        position = {
            "match_id": match_id, "hand_id": raw_hand["hand_id"],
            "event_id": frame["event_id"], "event_sequence": frame["sequence"],
        }
        scores = {seat: int(public["seats"][seat]["score"]) for seat in assignments}
        state_digest = _digest({
            "record_hash": replay.record["integrity"]["document_hash"], "position": position,
            "seat_assignments": assignments, "scores": scores,
            "round": [public["round_wind"], public["hand_number"], public["dealer"],
                      public["honba"], public["riichi_deposits"]], "rules": dict(match_rule_ref),
        })
        state_id = f"condition:{replay.record['record_id']}:{raw_hand['hand_id']}:{frame['event_id']}"
        completed = (replay.hand_index == replay.hand_count - 1
                     and replay.event_index == len(replay.current_hand()["frames"]) - 1)
        state = _seal({
            **_envelope("CONDITION_STATE", created_at=created_at, build_revision=self.build_revision),
            "state_id": state_id,
            "position": {**position, "state_digest": state_digest},
            "participants": [{"participant_id": assignments[seat], "seat": seat.upper()} for seat in assignments],
            "scores": [{"participant_id": assignments[seat], "value": scores[seat]} for seat in assignments],
            "round_state": {
                "round_wind": str(public["round_wind"]).upper(), "hand_number": int(public["hand_number"]),
                "dealer_participant_id": assignments[str(public["dealer"])],
                "honba": int(public["honba"]), "riichi_deposits": int(public["riichi_deposits"]) // 1000,
                "match_completion_state": "COMPLETED" if completed else "IN_PROGRESS",
                "initial_score_total": sum(scores.values()) + int(public["riichi_deposits"]),
            },
        })
        condition_ref = _seal({
            **_envelope("CONDITION_SUPPORT_REF", created_at=created_at, build_revision=self.build_revision),
            "condition_ref_id": f"condition-ref:{uuid4()}",
            "source": {**position, "source_kind": "COMPLETED_REPLAY",
                       "record_hash": replay.record["integrity"]["document_hash"]},
            "authority": {"mode": "COMPLETED_REPLAY", "user_id": user_id,
                          "requested_participant_id": requested_participant_id,
                          "grant_id": grant_id, "grant_scope": grant["scope"]},
            "state_ref": {"state_id": state_id, "state_digest": state_digest},
            "rules_ref": deepcopy(dict(match_rule_ref)), "request_digest": _digest(intent),
            "grant_ref": {"grant_id": grant_id, "grant_digest": document_hash(grant),
                          "cancellation_revision": grant["cancellation_revision"]},
        })
        request = _seal({
            **_envelope("CONDITION_REQUEST", created_at=created_at, build_revision=self.build_revision),
            "request_id": str(uuid4()), "calculation_basis": "CURRENT_HAND_COMEBACK",
            "ranking_scope": "SINGLE_MATCH_RANK",
            "target": {"participant_id": requested_participant_id, "target_rank": target_rank,
                       "comeback_tie_policy": comeback_tie_policy},
            "position_source": {"kind": "SUPPORT_REF", "condition_ref_id": condition_ref["condition_ref_id"],
                                "condition_ref_digest": document_hash(condition_ref), "evaluation_point": "HAND_END"},
            "match_rule_ref": deepcopy(dict(match_rule_ref)), "concept_refs": intent["concept_refs"],
            "provenance": {"source": "COMPLETED_REPLAY", "condition_ref_id": condition_ref["condition_ref_id"]},
            "search_scope": {"allowed_result_kinds": intent["allowed_result_kinds"], "max_depth": max_depth,
                             "max_candidates": max_candidates, "max_transitions": max_transitions,
                             "max_wall_time_ms": max_wall_time_ms},
        })
        self.calculation_count += 1
        response = self.calculator(request, profile, qualified_concepts=qualified_concepts,
                                   state=state, support_ref=condition_ref)
        result = LiveConditionResult(status="ALLOWED", condition_ref=condition_ref, state=state,
                                     request=request, response=response)
        self._results[condition_ref["condition_ref_id"]] = deepcopy(result)
        return result
