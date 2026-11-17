"""Strict v0.4 condition-document loading and pre-search validation.

This module deliberately stops before candidate generation.  It parses each
untrusted byte sequence once, dispatches exact schema versions, verifies the
closed document shape and canonical digest, and then validates the semantic
relationships that JSON Schema cannot express.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID


FORMAT_VERSION = "1.0.0"
INTEGRITY_ALGORITHM = "canonical-json-sha256.v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 48
MAX_OBJECT_FIELDS = 256
MAX_ARRAY_ITEMS = 4096
MAX_STRING = 16_384
MAX_ID = 256
MAX_CONCEPT_REFS = 512
MAX_SEARCH_DEPTH = 64
MAX_TRANSITIONS = 4096
MAX_CANDIDATES = 10_000
MAX_WALL_TIME_MS = 60_000

SCHEMA_IDS = {
    "CONDITION_REQUEST": "https://chombo.local/schemas/chombo.condition_request.v0.4.schema.json",
    "CONDITION_RESPONSE": "https://chombo.local/schemas/chombo.condition_response.v0.4.schema.json",
    "CONDITION_STATE": "https://chombo.local/schemas/chombo.condition_state.v0.4.schema.json",
    "AGGREGATE_RANK_PROFILE": "https://chombo.local/schemas/chombo.aggregate_rank_profile.v0.4.schema.json",
    "CONDITION_SUPPORT_REF": "https://chombo.local/schemas/chombo.condition_support_ref.v0.4.schema.json",
    "CONDITION_REPLAY_GRANT_REF": "https://chombo.local/schemas/chombo.condition_replay_grant_ref.v0.4.schema.json",
}

LOADER_ERRORS = frozenset({
    "MALFORMED_DOCUMENT", "RESOURCE_LIMIT", "UNSUPPORTED_SCHEMA",
    "SCHEMA_VALIDATION_FAILED", "INTEGRITY_MISMATCH",
})
CALCULATION_STOPS = frozenset({
    "INPUT_INCOMPLETE", "INVALID_REQUEST", "INVALID_STATE", "SNAPSHOT_MISMATCH",
    "RULES_INCOMPLETE", "KNOWLEDGE_UNQUALIFIED", "LIMIT_INVALID",
})

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BUILD = re.compile(r"[0-9a-f]{40}\Z")
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")


class ConditionDocumentError(ValueError):
    """A safe structured rejection from the loader or semantic gate."""

    def __init__(self, code: str, document_kind: str | None, pointer: str, reason: str):
        self.code = code
        self.document_kind = document_kind
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"{code} at {pointer}: {reason}")

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "document_kind": self.document_kind,
            "pointer": self.pointer,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConditionBundle:
    request: Mapping[str, Any]
    state: Mapping[str, Any]
    aggregate_profile: Mapping[str, Any] | None
    support_ref: Mapping[str, Any] | None


def _fail(code: str, kind: str | None, pointer: str, reason: str) -> None:
    raise ConditionDocumentError(code, kind, pointer, reason)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ConditionDocumentError("MALFORMED_DOCUMENT", None, "", "NON_JSON_VALUE") from error


def document_hash(document: Mapping[str, Any]) -> str:
    body = deepcopy(dict(document))
    integrity = body.get("integrity")
    if isinstance(integrity, Mapping):
        body["integrity"] = dict(integrity) | {"document_hash": None}
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def _tree(value: Any, kind: str | None, pointer: str = "", depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        _fail("RESOURCE_LIMIT", kind, pointer, "JSON_DEPTH")
    if type(value) is dict:
        if len(value) > MAX_OBJECT_FIELDS:
            _fail("RESOURCE_LIMIT", kind, pointer, "OBJECT_FIELDS")
        for key, child in value.items():
            if type(key) is not str:
                _fail("MALFORMED_DOCUMENT", kind, pointer, "NON_STRING_KEY")
            _tree(child, kind, f"{pointer}/{key}", depth + 1)
    elif type(value) is list:
        if len(value) > MAX_ARRAY_ITEMS:
            _fail("RESOURCE_LIMIT", kind, pointer, "ARRAY_ITEMS")
        for index, child in enumerate(value):
            _tree(child, kind, f"{pointer}/{index}", depth + 1)
    elif type(value) is str:
        if len(value) > MAX_STRING:
            _fail("RESOURCE_LIMIT", kind, pointer, "STRING_LENGTH")
    elif type(value) is float:
        if not math.isfinite(value) or (value == 0.0 and math.copysign(1.0, value) < 0):
            _fail("MALFORMED_DOCUMENT", kind, pointer, "NON_CANONICAL_NUMBER")
    elif type(value) not in (int, bool, type(None)):
        _fail("MALFORMED_DOCUMENT", kind, pointer, "NON_JSON_VALUE")


def _strict_parse(raw: bytes | str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, str)):
        _fail("MALFORMED_DOCUMENT", None, "", "EXPECTED_JSON_TEXT")
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeError as error:
        raise ConditionDocumentError("MALFORMED_DOCUMENT", None, "", "INVALID_UTF8") from error
    if len(encoded) > MAX_BYTES:
        _fail("RESOURCE_LIMIT", None, "", "DOCUMENT_BYTES")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("MALFORMED_DOCUMENT", None, "", "DUPLICATE_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail(
                               "MALFORMED_DOCUMENT", None, "", "NON_FINITE_NUMBER"))
    except ConditionDocumentError:
        raise
    except UnicodeError as error:
        raise ConditionDocumentError("MALFORMED_DOCUMENT", None, "", "INVALID_UTF8") from error
    except (json.JSONDecodeError, RecursionError) as error:
        code = "RESOURCE_LIMIT" if isinstance(error, RecursionError) else "MALFORMED_DOCUMENT"
        raise ConditionDocumentError(code, None, "", type(error).__name__) from error
    if type(value) is not dict:
        _fail("MALFORMED_DOCUMENT", None, "", "ROOT_NOT_OBJECT")
    _tree(value, value.get("document_kind"))
    return value


def _fields(value: Any, required: set[str], optional: set[str], kind: str, pointer: str) -> None:
    if type(value) is not dict:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "EXPECTED_OBJECT")
    actual = set(value)
    if not required <= actual or not actual <= required | optional:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "CLOSED_FIELD_SET")


def _text(value: Any, kind: str, pointer: str, *, identifier: bool = False) -> None:
    limit = MAX_ID if identifier else MAX_STRING
    if type(value) is not str or not value or len(value) > limit:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "INVALID_STRING")
    if identifier and _ID.fullmatch(value) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "INVALID_ID")


def _uuid(value: Any, kind: str, pointer: str) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "INVALID_UUID")


def _timestamp(value: Any, kind: str, pointer: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
    except (TypeError, AttributeError, ValueError):
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "INVALID_UTC_TIMESTAMP")


def _enum(value: Any, allowed: Iterable[str], kind: str, pointer: str) -> None:
    if value not in set(allowed):
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer, "UNKNOWN_ENUM")


def _ref(value: Any, kind: str, pointer: str, *, snapshot: bool = True) -> None:
    fields = {"profile_id", "version", "content_hash"} | ({"snapshot_id"} if snapshot else set())
    _fields(value, fields, set(), kind, pointer)
    _text(value["profile_id"], kind, pointer + "/profile_id", identifier=True)
    if _SEMVER.fullmatch(value["version"]) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer + "/version", "INVALID_SEMVER")
    if _HASH.fullmatch(value["content_hash"]) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, pointer + "/content_hash", "INVALID_HASH")
    if snapshot:
        _text(value["snapshot_id"], kind, pointer + "/snapshot_id", identifier=True)


def _envelope(document: dict[str, Any]) -> str:
    kind = document.get("document_kind")
    schema_id = document.get("schema_id")
    version = document.get("format_version")
    if kind not in SCHEMA_IDS or schema_id != SCHEMA_IDS.get(kind) or version != FORMAT_VERSION:
        _fail("UNSUPPORTED_SCHEMA", kind if isinstance(kind, str) else None, "", "UNKNOWN_ID_OR_VERSION")
    producer = document.get("producer")
    integrity = document.get("integrity")
    _fields(producer, {"app_id", "app_version", "build_revision", "engine_version"}, set(), kind, "/producer")
    for field in ("app_id", "app_version", "engine_version"):
        _text(producer[field], kind, f"/producer/{field}")
    if _BUILD.fullmatch(producer["build_revision"]) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/producer/build_revision", "INVALID_REVISION")
    _fields(integrity, {"algorithm", "document_hash"}, set(), kind, "/integrity")
    if integrity["algorithm"] != INTEGRITY_ALGORITHM or _HASH.fullmatch(str(integrity["document_hash"])) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/integrity", "INVALID_INTEGRITY")
    _timestamp(document.get("created_at"), kind, "/created_at")
    return kind


def _validate_state(document: dict[str, Any], kind: str) -> None:
    _fields(document, _COMMON | {"state_id", "position", "participants", "scores", "round_state"}, set(), kind, "")
    _text(document["state_id"], kind, "/state_id", identifier=True)
    _fields(document["position"], {"match_id", "hand_id", "event_id", "event_sequence", "state_digest"}, set(), kind, "/position")
    for field in ("match_id", "hand_id", "event_id"):
        _text(document["position"][field], kind, f"/position/{field}", identifier=True)
    if type(document["position"]["event_sequence"]) is not int or document["position"]["event_sequence"] < 0:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/position/event_sequence", "INVALID_INTEGER")
    if _HASH.fullmatch(str(document["position"]["state_digest"])) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/position/state_digest", "INVALID_HASH")
    if type(document["participants"]) is not list or len(document["participants"]) != 4:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/participants", "REQUIRES_FOUR")
    participant_ids = []
    seats = []
    for index, item in enumerate(document["participants"]):
        _fields(item, {"participant_id", "seat"}, set(), kind, f"/participants/{index}")
        _text(item["participant_id"], kind, f"/participants/{index}/participant_id", identifier=True)
        _enum(item["seat"], ("EAST", "SOUTH", "WEST", "NORTH"), kind, f"/participants/{index}/seat")
        participant_ids.append(item["participant_id"]); seats.append(item["seat"])
    if len(set(participant_ids)) != 4 or len(set(seats)) != 4:
        _fail("INVALID_STATE", kind, "/participants", "DUPLICATE_PARTICIPANT_OR_SEAT")
    if type(document["scores"]) is not list or len(document["scores"]) != 4:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/scores", "REQUIRES_FOUR")
    score_ids = []
    for index, item in enumerate(document["scores"]):
        _fields(item, {"participant_id", "value"}, set(), kind, f"/scores/{index}")
        if type(item["value"]) is not int:
            _fail("SCHEMA_VALIDATION_FAILED", kind, f"/scores/{index}/value", "EXPECTED_INTEGER")
        score_ids.append(item["participant_id"])
    if set(score_ids) != set(participant_ids) or len(set(score_ids)) != 4:
        _fail("INVALID_STATE", kind, "/scores", "PARTICIPANT_SET_MISMATCH")
    round_state = document["round_state"]
    _fields(round_state, {"round_wind", "hand_number", "dealer_participant_id", "honba", "riichi_deposits", "match_completion_state", "initial_score_total"}, set(), kind, "/round_state")
    _enum(round_state["round_wind"], ("EAST", "SOUTH", "WEST", "NORTH"), kind, "/round_state/round_wind")
    for field in ("hand_number", "honba", "riichi_deposits", "initial_score_total"):
        if type(round_state[field]) is not int or round_state[field] < (1 if field == "hand_number" else 0):
            _fail("SCHEMA_VALIDATION_FAILED", kind, f"/round_state/{field}", "INVALID_INTEGER")
    _enum(round_state["match_completion_state"], ("IN_PROGRESS", "COMPLETED"), kind, "/round_state/match_completion_state")
    if round_state["dealer_participant_id"] not in participant_ids:
        _fail("INVALID_STATE", kind, "/round_state/dealer_participant_id", "UNKNOWN_PARTICIPANT")
    if sum(item["value"] for item in document["scores"]) + round_state["riichi_deposits"] * 1000 != round_state["initial_score_total"]:
        _fail("INVALID_STATE", kind, "/scores", "SCORE_CONSERVATION_FAILED")


def _validate_request(document: dict[str, Any], kind: str) -> None:
    required = _COMMON | {"request_id", "calculation_basis", "ranking_scope", "target", "position_source", "match_rule_ref", "concept_refs", "provenance", "search_scope"}
    _fields(document, required, {"input_constraints", "aggregate_context"}, kind, "")
    _uuid(document["request_id"], kind, "/request_id")
    _enum(document["calculation_basis"], ("CURRENT_HAND_COMEBACK", "INPUT_CONSTRAINED_COMEBACK"), kind, "/calculation_basis")
    _enum(document["ranking_scope"], ("SINGLE_MATCH_RANK", "AGGREGATE_RANK"), kind, "/ranking_scope")
    _fields(document["target"], {"participant_id", "target_rank", "comeback_tie_policy"}, set(), kind, "/target")
    if type(document["target"]["target_rank"]) is not int or not 1 <= document["target"]["target_rank"] <= 4:
        _fail("INVALID_REQUEST", kind, "/target/target_rank", "OUT_OF_RANGE")
    _enum(document["target"]["comeback_tie_policy"], ("REQUIRE_STRICT_LEAD", "CHASER_ADVANTAGE"), kind, "/target/comeback_tie_policy")
    position = document["position_source"]
    if document["calculation_basis"] == "CURRENT_HAND_COMEBACK":
        _fields(position, {"kind", "condition_ref_id", "condition_ref_digest", "evaluation_point"}, set(), kind, "/position_source")
        if position["kind"] != "SUPPORT_REF" or position["evaluation_point"] != "HAND_END" or "input_constraints" in document:
            _fail("INVALID_REQUEST", kind, "/position_source", "CURRENT_HAND_REQUIRES_SUPPORT_REF")
    else:
        _fields(position, {"kind", "state"}, set(), kind, "/position_source")
        if position["kind"] != "INLINE_STATE" or "input_constraints" not in document:
            _fail("INVALID_REQUEST", kind, "/position_source", "INPUT_MODE_REQUIRES_INLINE_STATE")
        _validate_state(position["state"], "CONDITION_STATE")
        constraints = document["input_constraints"]
        _fields(constraints, {"evaluation_point", "allowed_result_kinds"}, {"result_participants", "score_bounds", "rank_gap_bounds", "hand_horizon", "force_match_end"}, kind, "/input_constraints")
        _enum(constraints["evaluation_point"], ("HAND_END", "MATCH_END"), kind, "/input_constraints/evaluation_point")
        if "force_match_end" in constraints and type(constraints["force_match_end"]) is not bool:
            _fail("INVALID_REQUEST", kind, "/input_constraints/force_match_end", "BOOLEAN_REQUIRED")
        if constraints.get("force_match_end") and (constraints["evaluation_point"] != "MATCH_END" or "game.condition.explicit_final_hand_cutoff" not in document["concept_refs"]):
            _fail("INVALID_REQUEST", kind, "/input_constraints/force_match_end", "QUALIFIED_CUTOFF_CONTRACT_REQUIRED")
    _ref(document["match_rule_ref"], kind, "/match_rule_ref")
    refs = document["concept_refs"]
    if type(refs) is not list or not refs or len(refs) > MAX_CONCEPT_REFS or len(set(refs)) != len(refs):
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/concept_refs", "INVALID_CONCEPT_REFS")
    scope = document["search_scope"]
    _fields(scope, {"allowed_result_kinds", "max_depth", "max_candidates", "max_transitions", "max_wall_time_ms"}, set(), kind, "/search_scope")
    allowed = scope["allowed_result_kinds"]
    valid_results = {"TARGET_RON", "TARGET_DEALT_IN", "TARGET_TSUMO", "OTHER_WIN", "DRAW"}
    if type(allowed) is not list or not allowed or len(set(allowed)) != len(allowed) or not set(allowed) <= valid_results:
        _fail("INVALID_REQUEST", kind, "/search_scope/allowed_result_kinds", "INVALID_RESULT_KINDS")
    limits = (("max_depth", MAX_SEARCH_DEPTH), ("max_candidates", MAX_CANDIDATES), ("max_transitions", MAX_TRANSITIONS), ("max_wall_time_ms", MAX_WALL_TIME_MS))
    for field, maximum in limits:
        if type(scope[field]) is not int or not 1 <= scope[field] <= maximum:
            _fail("LIMIT_INVALID", kind, f"/search_scope/{field}", "OUT_OF_RANGE")
    if document["ranking_scope"] == "AGGREGATE_RANK":
        if "aggregate_context" not in document:
            _fail("INPUT_INCOMPLETE", kind, "/aggregate_context", "REQUIRED")
        context = document["aggregate_context"]
        _fields(context, {"values", "profile_ref"}, set(), kind, "/aggregate_context")
        if type(context["values"]) is not list or len(context["values"]) != 4:
            _fail("INVALID_STATE", kind, "/aggregate_context/values", "REQUIRES_FOUR")
        for index, item in enumerate(context["values"]):
            _fields(item, {"participant_id", "value"}, set(), kind, f"/aggregate_context/values/{index}")
            if type(item["value"]) is not int:
                _fail("SCHEMA_VALIDATION_FAILED", kind, f"/aggregate_context/values/{index}/value", "EXPECTED_INTEGER")
        _ref(context["profile_ref"], kind, "/aggregate_context/profile_ref", snapshot=False)
    elif "aggregate_context" in document:
        _fail("INVALID_REQUEST", kind, "/aggregate_context", "FORBIDDEN_FOR_SINGLE_MATCH")


def _validate_aggregate_profile(document: dict[str, Any], kind: str) -> None:
    required = _COMMON | {"profile_id", "profile_version", "content_hash", "source_snapshot", "participant_count", "value_representation", "comparison_direction", "tie_policy", "priority_criteria", "rounding", "addition_unit", "penalty_boundary", "concept_refs", "source_refs"}
    _fields(document, required, set(), kind, "")
    _text(document["profile_id"], kind, "/profile_id", identifier=True)
    if _SEMVER.fullmatch(str(document["profile_version"])) is None or _HASH.fullmatch(str(document["content_hash"])) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/profile_version", "INVALID_PROFILE_IDENTITY")
    if document["participant_count"] != 4:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/participant_count", "REQUIRES_FOUR")
    source_snapshot = document["source_snapshot"]
    _fields(source_snapshot, {"snapshot_id", "snapshot_hash"}, set(), kind, "/source_snapshot")
    _text(source_snapshot["snapshot_id"], kind, "/source_snapshot/snapshot_id", identifier=True)
    if _HASH.fullmatch(str(source_snapshot["snapshot_hash"])) is None:
        _fail("SCHEMA_VALIDATION_FAILED", kind, "/source_snapshot/snapshot_hash", "INVALID_HASH")
    representation = document["value_representation"]
    _fields(representation, {"kind"}, {"scale"}, kind, "/value_representation")
    _enum(representation["kind"], ("INTEGER", "FIXED_DECIMAL"), kind, "/value_representation/kind")
    if representation["kind"] == "INTEGER" and "scale" in representation:
        _fail("RULES_INCOMPLETE", kind, "/value_representation/scale", "INTEGER_FORBIDS_SCALE")
    if representation["kind"] == "FIXED_DECIMAL" and (type(representation.get("scale")) is not int or representation["scale"] < 0):
        _fail("RULES_INCOMPLETE", kind, "/value_representation/scale", "FIXED_DECIMAL_REQUIRES_SCALE")
    _enum(document["comparison_direction"], ("HIGHER_IS_BETTER", "LOWER_IS_BETTER"), kind, "/comparison_direction")
    _enum(document["addition_unit"], ("PARTICIPANT_FINAL_POINTS_PER_COMPLETED_SINGLE_MATCH",), kind, "/addition_unit")
    tie = document["tie_policy"]
    _fields(tie, {"mode"}, {"rank_numbering"}, kind, "/tie_policy")
    _enum(tie["mode"], ("SHARED_RANK", "ORDERED_PRIORITY"), kind, "/tie_policy/mode")
    criteria = document["priority_criteria"]
    if tie["mode"] == "SHARED_RANK":
        if tie.get("rank_numbering") not in ("COMPETITION", "DENSE") or criteria != []:
            _fail("RULES_INCOMPLETE", kind, "/tie_policy", "INVALID_SHARED_RANK_COMBINATION")
    elif "rank_numbering" in tie or type(criteria) is not list or not criteria or len(set(criteria)) != len(criteria):
        _fail("RULES_INCOMPLETE", kind, "/priority_criteria", "INVALID_ORDERED_PRIORITY_COMBINATION")
    rounding = document["rounding"]
    _fields(rounding, {"stage", "mode", "input_scale", "output_scale"}, set(), kind, "/rounding")
    if rounding["stage"] == "NONE":
        if rounding["mode"] != "NONE" or rounding["input_scale"] != rounding["output_scale"]:
            _fail("RULES_INCOMPLETE", kind, "/rounding", "INVALID_NONE_ROUNDING")
    elif rounding["stage"] not in ("EACH_ADDITION", "AFTER_AGGREGATION", "DISPLAY_ONLY") or rounding["mode"] not in ("HALF_UP", "HALF_EVEN", "FLOOR", "CEILING", "TRUNCATE_TOWARD_ZERO"):
        _fail("RULES_INCOMPLETE", kind, "/rounding", "INVALID_ROUNDING")
    penalty = document["penalty_boundary"]
    if type(penalty) is not dict or penalty.get("mode") not in ("EXCLUDED", "EXPLICIT_ADJUSTMENT_LEDGER"):
        _fail("RULES_INCOMPLETE", kind, "/penalty_boundary", "INVALID_PENALTY_BOUNDARY")
    if penalty["mode"] == "EXCLUDED" and set(penalty) != {"mode"}:
        _fail("RULES_INCOMPLETE", kind, "/penalty_boundary", "EXCLUDED_FORBIDS_LEDGER")
    if penalty["mode"] == "EXPLICIT_ADJUSTMENT_LEDGER":
        _fields(penalty, {"mode", "ledger_id", "ledger_version", "ledger_hash", "participant_adjustments", "application_stage", "source_refs"}, set(), kind, "/penalty_boundary")
        if _SEMVER.fullmatch(str(penalty["ledger_version"])) is None or _HASH.fullmatch(str(penalty["ledger_hash"])) is None:
            _fail("RULES_INCOMPLETE", kind, "/penalty_boundary", "INVALID_LEDGER_IDENTITY")
        _enum(penalty["application_stage"], ("BEFORE_MATCH_ADDITION", "AFTER_MATCH_ADDITION_BEFORE_RANKING"), kind, "/penalty_boundary/application_stage")
        if type(penalty["participant_adjustments"]) is not list or len(penalty["participant_adjustments"]) != 4:
            _fail("RULES_INCOMPLETE", kind, "/penalty_boundary/participant_adjustments", "REQUIRES_FOUR")
    refs = document["concept_refs"]
    if type(refs) is not list or not refs or len(refs) > MAX_CONCEPT_REFS or len(set(refs)) != len(refs):
        _fail("RULES_INCOMPLETE", kind, "/concept_refs", "INVALID_CONCEPT_REFS")
    if type(document["source_refs"]) is not list or not document["source_refs"] or len(document["source_refs"]) > 128:
        _fail("RULES_INCOMPLETE", kind, "/source_refs", "PRIMARY_SOURCE_REQUIRED")


def _validate_reference(document: dict[str, Any], kind: str) -> None:
    if kind == "CONDITION_SUPPORT_REF":
        required = _COMMON | {"condition_ref_id", "source", "authority", "state_ref", "rules_ref", "request_digest", "grant_ref"}
        _fields(document, required, set(), kind, "")
        _text(document["condition_ref_id"], kind, "/condition_ref_id", identifier=True)
        _fields(document["state_ref"], {"state_id", "state_digest"}, set(), kind, "/state_ref")
        if _HASH.fullmatch(str(document["state_ref"]["state_digest"])) is None or _HASH.fullmatch(str(document["request_digest"])) is None:
            _fail("SCHEMA_VALIDATION_FAILED", kind, "/state_ref", "INVALID_HASH")
    else:
        required = _COMMON | {"grant_id", "user_id", "record_hash", "scope", "allowed_participant_ids", "expires_at", "cancellation_revision"}
        _fields(document, required, set(), kind, "")
        _timestamp(document["expires_at"], kind, "/expires_at")
        if type(document["allowed_participant_ids"]) is not list or not 1 <= len(document["allowed_participant_ids"]) <= 4 or len(set(document["allowed_participant_ids"])) != len(document["allowed_participant_ids"]):
            _fail("SCHEMA_VALIDATION_FAILED", kind, "/allowed_participant_ids", "INVALID_PARTICIPANTS")
        if type(document["cancellation_revision"]) is not int or document["cancellation_revision"] < 0:
            _fail("SCHEMA_VALIDATION_FAILED", kind, "/cancellation_revision", "INVALID_INTEGER")


def _validate_response(document: dict[str, Any], kind: str) -> None:
    required = _COMMON | {"request_id", "status", "calculation_basis", "ranking_scope", "evaluation_point", "calculator_version", "input_digest", "match_rule_ref", "aggregate_profile_ref", "concept_refs", "evidence", "usage"}
    optional = {"candidates", "partial_candidates", "complete_search_evidence", "limit_reason"}
    _fields(document, required, optional, kind, "")
    statuses = {"ACHIEVABLE", "UNREACHABLE", "INDETERMINATE_LIMIT"} | CALCULATION_STOPS
    _enum(document["status"], statuses, kind, "/status")
    if document["status"] == "ACHIEVABLE":
        if type(document.get("candidates")) is not list or not document["candidates"]:
            _fail("SCHEMA_VALIDATION_FAILED", kind, "/candidates", "REQUIRED_NON_EMPTY")
    elif document["status"] == "UNREACHABLE":
        if document.get("candidates") != [] or "complete_search_evidence" not in document:
            _fail("SCHEMA_VALIDATION_FAILED", kind, "", "UNREACHABLE_SHAPE")
    elif document["status"] == "INDETERMINATE_LIMIT":
        if "partial_candidates" not in document or "limit_reason" not in document:
            _fail("SCHEMA_VALIDATION_FAILED", kind, "", "LIMIT_SHAPE")
    elif optional & set(document):
        _fail("SCHEMA_VALIDATION_FAILED", kind, "", "STOP_STATUS_FORBIDS_CANDIDATES")


_COMMON = {"document_kind", "schema_id", "format_version", "created_at", "producer", "integrity"}
_VALIDATORS = {
    "CONDITION_REQUEST": _validate_request,
    "CONDITION_RESPONSE": _validate_response,
    "CONDITION_STATE": _validate_state,
    "AGGREGATE_RANK_PROFILE": _validate_aggregate_profile,
    "CONDITION_SUPPORT_REF": _validate_reference,
    "CONDITION_REPLAY_GRANT_REF": _validate_reference,
}


def load_condition_document(raw: bytes | str) -> Mapping[str, Any]:
    """Load one exact v0.4 document without repairing or migrating it."""
    document = _strict_parse(raw)
    kind = _envelope(document)
    if document_hash(document) != document["integrity"]["document_hash"]:
        _fail("INTEGRITY_MISMATCH", kind, "/integrity/document_hash", "DIGEST_MISMATCH")
    _VALIDATORS[kind](document, kind)
    return _freeze(document)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def validate_condition_bundle(
    request: Mapping[str, Any], *, state: Mapping[str, Any] | None = None,
    support_ref: Mapping[str, Any] | None = None,
    aggregate_profile: Mapping[str, Any] | None = None,
    qualified_concept_ids: Iterable[str] = (),
) -> ConditionBundle:
    """Validate cross-document identity, snapshots, participant sets, and knowledge."""
    kind = "CONDITION_REQUEST"
    if request["position_source"]["kind"] == "INLINE_STATE":
        resolved_state = request["position_source"]["state"]
        if state is not None or support_ref is not None:
            _fail("INVALID_REQUEST", kind, "/position_source", "AMBIGUOUS_STATE_SOURCE")
    else:
        if state is None or support_ref is None:
            _fail("INPUT_INCOMPLETE", kind, "/position_source", "UNRESOLVED_SUPPORT_REF")
        resolved_state = state
        position = request["position_source"]
        if position["condition_ref_id"] != support_ref["condition_ref_id"] or position["condition_ref_digest"] != document_hash(support_ref):
            _fail("SNAPSHOT_MISMATCH", kind, "/position_source", "SUPPORT_REF_MISMATCH")
        if support_ref["state_ref"]["state_id"] != state["state_id"] or support_ref["state_ref"]["state_digest"] != state["position"]["state_digest"]:
            _fail("SNAPSHOT_MISMATCH", kind, "/position_source", "STATE_REF_MISMATCH")
    participant_ids = {item["participant_id"] for item in resolved_state["participants"]}
    if request["target"]["participant_id"] not in participant_ids:
        _fail("INVALID_REQUEST", kind, "/target/participant_id", "UNKNOWN_PARTICIPANT")
    missing = sorted(set(request["concept_refs"]) - set(qualified_concept_ids))
    if missing:
        _fail("KNOWLEDGE_UNQUALIFIED", kind, "/concept_refs", "UNQUALIFIED_CONCEPT")
    if request["ranking_scope"] == "AGGREGATE_RANK":
        if aggregate_profile is None:
            _fail("RULES_INCOMPLETE", kind, "/aggregate_context/profile_ref", "PROFILE_NOT_RESOLVED")
        context = request["aggregate_context"]
        values = context["values"]
        value_ids = [item["participant_id"] for item in values]
        if set(value_ids) != participant_ids or len(set(value_ids)) != 4:
            _fail("INVALID_STATE", kind, "/aggregate_context/values", "PARTICIPANT_SET_MISMATCH")
        ref = context["profile_ref"]
        if (ref["profile_id"], ref["version"], ref["content_hash"]) != (
            aggregate_profile["profile_id"], aggregate_profile["profile_version"], aggregate_profile["content_hash"]):
            _fail("SNAPSHOT_MISMATCH", kind, "/aggregate_context/profile_ref", "PROFILE_REF_MISMATCH")
    elif aggregate_profile is not None:
        _fail("INVALID_REQUEST", kind, "/aggregate_context", "UNEXPECTED_PROFILE")
    return ConditionBundle(request=request, state=resolved_state,
                           aggregate_profile=aggregate_profile, support_ref=support_ref)


def load_schema_catalog(root: Path | None = None) -> Mapping[str, Mapping[str, Any]]:
    """Load the six published schemas and verify their identity tuple."""
    directory = root or Path(__file__).with_name("schemas")
    catalog = {}
    for kind, schema_id in SCHEMA_IDS.items():
        name = schema_id.rsplit("/", 1)[-1]
        schema = json.loads((directory / name).read_text(encoding="utf-8"))
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema" or schema.get("$id") != schema_id:
            _fail("UNSUPPORTED_SCHEMA", kind, "", "SCHEMA_CATALOG_ID_MISMATCH")
        catalog[kind] = MappingProxyType(schema)
    return MappingProxyType(catalog)
