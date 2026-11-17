"""Deterministic startup classification for durable v0.3.1 operations (R31-02)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from engine.operation_journal import OPERATION_FORMAT_VERSION, OPERATION_SCHEMA_ID


@dataclass(frozen=True)
class RecoveryDecision:
    operation_id: str
    request_id: str | None
    session_id: str | None
    lineage_id: str | None
    phase: str | None
    disposition: str
    reason_code: str
    base_checkpoint_id: str | None
    base_event_id: str | None
    result_checkpoint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StartupRecoveryClassifier:
    """Read operation artifacts without changing journals, checkpoints, or Live state."""

    _FILENAMES = {
        "PREPARED": "000-prepared.json",
        "COMMITTED": "001-committed.json",
        "DELIVERED": "002-delivered.json",
        "ABORTED": "001-aborted.json",
        "QUARANTINED": "001-quarantined.json",
    }
    _TERMINAL = {"DELIVERED", "ABORTED", "QUARANTINED"}

    def __init__(self, store: Any):
        self.store = store
        self.root = Path(store.root) / "operations"

    def classify_all(self, owner_id: str) -> list[dict[str, Any]]:
        decisions = []
        if not self.root.exists():
            return decisions
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            decisions.append(self.classify(directory.name, owner_id).to_dict())
        return decisions

    def classify(self, operation_id: str, owner_id: str) -> RecoveryDecision:
        documents: list[dict[str, Any]] = []
        directory = self.root / operation_id
        try:
            paths = sorted(directory.glob("*.json"))
            if not paths:
                return self._decision(operation_id, None, "RECOVERY_IMPOSSIBLE", "OPERATION_PHASE_MISSING")
            for path in paths:
                document = self.store._strict_read(path)
                self._validate_document(document, operation_id, path.name, owner_id)
                documents.append(document)
            self._validate_chain(documents)
        except Exception as error:
            code = getattr(error, "code", "OPERATION_CHAIN_INVALID")
            disposition = "UNSUPPORTED_VERSION" if code == "OPERATION_VERSION_UNSUPPORTED" else "QUARANTINED"
            return self._decision(operation_id, documents[0] if documents else None, disposition, code)

        prepared = documents[0]
        latest = documents[-1]
        phase = latest["phase"]
        if phase in {"ABORTED", "QUARANTINED"}:
            disposition = "DISCARDED" if phase == "ABORTED" else "QUARANTINED"
            return self._decision(operation_id, latest, disposition, latest["payload"].get("reason_code", phase))
        if phase in {"COMMITTED", "DELIVERED"}:
            return self._classify_committed(prepared, latest, owner_id)
        return self._classify_prepared(prepared, owner_id)

    def _validate_document(self, document: Mapping[str, Any], operation_id: str,
                           filename: str, owner_id: str) -> None:
        if (document.get("schema_id"), document.get("format_version")) != (
            OPERATION_SCHEMA_ID, OPERATION_FORMAT_VERSION
        ):
            raise self.store.error("OPERATION_VERSION_UNSUPPORTED", {"operation_id": operation_id})
        phase = document.get("phase")
        if (
            document.get("document_kind") != "OPERATION_PHASE"
            or document.get("operation_id") != operation_id
            or document.get("owner_id") != owner_id
            or self._FILENAMES.get(phase) != filename
            or document.get("integrity", {}).get("document_hash") != self.store.hash_document(document)
        ):
            raise self.store.error("OPERATION_CHAIN_INVALID", {"operation_id": operation_id})

    def _validate_chain(self, documents: list[Mapping[str, Any]]) -> None:
        if documents[0].get("phase") != "PREPARED" or documents[0].get("phase_sequence") != 0:
            raise self.store.error("OPERATION_CHAIN_INVALID")
        if len(documents) > 3:
            raise self.store.error("OPERATION_CHAIN_INVALID")
        invariant = ("owner_id", "session_id", "lineage_id", "operation_id", "request_id",
                     "operation_kind", "base_checkpoint_id", "base_boundary_revision",
                     "base_event_id", "base_state_digest", "rule_profile_id", "rule_snapshot_id")
        for sequence, document in enumerate(documents):
            if document.get("phase_sequence") != sequence:
                raise self.store.error("OPERATION_CHAIN_INVALID")
            if sequence == 0:
                if document.get("previous_phase_hash") is not None:
                    raise self.store.error("OPERATION_CHAIN_INVALID")
                continue
            previous = documents[sequence - 1]
            if document.get("previous_phase_hash") != previous["integrity"]["document_hash"]:
                raise self.store.error("OPERATION_CHAIN_INVALID")
            if any(document.get(key) != documents[0].get(key) for key in invariant):
                raise self.store.error("OPERATION_CHAIN_INVALID")
            if sequence == 1 and document.get("phase") not in {"COMMITTED", "ABORTED", "QUARANTINED"}:
                raise self.store.error("OPERATION_CHAIN_INVALID")
            if sequence == 2 and not (previous.get("phase") == "COMMITTED" and document.get("phase") == "DELIVERED"):
                raise self.store.error("OPERATION_CHAIN_INVALID")
            if previous.get("phase") in self._TERMINAL:
                raise self.store.error("OPERATION_CHAIN_INVALID")

    def _classify_prepared(self, prepared: Mapping[str, Any], owner_id: str) -> RecoveryDecision:
        base_id = prepared.get("base_checkpoint_id")
        if base_id is None:
            return self._decision(prepared["operation_id"], prepared, "RECOVERY_IMPOSSIBLE",
                                  "RECOVERY_BASE_NOT_DURABLE")
        try:
            base = self.store.load(base_id, owner_id)
        except Exception as error:
            return self._decision(prepared["operation_id"], prepared, "QUARANTINED",
                                  getattr(error, "code", "OPERATION_BASE_MISMATCH"))
        if not self._base_matches(prepared, base):
            return self._decision(prepared["operation_id"], prepared, "QUARANTINED",
                                  "OPERATION_BASE_MISMATCH")
        if prepared["payload"].get("replayable") is True and self._runtime_matches(prepared):
            return self._decision(prepared["operation_id"], prepared, "SAFE_REPLAY",
                                  "PREPARED_REPLAYABLE")
        return self._decision(prepared["operation_id"], prepared, "SAFE_DISCARD",
                              "RECOVERY_REPLAY_UNSUPPORTED")

    def _classify_committed(self, prepared: Mapping[str, Any], latest: Mapping[str, Any],
                            owner_id: str) -> RecoveryDecision:
        committed = latest
        if latest["phase"] == "DELIVERED":
            committed_path = self.root / latest["operation_id"] / "001-committed.json"
            committed = self.store._strict_read(committed_path)
        checkpoint_id = committed.get("payload", {}).get("checkpoint_id")
        try:
            checkpoint = self.store.load(checkpoint_id, owner_id)
        except Exception as error:
            return self._decision(prepared["operation_id"], latest, "QUARANTINED",
                                  getattr(error, "code", "OPERATION_STATE_DIGEST_MISMATCH"), checkpoint_id)
        payload = committed["payload"]
        valid = (
            checkpoint.get("session_id") == prepared["session_id"]
            and checkpoint.get("lineage_id") == prepared["lineage_id"]
            and checkpoint.get("access_binding", {}).get("owner_id") == prepared["owner_id"]
            and checkpoint.get("rules", {}).get("profile_id") == prepared["rule_profile_id"]
            and checkpoint.get("rules", {}).get("snapshot_id") == prepared["rule_snapshot_id"]
            and checkpoint.get("position", {}).get("committed_operation_id") == prepared["operation_id"]
            and checkpoint.get("position", {}).get("event_id") == payload.get("last_event_id")
            and checkpoint.get("session", {}).get("state_digest") == payload.get("final_state_digest")
            and checkpoint.get("operation_journal", {}).get("head_phase_hash")
                == committed.get("integrity", {}).get("document_hash")
        )
        if not valid:
            return self._decision(prepared["operation_id"], latest, "QUARANTINED",
                                  "OPERATION_STATE_DIGEST_MISMATCH", checkpoint_id)
        disposition = "COMPLETED" if latest["phase"] == "DELIVERED" else "REUSE_COMMITTED_RESULT"
        reason = "DELIVERY_COMPLETE" if latest["phase"] == "DELIVERED" else "COMMITTED_RESULT_VALID"
        return self._decision(prepared["operation_id"], latest, disposition, reason, checkpoint_id)

    def _base_matches(self, prepared: Mapping[str, Any], base: Mapping[str, Any]) -> bool:
        return (
            base.get("session_id") == prepared["session_id"]
            and base.get("lineage_id") == prepared["lineage_id"]
            and base.get("rules", {}).get("profile_id") == prepared["rule_profile_id"]
            and base.get("rules", {}).get("snapshot_id") == prepared["rule_snapshot_id"]
            and base.get("position", {}).get("boundary_revision") == prepared["base_boundary_revision"]
            and base.get("position", {}).get("event_id") == prepared["base_event_id"]
            and base.get("session", {}).get("state_digest") == prepared["base_state_digest"]
        )

    def _runtime_matches(self, prepared: Mapping[str, Any]) -> bool:
        producer = prepared.get("producer", {})
        return (
            producer.get("app_id") == self.store.producer.get("app_id")
            and producer.get("engine_version") == self.store.producer.get("engine_version")
            and producer.get("build_revision") == self.store.producer.get("build_revision")
        )

    @staticmethod
    def _decision(operation_id: str, document: Mapping[str, Any] | None,
                  disposition: str, reason_code: str,
                  result_checkpoint_id: str | None = None) -> RecoveryDecision:
        document = document or {}
        return RecoveryDecision(
            operation_id=operation_id,
            request_id=document.get("request_id"),
            session_id=document.get("session_id"),
            lineage_id=document.get("lineage_id"),
            phase=document.get("phase"),
            disposition=disposition,
            reason_code=reason_code,
            base_checkpoint_id=document.get("base_checkpoint_id"),
            base_event_id=document.get("base_event_id"),
            result_checkpoint_id=result_checkpoint_id,
        )
