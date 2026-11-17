"""Transport-independent, qualified application entry point (I50-01).

Only the outer composition root creates ApplicationContext. JSON bodies never
create contexts or grant access to position IDs. Existing CLI/HTTP adapters are
connected in I50-04/05; no Engine function is imported from a transport here.
"""
from __future__ import annotations

from engine.knowledge_store import concept_document

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any

from engine.application_schema import (
    CONNECTED, CONTRACT_ID, CONTRACT_VERSION, CONTRACT_VERSIONS, INPUTS, inputs_for, capabilities,
    json_tree, request_schema, validate,
)
from engine.hand_completion import analyze_hand, standard_wait_tiles_with_fixed_melds
from engine.local_calculations import CALCULATOR_VERSION
from engine.position_conversation import ANALYSIS_CONCEPT_IDS, PositionConversationGate


NOTATION_IDS = tuple("tile.notation.compact." + suffix for suffix in ("numbered", "honor", "hand", "red_five"))
CONCEPT_FILES = ("hand-completion-tenpai-and-waits.json", "hand-ukeire.json", "compact-tile-notation.json")


@dataclass(frozen=True)
class ApplicationContext:
    """Trusted owner-scoped dependencies, supplied by the composition root.

    A context owns a private gate, or explicitly receives the position IDs that
    the outer authenticated integration has bound to this owner. Merely knowing
    a reference ID in a shared gate never grants access.
    """
    owner_id: str
    conversations: PositionConversationGate | None = None
    position_ref_ids: frozenset[str] = frozenset()
    credential_binding: tuple[str, str] | None = None


class Application:
    """One shared dispatcher for Python and future transport adapters."""

    def __init__(self, *, concepts_dir: Path, max_requests_per_owner: int = 1024, services=None):
        self.concepts_dir = Path(concepts_dir)
        self.services = services
        if type(max_requests_per_owner) is not int or max_requests_per_owner < 1:
            raise ValueError("invalid request limit")
        self.max_requests_per_owner = max_requests_per_owner
        self._requests: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._lock = RLock()

    def capabilities(self):
        result = capabilities()
        if self.services:
            for item in result["operations"]:
                if item["operation"] in self.services.operations:
                    item.update(available=True, reason_code=None)
                    if item['operation'] in {'interaction.authorize_send', 'interaction.retry'}:
                        item.update(type_version='1.1.0', input_type='v1_1.' + item['operation'] + '.input',
                                    result_type='v1_1.' + item['operation'] + '.result')
        try:
            self._qualified()
        except (OSError, ValueError, KeyError, TypeError):
            for operation in result["operations"]:
                if operation["operation"] in CONNECTED:
                    operation.update(available=False, reason_code="REVIEW_REQUIRED")
        if self.services:
            result["operations"].append({"operation": "interaction.list", "input_type": "v1_1.interaction.list.input",
                "result_type": "v1_1.interaction.list.result", "type_version": "1.1.0", "available": True, "reason_code": None})
            result["dialogue"] = self.services.dialogue_capabilities()
        return result

    def _qualified(self):
        qualified = set()
        for filename in CONCEPT_FILES:
            document = concept_document(self.concepts_dir, filename)
            if document.get("status") == "QUALIFIED":
                qualified.update(item["concept_id"] for item in document["concepts"])
        if not set(ANALYSIS_CONCEPT_IDS + NOTATION_IDS) <= qualified:
            raise ValueError("REVIEW_REQUIRED")

    @staticmethod
    def _envelope(request, *, data=None, code=None, failed=False, meta=None):
        return {"contract_id": CONTRACT_ID, "contract_version": request.get("contract_version") if request.get("contract_version") in CONTRACT_VERSIONS else CONTRACT_VERSION,
                "request_id": request.get("request_id") if type(request.get("request_id")) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request["request_id"]) else None,
                "operation": request.get("operation") if type(request.get("operation")) is str and 0 < len(request["operation"]) <= 16384 else None,
                "status": ("FAILED" if failed else "REJECTED") if code else "SUCCEEDED",
                "data": None if code else data,
                "error": {"code": code, "source_code": None, "retryable": False, "details": {}} if code else None,
                "meta": meta or {"engine_version": None, "rule_ref": None,
                                 "applied_concept_ids": [], "position_ref": None}}

    def invoke_json(self, raw: bytes | str, context: ApplicationContext):
        """Strict JSON decoding for adapters; malformed transport has no fake ID."""
        def pairs(items):
            value = {}
            for key, child in items:
                if key in value:
                    raise ValueError("INVALID_REQUEST")
                value[key] = child
            return value

        try:
            if type(raw) not in (str, bytes) or len(raw) > 90 * 1024 * 1024:
                raise ValueError("INVALID_REQUEST")
            request = json.loads(raw, object_pairs_hook=pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError("INVALID_REQUEST")))
        except (ValueError, UnicodeError, RecursionError):
            return self._envelope({}, code="INVALID_REQUEST")
        return self.invoke(request, context)

    def invoke(self, request, context: ApplicationContext):
        """Validate before dispatch; cache never bypasses current authority."""
        original = request if type(request) is dict else {}
        try:
            json_tree(request)
            if type(request) is not dict:
                raise ValueError("INVALID_REQUEST")
            request = deepcopy(request)
            if request.get("contract_id") != CONTRACT_ID:
                raise ValueError("INVALID_REQUEST")
            if request.get("contract_version") not in CONTRACT_VERSIONS:
                raise ValueError("UNSUPPORTED_CONTRACT_VERSION")
            operation = request.get("operation")
            if type(operation) is not str or operation not in inputs_for(request["contract_version"]):
                raise ValueError("UNKNOWN_OPERATION")
            # Deferred input schemas may reference legacy documents. They cannot
            # execute before their adapters/validators are connected.
            if not isinstance(context, ApplicationContext) or type(context.owner_id) is not str or not context.owner_id:
                raise ValueError("PERMISSION_DENIED")
            if operation not in CONNECTED and not (self.services and operation in self.services.operations):
                shell = request_schema(operation, request["contract_version"])
                shell["properties"]["input"] = {"type": "object"}
                validate(request, shell)
                return self._envelope(request, code="CAPABILITY_UNAVAILABLE")
            if self.services and operation in self.services.operations:
                self.services.validate_request(request)
            else:
                validate(request, request_schema(operation, request["contract_version"]))
        except (ValueError, UnicodeError, RecursionError, TypeError) as error:
            code = str(error)
            return self._envelope(original, code=code if code in {
                "INVALID_REQUEST", "RESOURCE_LIMIT", "UNSUPPORTED_CONTRACT_VERSION", "UNKNOWN_OPERATION", "PERMISSION_DENIED"
            } else "INVALID_REQUEST")

        if self.services:
            code = self.services.bind_request(request, context)
            if code:
                return self._envelope(request, code=code)
        if self.services and operation in self.services.operations:
            return self.services.invoke(self, request, context)
        with self._lock:
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            key = (context.owner_id, request["request_id"])
            previous = self._requests.get(key)
            if previous and previous[0] != digest:
                return self._envelope(request, code="REQUEST_ID_CONFLICT")
            if not previous and sum(owner == context.owner_id for owner, _ in self._requests) >= self.max_requests_per_owner:
                return self._envelope(request, code="RESOURCE_LIMIT")
            try:
                if operation.startswith("position."):
                    ref_id = request["input"]["conversation_ref_id"]
                    if context.conversations is None or ref_id not in context.position_ref_ids:
                        return self._envelope(request, code="RESOURCE_NOT_FOUND")
                    decision = context.conversations.authorize(ref_id)
                    if decision["status"] != "ALLOWED":
                        result = self._envelope(request, data=decision)
                        self._requests[key] = (digest, result)
                        return deepcopy(result)
                self._qualified()
            except (OSError, ValueError, KeyError, TypeError):
                return self._envelope(request, code="REVIEW_REQUIRED")
            if previous:
                return deepcopy(previous[1])
            try:
                if operation == "hand.analyze":
                    data = self._hand(request["input"])
                    meta = {"engine_version": CALCULATOR_VERSION, "rule_ref": None,
                            "applied_concept_ids": list(ANALYSIS_CONCEPT_IDS + NOTATION_IDS), "position_ref": None}
                else:
                    gate = context.conversations
                    data = gate.snapshot(ref_id, subject_only=True) if operation == "position.snapshot" else gate.answer(ref_id)
                    # Recheck after calculation and before disclosing any result.
                    decision = gate.authorize(ref_id)
                    if decision["status"] != "ALLOWED":
                        data = decision
                    try:
                        self._qualified()
                    except (OSError, ValueError, KeyError, TypeError):
                        return self._envelope(request, code="REVIEW_REQUIRED")
                    public = gate.reference_metadata(ref_id) if decision["status"] == "ALLOWED" else None
                    if decision["status"] == "ALLOWED" and public is None:
                        return self._envelope(request, data=gate.authorize(ref_id))
                    if public is not None:
                        try:
                            self._validate_position_result(data, public, operation)
                        except ValueError:
                            return self._envelope(request, code="INTERNAL_ERROR", failed=True)
                    meta = {"engine_version": CALCULATOR_VERSION if data.get("engine_results") else None,
                            "rule_ref": {"kind": "MATCH", "profile_id": public["rule_profile_id"], "snapshot_id": public["rule_snapshot_id"]} if public else None,
                            "applied_concept_ids": data.get("applied_concept_ids", []), "position_ref": public}
                result = self._envelope(request, data=data, meta=meta)
                # No private mutable Engine objects may escape the wire boundary.
                try:
                    json_tree(result)
                    from engine.application_schema import result_schemas
                    schemas = result_schemas()
                    validate(result, schemas["Response"], schemas)
                except (ValueError, KeyError, TypeError):
                    result = self._envelope(request, code="INTERNAL_ERROR", failed=True)
            except ValueError:
                result = self._envelope(request, code="INVALID_REQUEST")
            except Exception:
                # Exception text and local filesystem paths are never wire data.
                result = self._envelope(request, code="INTERNAL_ERROR", failed=True)
            self._requests[key] = (digest, result)
            return deepcopy(result)

    @staticmethod
    def _validate_position_result(data, public, operation):
        """Bind disclosure to the authorized reference and its single subject."""
        if any(data.get(key) != value for key, value in public.items()
               if key not in {"projection_digest", "expires_at"}):
            raise ValueError("OUTPUT_INVALID")
        if data.get("status") != "ALLOWED":
            return
        subject = (None if public["requested_subject"] == "PUBLIC_TABLE" else
                   public["observer_seat"] if public["requested_subject"] == "SELF" else public["subject_seat"])
        if operation == "position.snapshot":
            if data.get("visible_seat") != subject:
                raise ValueError("OUTPUT_INVALID")
            state = data.get("state", {})
        else:
            if data.get("subject") != (subject or "PUBLIC_TABLE"):
                raise ValueError("OUTPUT_INVALID")
            state = data.get("public_state", {})
        for seat, details in state.get("seats", {}).items():
            if seat != subject and {"concealed_tiles", "concealed_hand"} & details.keys():
                raise ValueError("OUTPUT_INVALID")
        if data.get("position_input_digest") != public["projection_digest"]:
            raise ValueError("OUTPUT_INVALID")

    @staticmethod
    def _hand(inputs):
        count = inputs.get("fixed_meld_count", 0)
        tiles = inputs.get("fixed_tiles", "")
        if count:
            if not tiles:
                raise ValueError("INVALID_REQUEST")
            waits = standard_wait_tiles_with_fixed_melds(inputs["notation"], count, tiles)
            return {"input": inputs["notation"], "fixed_meld_count": count, "fixed_tiles": tiles,
                    "scope": "shape_only_standard_waits_with_fixed_melds", "waits": list(waits),
                    "wait_tile_type_count": len(waits),
                    "excluded_judgments": ["action_legality", "yaku", "score", "furiten", "discard_choice"]}
        if tiles:
            raise ValueError("INVALID_REQUEST")
        return analyze_hand(inputs["notation"])
