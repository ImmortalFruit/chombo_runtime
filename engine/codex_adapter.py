"""Local Codex composition; no provider, credentials, or outbound transport.

Only the trusted host registers references. A pasted clipboard document is
display data and cannot register a reference or supply an ApplicationContext.
"""
from pathlib import Path
import hashlib
from threading import RLock
from uuid import uuid4

from engine.application import Application, ApplicationContext
from engine.application_schema import CONTRACT_ID, CONTRACT_VERSION


class CodexAdapter:
    def __init__(self, application: Application, *, owner_id: str, conversations=None):
        self.application = application
        self.owner_id = owner_id
        self.conversations = conversations
        self._ref_ids = set()
        self._confirmations = {}
        self._lock = RLock()

    def bind_reference(self, ref):
        """Called by the host after issuing a reference for this owner."""
        with self._lock:
            self._ref_ids.add(ref.conversation_ref_id)
        return ref

    def invoke(self, operation, inputs, *, request_id=None):
        with self._lock:
            context = ApplicationContext(self.owner_id, self.conversations, frozenset(self._ref_ids))
            return self.application.invoke({
                "contract_id": CONTRACT_ID, "contract_version": CONTRACT_VERSION,
                "request_id": request_id if request_id is not None else str(uuid4()),
                "operation": operation, "input": inputs,
            }, context)

    def confirm_subject(self, ref_id, subject):
        """Resolve an ambiguous question on its original frozen projection."""
        with self._lock:
            if ref_id not in self._ref_ids or self.conversations is None:
                raise ValueError("RESOURCE_NOT_FOUND")
            previous = self._confirmations.get(ref_id)
            if previous is not None:
                if previous.requested_subject != subject:
                    raise ValueError("CONFIRMATION_CONFLICT")
                decision = self.conversations.authorize(previous.conversation_ref_id)
                if decision["status"] == "REFERENCE_INVALID":
                    raise ValueError(decision["reason_code"])
                return previous
            ref = self.conversations.resolve_subject(ref_id, subject)
            self._confirmations[ref_id] = ref
            return self.bind_reference(ref)

    def legacy_position(self, ref_id, *, snapshot=False):
        """Retain old response bodies and explicit all-hands clipboard export."""
        with self._lock:
            operation = "position.snapshot" if snapshot else "position.analyze"
            # Old clients do not send request IDs. Bind retries to the immutable
            # reference, rather than spending a request slot or calculating again.
            request_id = ("legacy." + operation + "." + hashlib.sha256(ref_id.encode()).hexdigest()
                          if type(ref_id) is str else None)
            result = self.invoke(operation, {"conversation_ref_id": ref_id}, request_id=request_id)
            if result["status"] != "SUCCEEDED":
                return {"status": "REFERENCE_INVALID", "reason_code": result["error"]["code"]}
            data = result["data"]
            if snapshot and data.get("status") == "ALLOWED":
                # This old explicit export intentionally retains ALL_HANDS.
                # Qualification, owner and the minimum projection passed the
                # shared dispatcher; the gate rechecks expiry/revocation here.
                return self.conversations.snapshot(ref_id, subject_only=False)
            return data


def analyze_hand_for_codex(notation, *, fixed_meld_count=0, fixed_tiles=""):
    app = Application(concepts_dir=Path(__file__).resolve().parents[1] / "concepts")
    result = CodexAdapter(app, owner_id="local-skill").invoke("hand.analyze", {
        "notation": notation, "fixed_meld_count": fixed_meld_count, "fixed_tiles": fixed_tiles,
    })
    if result["status"] != "SUCCEEDED":
        code = result["error"]["code"]
        raise ValueError(code + (": expected valid 13 or 14 tile input (or fixed-meld remainder)" if code == "INVALID_REQUEST" else ""))
    return result["data"]
