"""Durable immutable operation phases for v0.3.1 action commits (R31-01)."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


OPERATION_SCHEMA_ID = "https://chombo.local/schemas/chombo.operation_phase.v0.3.1.schema.json"
OPERATION_FORMAT_VERSION = "1.0.0"


class OperationJournal:
    """Write phase bodies before their derived request index.

    Recovery classification is deliberately outside this class (R31-02).  This
    writer only creates chains that a future classifier can validate without
    consulting mutable process memory.
    """

    def __init__(self, store: Any):
        self.store = store
        self.root = Path(store.root) / "operations"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def input_hash(action_id: str) -> str:
        return "sha256:" + hashlib.sha256(
            store_canonical({"action_id": action_id})
        ).hexdigest()

    def find_request(self, owner_id: str, session_id: str, request_id: str) -> dict[str, Any] | None:
        for path in self.root.glob("*/000-prepared.json"):
            try:
                document = self.store._strict_read(path)
            except Exception:
                continue
            if document.get("integrity", {}).get("document_hash") != self.store.hash_document(document):
                continue
            if (document.get("owner_id"), document.get("session_id"), document.get("request_id")) == (
                owner_id, session_id, request_id
            ):
                return document
        return None

    def latest(self, operation_id: str) -> dict[str, Any] | None:
        directory = self.root / operation_id
        paths = sorted(directory.glob("*.json"))
        if not paths:
            return None
        document = self.store._strict_read(paths[-1])
        if document.get("integrity", {}).get("document_hash") != self.store.hash_document(document):
            raise self.store.error("OPERATION_CHAIN_INVALID", {"operation_id": operation_id})
        return document

    def _base(self, live: Any, operation_id: str, request_id: str, phase: str,
              sequence: int, previous_hash: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
        hand = live.engine.current_hand if hasattr(live.engine, "current_hand") else live.engine
        return {
            "document_kind": "OPERATION_PHASE", "schema_id": OPERATION_SCHEMA_ID,
            "format_version": OPERATION_FORMAT_VERSION, "producer": deepcopy(self.store.producer),
            "created_at": self.store.utc_now(), "owner_id": live.owner_id,
            "session_id": live.session_id, "lineage_id": live.lineage_id,
            "operation_id": operation_id, "request_id": request_id, "operation_kind": "LIVE_ACTION",
            "phase": phase, "phase_sequence": sequence, "previous_phase_hash": previous_hash,
            "base_checkpoint_id": live.parent_checkpoint_id,
            "base_boundary_revision": live.boundary_revision,
            "base_event_id": hand.last_event.event_id,
            "base_state_digest": self.store.state_digest(live),
            "rule_profile_id": live.profile.profile_id, "rule_snapshot_id": live.profile.snapshot_id,
            "payload": deepcopy(dict(payload)),
            "integrity": {"algorithm": "canonical-json-sha256.v1"},
        }

    def prepare(self, live: Any, request_id: str, action_id: str, legal_action_ids: list[str]) -> dict[str, Any]:
        previous = self.find_request(live.owner_id, live.session_id, request_id)
        fingerprint = self.input_hash(action_id)
        if previous is not None:
            if previous["payload"]["input_hash"] != fingerprint:
                raise self.store.error("REQUEST_ID_CONFLICT", {"request_id": request_id})
            return previous
        self.store.ensure_operation_capacity()
        operation_id = str(uuid4())
        hand = live.engine.current_hand if hasattr(live.engine, "current_hand") else live.engine
        legal_digest = "sha256:" + hashlib.sha256(store_canonical(sorted(legal_action_ids))).hexdigest()
        document = self._base(live, operation_id, request_id, "PREPARED", 0, None, {
            "normalized_input": {"action_id": action_id}, "input_hash": fingerprint,
            "authorized_subject": live.owner_id, "legal_action_set_digest": legal_digest,
            "controller_input": {"human_seat": live.human_seat.value, "saved_selections": []},
            "random_position": {
                "rng_id": hand.rng_id, "seed": str(hand.seed),
                "decision_count": hand.decision_count,
            },
            "replayable": True,
        })
        document["integrity"]["document_hash"] = self.store.hash_document(document)
        directory = self.root / operation_id
        directory.mkdir(mode=0o700)
        self.store._write(directory / "000-prepared.json", document)
        return document

    def commit(self, prepared: Mapping[str, Any], scratch: Any, checkpoint_id: str,
               result: Mapping[str, Any], *, persist: bool = True) -> dict[str, Any]:
        operation_id = prepared["operation_id"]
        hand = scratch.engine.current_hand if hasattr(scratch.engine, "current_hand") else scratch.engine
        decision_start = prepared["payload"]["random_position"]["decision_count"]
        same_rng = prepared["payload"]["random_position"]["rng_id"] == hand.rng_id
        saved_selections = [
            deepcopy(item) for item in getattr(hand, "controller_decisions", ())
            if not same_rng or item["decision_index"] >= decision_start
        ]
        document = self._base(
            scratch, operation_id, prepared["request_id"], "COMMITTED", 1,
            prepared["integrity"]["document_hash"], {
                "checkpoint_id": checkpoint_id,
                "first_event_id": prepared["base_event_id"], "last_event_id": hand.last_event.event_id,
                "final_state_digest": self.store.state_digest(scratch),
                "random_position": {
                    "rng_id": hand.rng_id, "seed": str(hand.seed),
                    "decision_count": hand.decision_count,
                },
                "controller_progress": {
                    "decision_count_before": decision_start,
                    "decision_count_after": hand.decision_count,
                    "rng_changed": not same_rng,
                    "saved_agent_selections": saved_selections,
                    "accepted_inputs": deepcopy(scratch.input_ledger),
                },
                "result_hash": "sha256:" + hashlib.sha256(store_canonical(result)).hexdigest(),
                "result": deepcopy(dict(result)),
                "outbox_items": [{
                    "delivery_id": self.delivery_id(operation_id, "response"),
                    "kind": "RESPONSE",
                    "payload_hash": "sha256:" + hashlib.sha256(store_canonical(result)).hexdigest(),
                }],
            },
        )
        # The base fields describe the PREPARED boundary, not scratch's new one.
        for key in ("base_checkpoint_id", "base_boundary_revision", "base_event_id", "base_state_digest"):
            document[key] = prepared[key]
        document["integrity"]["document_hash"] = self.store.hash_document(document)
        if persist:
            self.persist_commit(document)
        return document

    def persist_commit(self, document: Mapping[str, Any]) -> None:
        self.store._write(
            self.root / document["operation_id"] / "001-committed.json", document
        )

    @staticmethod
    def delivery_id(operation_id: str, channel: str) -> str:
        return "delivery:" + hashlib.sha256(f"{operation_id}:{channel}".encode()).hexdigest()

    def deliver(self, committed: Mapping[str, Any]) -> dict[str, Any]:
        """Durably expose one immutable response without applying Engine state again."""
        operation_id = committed["operation_id"]
        latest = self.latest(operation_id)
        if latest is not None and latest["phase"] == "DELIVERED":
            return latest
        if latest is None or latest["phase"] != "COMMITTED":
            raise self.store.error("DELIVERY_STATE_UNKNOWN", {"operation_id": operation_id})
        committed = latest
        response = deepcopy(committed["payload"]["result"])
        deliveries = [{
            "delivery_id": item["delivery_id"], "status": "AVAILABLE",
            "payload_hash": item["payload_hash"],
        } for item in committed["payload"]["outbox_items"]]
        document = deepcopy(dict(committed))
        document.update({
            "created_at": self.store.utc_now(), "phase": "DELIVERED", "phase_sequence": 2,
            "previous_phase_hash": committed["integrity"]["document_hash"],
            "payload": {"deliveries": deliveries, "response": response,
                        "response_hash": "sha256:" + hashlib.sha256(store_canonical(response)).hexdigest()},
            "integrity": {"algorithm": "canonical-json-sha256.v1"},
        })
        document["integrity"]["document_hash"] = self.store.hash_document(document)
        self.store._write(self.root / operation_id / "002-delivered.json", document)
        return document

    def terminate_prepared(self, prepared: Mapping[str, Any], phase: str,
                           payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append an immutable non-commit terminal to a verified PREPARED phase."""
        if phase not in {"ABORTED", "QUARANTINED"} or prepared.get("phase") != "PREPARED":
            raise self.store.error("OPERATION_CHAIN_INVALID",
                                   {"operation_id": prepared.get("operation_id")})
        operation_id = prepared["operation_id"]
        latest = self.latest(operation_id)
        if latest is None or latest.get("integrity", {}).get("document_hash") != prepared.get("integrity", {}).get("document_hash"):
            raise self.store.error("OPERATION_CHAIN_INVALID", {"operation_id": operation_id})
        document = deepcopy(dict(prepared))
        document.update({
            "created_at": self.store.utc_now(), "phase": phase, "phase_sequence": 1,
            "previous_phase_hash": prepared["integrity"]["document_hash"],
            "payload": deepcopy(dict(payload)),
            "integrity": {"algorithm": "canonical-json-sha256.v1"},
        })
        document["integrity"]["document_hash"] = self.store.hash_document(document)
        filename = "001-aborted.json" if phase == "ABORTED" else "001-quarantined.json"
        self.store._write(self.root / operation_id / filename, document)
        return document


def store_canonical(value: Any) -> bytes:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
