"""Immutable, observer-scoped position conversation gate (U3-05)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping
from uuid import uuid4

from engine.local_calculations import conversation_shape_analysis


ANALYSIS_CONCEPT_IDS = (
    "hand.complete.standard", "hand.complete.seven_pairs",
    "hand.complete.thirteen_orphans", "hand.shanten", "hand.tenpai",
    "hand.wait", "hand.wait.in_hand_copy_limit", "hand.ukeire",
    "hand.ukeire.in_hand_copy_count",
)
ACTIVE_MODES = frozenset({"PLAYING", "ACTIVE_REPLAY"})
SUBJECTS = frozenset({"SELF", "OTHER_SEAT", "PUBLIC_TABLE", "AMBIGUOUS"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _tile_notation(tiles: list[Mapping[str, Any]]) -> str:
    groups = {suit: [] for suit in "mpsz"}
    for tile in sorted(tiles, key=lambda item: ("mpsz".index(str(item["tile_type"])[1]), int(str(item["tile_type"])[0]))):
        tile_type = str(tile["tile_type"])
        groups[tile_type[1]].append(tile_type[0])
    return "".join("".join(groups[suit]) + suit for suit in "mpsz" if groups[suit])


@dataclass(frozen=True)
class PositionConversationRef:
    conversation_ref_id: str
    source_kind: str
    session_id: str | None
    lineage_id: str | None
    checkpoint_id: str | None
    record_id: str
    hand_id: str
    event_id: str | None
    event_sequence: int
    mode: str
    observer_kind: str
    observer_seat: str | None
    requested_subject: str
    subject_seat: str | None
    permission_grant_id: str | None
    rule_profile_id: str
    rule_snapshot_id: str
    projection_digest: str
    created_at: str
    expires_at: str

    def public_metadata(self) -> dict[str, Any]:
        return {
            "conversation_ref_id": self.conversation_ref_id,
            "source_kind": self.source_kind, "session_id": self.session_id,
            "lineage_id": self.lineage_id, "checkpoint_id": self.checkpoint_id,
            "record_id": self.record_id, "hand_id": self.hand_id,
            "event_id": self.event_id, "event_sequence": self.event_sequence,
            "mode": self.mode, "observer_kind": self.observer_kind,
            "observer_seat": self.observer_seat, "requested_subject": self.requested_subject,
            "subject_seat": self.subject_seat, "rule_profile_id": self.rule_profile_id,
            "rule_snapshot_id": self.rule_snapshot_id,
        }


class PositionConversationGate:
    """Hold fixed permitted projections and gate all calculation behind policy."""

    def __init__(self, *, ttl_seconds: int = 900, now=None):
        self.ttl_seconds = ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._refs: dict[str, tuple[PositionConversationRef, dict[str, Any]]] = {}
        self._revoked_grants: set[str] = set()

    def issue(self, *, source_kind: str, mode: str, observer_kind: str,
              observer_seat: str | None, requested_subject: str,
              subject_seat: str | None, projection: Mapping[str, Any],
              session_id: str | None, lineage_id: str | None,
              checkpoint_id: str | None, record_id: str, hand_id: str,
              event_id: str | None, event_sequence: int,
              rule_profile_id: str, rule_snapshot_id: str,
              permission_grant_id: str | None = None) -> PositionConversationRef:
        if requested_subject not in SUBJECTS:
            raise ValueError("INVALID_CONVERSATION_SUBJECT")
        seats = {"east", "south", "west", "north"}
        if subject_seat is not None and subject_seat not in seats:
            raise ValueError("INVALID_SUBJECT_SEAT")
        if requested_subject == "OTHER_SEAT" and subject_seat is None:
            raise ValueError("SUBJECT_SEAT_REQUIRED")
        created = self.now()
        fixed = deepcopy(dict(projection))
        ref = PositionConversationRef(
            str(uuid4()), source_kind, session_id, lineage_id, checkpoint_id,
            record_id, hand_id, event_id, event_sequence, mode, observer_kind,
            observer_seat, requested_subject, subject_seat, permission_grant_id,
            rule_profile_id, rule_snapshot_id, _digest(fixed), created.isoformat(),
            (created + timedelta(seconds=self.ttl_seconds)).isoformat(),
        )
        self._refs[ref.conversation_ref_id] = (ref, fixed)
        return ref

    def revoke(self, permission_grant_id: str) -> None:
        self._revoked_grants.add(permission_grant_id)

    def resolve_subject(self, conversation_ref_id: str, subject: str) -> PositionConversationRef:
        """Narrow an ambiguous reference without refreshing its event or TTL.

        A confirmation never assigns a seat or adds a Replay grant. The root
        remains immutable; the derived reference uses the same fixed input.
        """
        decision = self.authorize(conversation_ref_id)
        if decision.get("reason_code") != "SUBJECT_AMBIGUOUS":
            raise ValueError(decision.get("reason_code", "CONFIRMATION_NOT_REQUIRED"))
        if subject not in {"SELF", "PUBLIC_TABLE"}:
            raise ValueError("INVALID_CONFIRMATION_CHOICE")
        root, projection = self._refs[conversation_ref_id]
        ref = replace(root, conversation_ref_id=str(uuid4()), requested_subject=subject, subject_seat=None)
        self._refs[ref.conversation_ref_id] = (ref, deepcopy(projection))
        return ref

    def reference_metadata(self, conversation_ref_id: str) -> dict[str, Any] | None:
        """Return a valid public API reference, never a grant or projection."""
        decision = self.authorize(conversation_ref_id)
        if decision["status"] != "ALLOWED":
            return None
        ref, _ = self._refs[conversation_ref_id]
        return {**ref.public_metadata(), "projection_digest": ref.projection_digest,
                "expires_at": ref.expires_at}

    def authorize(self, conversation_ref_id: str) -> dict[str, Any]:
        """Check the fixed reference and subject without calculating or exposing it."""
        stored = self._refs.get(conversation_ref_id)
        if stored is None:
            return self._result("REFERENCE_INVALID", "REFERENCE_NOT_FOUND")
        ref, projection = stored
        metadata = ref.public_metadata()
        if self.now() > datetime.fromisoformat(ref.expires_at):
            return self._result("REFERENCE_INVALID", "REFERENCE_EXPIRED", metadata)
        if _digest(projection) != ref.projection_digest:
            return self._result("REFERENCE_INVALID", "PROJECTION_MISMATCH", metadata)
        if ref.permission_grant_id in self._revoked_grants:
            return self._result("REFERENCE_INVALID", "REPLAY_PERMISSION_REQUIRED", metadata)
        if ref.requested_subject == "AMBIGUOUS":
            return self._result("CLARIFICATION_REQUIRED", "SUBJECT_AMBIGUOUS", metadata)
        if ref.requested_subject == "SELF" and ref.observer_seat is None:
            return self._result("CLARIFICATION_REQUIRED", "SELF_SEAT_UNRESOLVED", metadata)
        if ref.mode in ACTIVE_MODES and ref.requested_subject == "OTHER_SEAT":
            return self._result("DENIED", "OTHER_SEAT_DURING_ACTIVE_PLAY", metadata)
        if projection.get("state", {}).get("rule_profile_id", ref.rule_profile_id) != ref.rule_profile_id:
            return self._result("REFERENCE_INVALID", "RULE_SNAPSHOT_MISMATCH", metadata)
        if projection.get("state", {}).get("rule_snapshot_id", ref.rule_snapshot_id) != ref.rule_snapshot_id:
            return self._result("REFERENCE_INVALID", "RULE_SNAPSHOT_MISMATCH", metadata)
        if ref.requested_subject != "PUBLIC_TABLE":
            seat = ref.observer_seat if ref.requested_subject == "SELF" else ref.subject_seat
            if ref.observer_kind != "ALL_HANDS" and seat != ref.observer_seat:
                return self._result("DENIED", "REPLAY_PERMISSION_REQUIRED", metadata)
            concealed = projection.get("state", {}).get("seats", {}).get(seat or "", {}).get("concealed_tiles")
            if not isinstance(concealed, list):
                return self._result("DENIED", "REPLAY_PERMISSION_REQUIRED", metadata)
        return {"status": "ALLOWED", **metadata}

    def snapshot(self, conversation_ref_id: str, *, subject_only: bool = False) -> dict[str, Any]:
        """Gate the snapshot; common API requests minimize to the question subject.

        The existing explicit all-hands clipboard export remains compatible.
        New application adapters always select subject_only=True.
        """
        decision = self.authorize(conversation_ref_id)
        if decision["status"] != "ALLOWED":
            return decision
        ref, projection = self._refs[conversation_ref_id]
        metadata = ref.public_metadata()
        state = deepcopy(projection.get("state", projection))
        visible_seat = (None if ref.requested_subject == "PUBLIC_TABLE" else
                        ref.observer_seat if ref.requested_subject == "SELF" else ref.subject_seat)
        if not subject_only:
            visible_seat = "ALL" if ref.observer_kind == "ALL_HANDS" else ref.observer_seat
        for seat, details in state.get("seats", {}).items():
            if visible_seat != "ALL" and seat != visible_seat:
                details.pop("concealed_tiles", None)
                details.pop("concealed_hand", None)
        return {
            "status": "ALLOWED",
            **metadata,
            "position_input_digest": ref.projection_digest,
            "visible_seat": visible_seat,
            "state": state,
        }

    def answer(self, conversation_ref_id: str) -> dict[str, Any]:
        decision = self.authorize(conversation_ref_id)
        if decision["status"] != "ALLOWED":
            return decision
        ref, projection = self._refs[conversation_ref_id]
        metadata = ref.public_metadata()
        if ref.requested_subject == "PUBLIC_TABLE":
            public_state = deepcopy(projection["state"])
            for details in public_state.get("seats", {}).values():
                details.pop("concealed_tiles", None)
                details.pop("concealed_hand", None)
            return {"status": "ALLOWED", **metadata, "subject": "PUBLIC_TABLE",
                    "position_input_digest": ref.projection_digest,
                    "public_state": public_state, "engine_results": {},
                    "applied_concept_ids": []}
        seat = ref.observer_seat if ref.requested_subject == "SELF" else ref.subject_seat
        concealed = projection.get("state", {}).get("seats", {}).get(seat or "", {}).get("concealed_tiles")
        if not isinstance(concealed, list):
            return self._result("DENIED", "REPLAY_PERMISSION_REQUIRED", metadata)
        melds = projection["state"]["seats"][seat].get("melds", [])
        fixed_tiles = [tile for meld in melds for tile in meld.get("tiles", [])]
        analysis = conversation_shape_analysis(
            _tile_notation(concealed), rule_profile_id=ref.rule_profile_id,
            rule_snapshot_id=ref.rule_snapshot_id, mode=ref.mode,
            observer_kind=ref.observer_kind, observer_seat=ref.observer_seat,
            subject_seat=seat, input_digest=ref.projection_digest,
            fixed_meld_count=len(melds), fixed_tiles=_tile_notation(fixed_tiles) if fixed_tiles else "",
        )
        shanten = analysis.get("overall_shanten")
        waits = [item["tile"] for item in analysis.get("waits", [])]
        ukeire = [item["tile"] for item in analysis.get("ukeire", [])]
        explanation = f"{seat}家は{shanten}向聴です。"
        if waits:
            explanation += " 待ちは" + "・".join(waits) + "です。"
        elif ukeire:
            explanation += " 受入牌種は" + "・".join(ukeire) + "です。"
        return {"status": "ALLOWED", **metadata, "subject": seat,
                "position_input_digest": ref.projection_digest,
                "engine_results": {"shape": analysis},
                "explanation": explanation,
                "applied_concept_ids": list(ANALYSIS_CONCEPT_IDS)}

    @staticmethod
    def _result(status: str, reason: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"status": status, "reason_code": reason, **dict(metadata or {})}
