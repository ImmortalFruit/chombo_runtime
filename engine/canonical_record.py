"""Strict v0.3 baseline hand/match record writer and reader.

The v0.1/v0.2 record readers remain intentionally separate.  This module never
guesses a format from an application version and never upgrades an older file.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from engine.hand_log import HandLog
from engine.match_record import match_record_document
from engine.match_orchestrator import MatchRun
from engine.match_reconstruction import _public_state, _tile_ids
from engine.record_replay import apply_state_delta
from engine.hand_viewer import viewer_document
from engine.observer_projection import Observer, ObserverKind, PUBLIC_OBSERVER, human_observer
from engine.game_state import SEATS, Seat
from engine.rule_drafts import DraftStore, DraftValidator, confirmed_ref, new_draft, snapshot
from engine.rule_profiles import _canonical_snapshot_id


ROOT = Path(__file__).resolve().parents[1]
FORMAT_VERSION = "1.0.0"
HAND_SCHEMA_ID = "https://chombo.local/schemas/chombo.hand_record.v0.3.schema.json"
MATCH_SCHEMA_ID = "https://chombo.local/schemas/chombo.match_record.v0.3.schema.json"
INTEGRITY_ALGORITHM = "canonical-json-sha256.v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 64
MAX_HANDS = 1024
MAX_EVENTS = 1_000_000
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_BUILD = re.compile(r"[0-9a-f]{40}")
_APP_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


class CanonicalRecordError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any] = {}):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {self.details}")


def _fail(code: str, **details: Any) -> None:
    raise CanonicalRecordError(code, details)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise CanonicalRecordError("MALFORMED_RECORD", {"reason": "NON_JSON_VALUE"}) from error


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _document_digest(document: Mapping[str, Any]) -> str:
    body = deepcopy(dict(document))
    integrity = body.get("integrity")
    if isinstance(integrity, Mapping):
        body["integrity"] = dict(integrity) | {"document_hash": None}
    return _digest(body)


def _tree(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        _fail("RESOURCE_LIMIT", resource="json_depth")
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            _fail("MALFORMED_RECORD", reason="NON_STRING_KEY")
        for child in value.values():
            _tree(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _tree(child, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        _fail("MALFORMED_RECORD", reason="NON_JSON_VALUE")
    elif type(value) is float and not math.isfinite(value):
        _fail("MALFORMED_RECORD", reason="NON_FINITE_NUMBER")


def _strict_load(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        _fail("RESOURCE_LIMIT", resource="file_size")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("MALFORMED_RECORD", reason="DUPLICATE_KEY", key=key)
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail("MALFORMED_RECORD", reason="NON_FINITE_NUMBER"))
        _tree(value)
    except CanonicalRecordError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        code = "RESOURCE_LIMIT" if isinstance(error, RecursionError) else "MALFORMED_RECORD"
        raise CanonicalRecordError(code, {"reason": type(error).__name__}) from error
    if type(value) is not dict:
        _fail("MALFORMED_RECORD", reason="ROOT_NOT_OBJECT")
    return value


def _fields(value: Any, expected: set[str], *, location: str) -> None:
    if type(value) is not dict or set(value) != expected:
        _fail("SCHEMA_VALIDATION_FAILED", location=location,
              expected_fields=sorted(expected), actual_fields=sorted(value) if type(value) is dict else None)


def _uuid(value: Any, *, field: str) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail("SCHEMA_VALIDATION_FAILED", location=field, reason="INVALID_UUID")


def _producer(app_version: str, build_revision: str, engine_version: str) -> dict[str, str]:
    if not _APP_VERSION.fullmatch(app_version):
        _fail("PRODUCER_METADATA_REQUIRED", field="app_version")
    if not _BUILD.fullmatch(build_revision):
        _fail("PRODUCER_METADATA_REQUIRED", field="build_revision")
    if not isinstance(engine_version, str) or not engine_version.strip():
        _fail("PRODUCER_METADATA_REQUIRED", field="engine_version")
    return {"app_id": "chombo", "app_version": app_version,
            "build_revision": build_revision, "engine_version": engine_version}


def development_producer(root: Path = ROOT) -> dict[str, str]:
    """Resolve the development build metadata before a UI session starts."""
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        try:
            revision = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))["source_revision"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise CanonicalRecordError("PRODUCER_METADATA_REQUIRED", {"field": "build_revision"}) from error
    return _producer("0.3.0-dev.r3-01", revision, "chombo-engine.v0.3")


def _official_rules(profile_id: str, snapshot_id: str, root: Path) -> dict[str, Any]:
    validator = DraftValidator(root)
    approval, capability, scope, concepts, profiles, code_hashes = validator.catalogs()
    selected = next(((entry, body) for entry, body in profiles
                     if entry["ref"]["profile_id"] == profile_id), None)
    if selected is None:
        _fail("DEPENDENCY_MISSING", dependency=profile_id)
    entry, body = selected
    if entry["ref"]["snapshot_id"] != snapshot_id:
        _fail("SNAPSHOT_MISMATCH", profile_id=profile_id)
    concept_ids = sorted({item for dimension in body["dimensions"].values()
                          for item in dimension["concept_ids"]})
    dependencies = [
        {"kind": "approval_catalog", "id": "custom_rule_approval.v1", "version": "1",
         "digest": _digest(approval), "body": approval},
        {"kind": "capability_catalog", "id": "custom_rule_capability.v1", "version": "1",
         "digest": _digest(capability), "body": capability},
        {"kind": "profile_scope", "id": "four_player_east_south_complete.v1", "version": "1",
         "digest": _digest(sorted(scope)), "body": sorted(scope)},
    ]
    dependencies.extend(
        {"kind": "concept", "id": concept_id, "version": "QUALIFIED",
         "digest": concepts[concept_id]["digest"]}
        for concept_id in concept_ids
    )
    dependencies.extend(
        {"kind": "engine_code", "id": path, "version": "1",
         "digest": "sha256:" + digest}
        for path, digest in sorted(code_hashes.items())
    )
    return {
        "selected_ref": deepcopy(entry["ref"]),
        "artifacts": [{"artifact_kind": "OFFICIAL", "identity": deepcopy(entry["ref"]),
                       "snapshot_algorithm": "legacy-rule-profile-sha256.v1",
                       "body": deepcopy(body)}],
        "dependencies": dependencies,
    }


def custom_rules_bundle(selected_ref: Mapping[str, Any], confirmed_documents: Sequence[Mapping[str, Any]],
                        *, root: Path = ROOT) -> dict[str, Any]:
    """Build a self-contained confirmed-custom artifact chain for a writer."""
    documents = {doc.get("profile_id"): deepcopy(dict(doc)) for doc in confirmed_documents}
    artifacts: list[dict[str, Any]] = []
    ref = deepcopy(dict(selected_ref))
    seen: set[str] = set()
    while ref.get("kind") == "CUSTOM_CONFIRMED":
        if ref.get("profile_id") in seen or len(seen) >= 32:
            _fail("REFERENCE_CYCLE" if ref.get("profile_id") in seen else "RESOURCE_LIMIT", resource="ancestors")
        seen.add(ref["profile_id"])
        body = documents.get(ref["profile_id"])
        if body is None or confirmed_ref(body) != ref or snapshot(body) != ref.get("snapshot_id"):
            _fail("DEPENDENCY_MISSING", dependency=ref.get("profile_id"))
        artifacts.append({"artifact_kind": "CUSTOM_CONFIRMED", "identity": deepcopy(ref),
                          "snapshot_algorithm": "custom-confirmed-sha256.v1", "body": body})
        ref = deepcopy(body["base_ref"])
    official = _official_rules(ref.get("profile_id"), ref.get("snapshot_id"), root)
    artifacts.extend(official["artifacts"])
    return {"selected_ref": deepcopy(dict(selected_ref)), "artifacts": artifacts,
            "dependencies": official["dependencies"]}


def custom_rules_for_selection(selection: Any, *, root: Path = ROOT) -> dict[str, Any] | None:
    """Capture a UI selection's confirmed chain; official selections need no override."""
    ref = getattr(selection, "reference", None)
    if not isinstance(ref, Mapping) or ref.get("kind") != "CUSTOM_CONFIRMED":
        return None
    directory = getattr(selection, "store_directory", None)
    if directory is None:
        _fail("DEPENDENCY_MISSING", dependency="custom_rule_store")
    store = DraftStore(Path(directory), DraftValidator(root))
    documents = []
    current = dict(ref)
    while current.get("kind") == "CUSTOM_CONFIRMED":
        body = store.lookup(current)
        if body is None:
            _fail("DEPENDENCY_MISSING", dependency=current.get("profile_id"))
        documents.append(body)
        current = body["base_ref"]
    return custom_rules_bundle(ref, documents, root=root)


def _participants(log: HandLog) -> list[dict[str, str]]:
    return [{"participant_id": item.participant_id, "display_name": item.display_name,
             "controller_id": item.controller_id} for item in log.participants]


def _hand(log: HandLog) -> dict[str, Any]:
    old = log.to_dict()
    return {key: deepcopy(old[key]) for key in (
        "hand_id", "seed", "rng_id", "policy_id", "seat_assignments",
        "authoritative_initial_state", "events", "result")}


def _header(kind: str, schema_id: str, *, app_version: str, build_revision: str,
            engine_version: str, baseline_status: str) -> dict[str, Any]:
    if baseline_status not in {"EVALUATION", "RELEASED"}:
        _fail("SCHEMA_VALIDATION_FAILED", location="baseline_status")
    if baseline_status == "RELEASED" and app_version != "0.3.0":
        _fail("PRODUCER_METADATA_REQUIRED", reason="RELEASED_REQUIRES_RELEASE_BUILD")
    return {"document_kind": kind, "schema_id": schema_id, "format_version": FORMAT_VERSION,
            "origin_kind": "CHOMBO_SESSION", "record_state": "FINALIZED",
            "mutation_policy": "IMMUTABLE", "producer": _producer(app_version, build_revision, engine_version),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "baseline_status": baseline_status}


def hand_record_document(log: HandLog, *, app_version: str, build_revision: str,
                         engine_version: str, source_session_id: str,
                         baseline_status: str = "EVALUATION", root: Path = ROOT,
                         rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _uuid(source_session_id, field="source_session_id")
    record_id, lineage_id = str(uuid4()), str(uuid4())
    document = _header("HAND_RECORD", HAND_SCHEMA_ID, app_version=app_version,
                       build_revision=build_revision, engine_version=engine_version,
                       baseline_status=baseline_status) | {
        "record_id": record_id,
        "lineage": {"lineage_id": lineage_id, "source_session_id": source_session_id,
                    "parent_checkpoint_id": None, "parent_record_id": None, "fork_position": None},
        "participants": _participants(log),
        "rules": deepcopy(rules) if rules is not None else _official_rules(log.rule_profile_id, log.rule_snapshot_id, root),
        "payload": {"hand": _hand(log)},
        "integrity": {"algorithm": INTEGRITY_ALGORITHM, "document_hash": None},
    }
    document["integrity"]["document_hash"] = _document_digest(document)
    validate_record(document, root=root, allow_evaluation=True)
    return document


def match_record_v03_document(run: MatchRun, *, app_version: str, build_revision: str,
                               engine_version: str, source_session_id: str,
                               baseline_status: str = "EVALUATION", root: Path = ROOT,
                               rules: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _uuid(source_session_id, field="source_session_id")
    legacy = match_record_document(run)
    first = run.hand_logs[0]
    document = _header("MATCH_RECORD", MATCH_SCHEMA_ID, app_version=app_version,
                       build_revision=build_revision, engine_version=engine_version,
                       baseline_status=baseline_status) | {
        "record_id": str(uuid4()),
        "lineage": {"lineage_id": str(uuid4()), "source_session_id": source_session_id,
                    "parent_checkpoint_id": None, "parent_record_id": None, "fork_position": None},
        "participants": _participants(first),
        "rules": deepcopy(rules) if rules is not None else _official_rules(first.rule_profile_id, first.rule_snapshot_id, root),
        "payload": {"match_id": legacy["match_id"], "format": "east_south", "seed": legacy["seed"],
                    "hands": [_hand(log) for log in run.hand_logs],
                    "transitions": deepcopy(legacy["transitions"]), "result": deepcopy(legacy["result"])},
        "integrity": {"algorithm": INTEGRITY_ALGORITHM, "document_hash": None},
    }
    document["integrity"]["document_hash"] = _document_digest(document)
    validate_record(document, root=root, allow_evaluation=True)
    return document


def _validate_header(document: Mapping[str, Any], *, allow_evaluation: bool) -> str:
    kind = document.get("document_kind")
    expected_schema = {"HAND_RECORD": HAND_SCHEMA_ID, "MATCH_RECORD": MATCH_SCHEMA_ID}.get(kind)
    if expected_schema is None:
        _fail("DOCUMENT_KIND_NOT_ALLOWED", document_kind=kind)
    if document.get("schema_id") != expected_schema or document.get("format_version") != FORMAT_VERSION:
        _fail("UNKNOWN_FORMAT_VERSION", schema_id=document.get("schema_id"),
              format_version=document.get("format_version"))
    if document.get("baseline_status") != "RELEASED" and not (
            allow_evaluation and document.get("baseline_status") == "EVALUATION"):
        _fail("EVALUATION_RECORD_NOT_ALLOWED")
    if (document.get("origin_kind"), document.get("record_state"), document.get("mutation_policy")) != (
            "CHOMBO_SESSION", "FINALIZED", "IMMUTABLE"):
        _fail("ORIGIN_NOT_SUPPORTED")
    return kind


def _remaining_engine_dependencies(missing: set, actual: set, root: Path) -> set:
    """Resolve only explicitly verified code hashes; never substitute rule data."""
    path = root / "engine/profiles/record_engine_code_compatibility.v1.json"
    if not path.is_file():
        return missing
    catalog = _strict_load(path.read_bytes())
    _fields(catalog, {"schema_version", "catalog_id", "entries"}, location="code_compatibility")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1 or catalog["catalog_id"] != "chombo.record_engine_code_compatibility.v1":
        _fail("DEPENDENCY_UNSUPPORTED", dependency="code_compatibility")
    entries = catalog["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= 32:
        _fail("DEPENDENCY_UNSUPPORTED", dependency="code_compatibility")
    remaining = set(missing)
    seen = set()
    for entry in entries:
        _fields(entry, {"path", "current_digest", "compatible_digests", "verification_id"}, location="code_compatibility.entry")
        identity = entry["path"]
        current = entry["current_digest"]
        compatible = entry["compatible_digests"]
        if (not isinstance(identity, str) or not identity.startswith("engine/") or not identity.endswith(".py")
                or not isinstance(current, str) or not _HASH.fullmatch(current)
                or type(compatible) is not list or not 1 <= len(compatible) <= 8
                or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in compatible)
                or len(set(compatible)) != len(compatible) or current in compatible
                or not isinstance(entry["verification_id"], str) or not entry["verification_id"]
                or identity in seen):
            _fail("DEPENDENCY_UNSUPPORTED", dependency="code_compatibility")
        seen.add(identity)
        dependency = ("engine_code", identity, current)
        if dependency in remaining and any(("engine_code", identity, digest) in actual for digest in compatible):
            remaining.remove(dependency)
    return remaining


def _validate_rules(rules: Any, root: Path) -> tuple[str, str]:
    _fields(rules, {"selected_ref", "artifacts", "dependencies"}, location="rules")
    ref = rules["selected_ref"]
    if type(ref) is not dict or ref.get("kind") not in {"OFFICIAL", "CUSTOM_CONFIRMED"}:
        _fail("DEPENDENCY_UNSUPPORTED", reason="OFFICIAL_OR_CONFIRMED_REQUIRED")
    artifacts = rules["artifacts"]
    if type(artifacts) is not list or not artifacts or len(artifacts) > 33:
        _fail("DEPENDENCY_MISSING", dependency="selected_artifact")
    identities: set[str] = set()
    custom_bodies: dict[str, Mapping[str, Any]] = {}
    official_ref = None
    for index, artifact in enumerate(artifacts):
        _fields(artifact, {"artifact_kind", "identity", "snapshot_algorithm", "body"}, location=f"rules.artifacts[{index}]")
        identity, body = artifact["identity"], artifact["body"]
        profile_id = identity.get("profile_id") if isinstance(identity, Mapping) else None
        if not profile_id or profile_id in identities:
            _fail("SNAPSHOT_MISMATCH", reason="ARTIFACT_IDENTITY")
        identities.add(profile_id)
        if artifact["artifact_kind"] == "OFFICIAL":
            if artifact["snapshot_algorithm"] != "legacy-rule-profile-sha256.v1" or _canonical_snapshot_id(body) != identity.get("snapshot_id"):
                _fail("SNAPSHOT_MISMATCH", reason="ARTIFACT_BODY")
            official_ref = identity
        elif artifact["artifact_kind"] == "CUSTOM_CONFIRMED":
            if artifact["snapshot_algorithm"] != "custom-confirmed-sha256.v1" or confirmed_ref(body) != identity or snapshot(body) != identity.get("snapshot_id"):
                _fail("SNAPSHOT_MISMATCH", reason="ARTIFACT_BODY")
            custom_bodies[profile_id] = body
        else:
            _fail("DEPENDENCY_UNSUPPORTED", artifact_kind=artifact["artifact_kind"])
    if artifacts[0]["identity"] != ref or official_ref is None:
        _fail("SNAPSHOT_MISMATCH", reason="ARTIFACT_CHAIN_ROOT")
    trusted = _official_rules(official_ref.get("profile_id"), official_ref.get("snapshot_id"), root)
    if ref["kind"] == "CUSTOM_CONFIRMED":
        validator = DraftValidator(root)
        result = validator.validate(new_draft(ref, "record replay"),
                                    lambda item: custom_bodies.get(item.get("profile_id")),
                                    _confirmed_root=True)
        if result["status"] != "VALIDATED":
            _fail(result["status"], issues=result["issues"])
    trusted_dependencies = {(item["kind"], item["id"], item["digest"])
                            for item in trusted["dependencies"]}
    actual_dependencies = rules["dependencies"]
    if type(actual_dependencies) is not list:
        _fail("SCHEMA_VALIDATION_FAILED", location="rules.dependencies")
    for index, item in enumerate(actual_dependencies):
        if type(item) is not dict or not {"kind", "id", "version", "digest"} <= set(item) or not _HASH.fullmatch(str(item["digest"])):
            _fail("SCHEMA_VALIDATION_FAILED", location=f"rules.dependencies[{index}]")
        if "body" in item and _digest(item["body"]) != item["digest"]:
            _fail("SNAPSHOT_MISMATCH", dependency=item["id"])
    actual_set = {(item["kind"], item["id"], item["digest"]) for item in actual_dependencies}
    missing = trusted_dependencies - actual_set
    if missing:
        missing = _remaining_engine_dependencies(missing, actual_set, root)
        if missing:
            _fail("DEPENDENCY_MISSING", dependencies=sorted(item[1] for item in missing))
    return ref["profile_id"], ref["snapshot_id"]


def _validate_event(event: Any, expected: int, prior_ids: set[str]) -> None:
    common = {"sequence", "event_id", "kind", "actor", "caused_by_event_ids",
              "public_data", "authoritative_data", "applied_concept_ids"}
    _fields(event, common, location=f"event[{expected}]")
    if event["sequence"] != expected or type(event["event_id"]) is not str or not event["event_id"]:
        _fail("EVENT_SEQUENCE_MISMATCH", expected=expected)
    if event["event_id"] in prior_ids or any(cause not in prior_ids for cause in event["caused_by_event_ids"]):
        _fail("INVALID_CAUSE_REFERENCE", event_id=event["event_id"])
    allowed = {
        "hand_started": (set(), set(), set()),
        "action_applied": ({"action_id", "kind", "actor", "tile_ids", "claimed_tile_id", "target_seat", "target_meld_index"}, {"state_delta"}, {"state_delta"}),
        "response_resolved": ({"adopted_actions"}, {"state_delta"}, {"state_delta"}),
        "tenpai_declared": ({"declaration"}, set(), set()),
        "hand_ended": (
            {"reason", "winners", "declared_tenpai_player_ids"},
            {"win_detail", "win_details", "special_result_family", "qualifier_seats"},
            set(),
        ),
        "settlement_applied": ({"point_deltas", "final_scores", "state_delta"}, set(), {"state_delta"}),
    }.get(event["kind"])
    if allowed is None:
        _fail("UNKNOWN_EVENT_KIND", kind=event["kind"])
    public_keys = set(event["public_data"]) if type(event["public_data"]) is dict else None
    authoritative_keys = set(event["authoritative_data"]) if type(event["authoritative_data"]) is dict else None
    required_public, optional_public, expected_authoritative = allowed
    if public_keys is None or not required_public <= public_keys or not public_keys <= required_public | optional_public:
        _fail("SCHEMA_VALIDATION_FAILED", location=f"event[{expected}].public_data")
    if authoritative_keys != expected_authoritative:
        _fail("SCHEMA_VALIDATION_FAILED", location=f"event[{expected}].authoritative_data")
    prior_ids.add(event["event_id"])


def _validate_hand(hand: Any, profile_id: str, snapshot_id: str,
                   *, hand_index: int) -> dict[str, Any]:
    _fields(hand, {"hand_id", "seed", "rng_id", "policy_id", "seat_assignments",
                   "authoritative_initial_state", "events", "result"}, location="hand")
    if type(hand["events"]) is not list or not hand["events"]:
        _fail("EMPTY_EVENT_LOG")
    initial = hand["authoritative_initial_state"]
    if not isinstance(initial, Mapping) or (initial.get("rule_profile_id"), initial.get("rule_snapshot_id")) != (profile_id, snapshot_id):
        _fail("RULE_SNAPSHOT_MISMATCH", hand_id=hand.get("hand_id"))
    prior: set[str] = set()
    state = deepcopy(dict(initial))
    _tile_ids(state, hand_index=hand_index, sequence=-1)
    public_state = _public_state(state, hand_index=hand_index, sequence=-1)
    for expected, event in enumerate(hand["events"]):
        _validate_event(event, expected, prior)
        state = apply_state_delta(state, event["authoritative_data"].get("state_delta", {}))
        _tile_ids(state, hand_index=hand_index, sequence=expected)
        public_state = apply_state_delta(public_state, event["public_data"].get("state_delta", {}))
        if public_state != _public_state(state, hand_index=hand_index, sequence=expected):
            _fail("PUBLIC_STATE_MISMATCH", hand_index=hand_index, sequence=expected)
    if hand["events"][0]["kind"] != "hand_started" or hand["events"][-1]["kind"] != "settlement_applied":
        _fail("INVALID_EVENT_BOUNDARY", hand_id=hand["hand_id"])
    result, ended, settled = hand["result"], hand["events"][-2], hand["events"][-1]
    if (
        ended["kind"] != "hand_ended"
        or ended["public_data"]["reason"] != result.get("end_reason")
        or ended["public_data"]["winners"] != result.get("winners")
    ):
        _fail("HAND_RESULT_MISMATCH", hand_id=hand["hand_id"])
    special_families = {
        "nagashi_mangan": "NAGASHI_MANGAN",
        "nine_terminals": "ABORTIVE_DRAW",
        "four_winds_first_discard": "ABORTIVE_DRAW",
        "four_riichi": "ABORTIVE_DRAW",
        "four_kans": "ABORTIVE_DRAW",
        "three_ron": "ABORTIVE_DRAW",
    }
    expected_family = special_families.get(result.get("end_reason"))
    if ended["public_data"].get("special_result_family") != expected_family:
        _fail("SPECIAL_HAND_END_FAMILY_MISMATCH", hand_id=hand["hand_id"])
    expected_qualifiers = result.get("winners", []) if expected_family == "NAGASHI_MANGAN" else []
    if ended["public_data"].get("qualifier_seats", []) != expected_qualifiers:
        _fail("SPECIAL_HAND_END_QUALIFIER_MISMATCH", hand_id=hand["hand_id"])
    if settled["public_data"]["point_deltas"] != result.get("point_deltas") or settled["public_data"]["final_scores"] != result.get("final_scores"):
        _fail("SETTLEMENT_RESULT_MISMATCH", hand_id=hand["hand_id"])
    final_scores = {player["seat"]: player["score"] for player in state.get("players", [])}
    if final_scores != result.get("final_scores"):
        _fail("FINAL_STATE_SCORE_MISMATCH", hand_id=hand["hand_id"])
    return state


def validate_record(document: Mapping[str, Any], *, root: Path = ROOT,
                    allow_evaluation: bool = False) -> None:
    _tree(document)
    if "schema_version" in document and "schema_id" not in document:
        _fail("UNKNOWN_FORMAT_VERSION", schema_version=document.get("schema_version"))
    common = {"document_kind", "schema_id", "format_version", "origin_kind", "record_state",
              "mutation_policy", "producer", "created_at", "baseline_status", "record_id",
              "lineage", "participants", "rules", "payload", "integrity"}
    _fields(document, common, location="$" )
    kind = _validate_header(document, allow_evaluation=allow_evaluation)
    _uuid(document["record_id"], field="record_id")
    _fields(document["producer"], {"app_id", "app_version", "build_revision", "engine_version"}, location="producer")
    _producer(document["producer"]["app_version"], document["producer"]["build_revision"], document["producer"]["engine_version"])
    if document["baseline_status"] == "RELEASED" and document["producer"]["app_version"] != "0.3.0":
        _fail("PRODUCER_METADATA_REQUIRED", reason="RELEASED_REQUIRES_RELEASE_BUILD")
    if document["producer"]["app_id"] != "chombo":
        _fail("PRODUCER_METADATA_REQUIRED", field="app_id")
    lineage = document["lineage"]
    _fields(lineage, {"lineage_id", "source_session_id", "parent_checkpoint_id", "parent_record_id", "fork_position"}, location="lineage")
    _uuid(lineage["lineage_id"], field="lineage.lineage_id")
    _uuid(lineage["source_session_id"], field="lineage.source_session_id")
    if any(lineage[key] is not None for key in ("parent_checkpoint_id", "parent_record_id", "fork_position")):
        _fail("SCHEMA_VALIDATION_FAILED", location="lineage", reason="ROOT_PARENT_MUST_BE_NULL")
    participants = document["participants"]
    if type(participants) is not list or len(participants) != 4:
        _fail("INVALID_PARTICIPANTS")
    for index, participant in enumerate(participants):
        _fields(participant, {"participant_id", "display_name", "controller_id"}, location=f"participants[{index}]")
    if len({item["participant_id"] for item in participants}) != 4:
        _fail("INVALID_PARTICIPANTS")
    profile_id, snapshot_id = _validate_rules(document["rules"], root)
    payload = document["payload"]
    if kind == "HAND_RECORD":
        _fields(payload, {"hand"}, location="payload")
        hands = [payload["hand"]]
    else:
        _fields(payload, {"match_id", "format", "seed", "hands", "transitions", "result"}, location="payload")
        if payload["format"] != "east_south" or type(payload["hands"]) is not list or not payload["hands"]:
            _fail("INVALID_MATCH_RECORD_IDENTITY")
        if len(payload["hands"]) > MAX_HANDS or len(payload["transitions"]) != len(payload["hands"]):
            _fail("RESOURCE_LIMIT" if len(payload["hands"]) > MAX_HANDS else "TRANSITION_COUNT_MISMATCH", resource="hands")
        hands = payload["hands"]
    if sum(len(hand.get("events", [])) for hand in hands if isinstance(hand, Mapping)) > MAX_EVENTS:
        _fail("RESOURCE_LIMIT", resource="events")
    hand_ids: set[str] = set()
    final_states = []
    for hand_index, hand in enumerate(hands):
        final_states.append(_validate_hand(hand, profile_id, snapshot_id, hand_index=hand_index))
        if hand["hand_id"] in hand_ids:
            _fail("INVALID_HAND_ID", hand_id=hand["hand_id"])
        hand_ids.add(hand["hand_id"])
    if kind == "MATCH_RECORD":
        for index, (hand, transition, state) in enumerate(zip(hands, payload["transitions"], final_states)):
            if transition.get("completed_hand_id") != hand["hand_id"] or transition.get("scores") != hand["result"]["final_scores"]:
                _fail("TRANSITION_HAND_MISMATCH", hand_index=index)
            if state.get("riichi_deposits") != transition.get("carried_riichi_deposits"):
                _fail("FINAL_DEPOSIT_MISMATCH", hand_index=index)
            if index:
                previous = payload["transitions"][index - 1]
                initial = hand["authoritative_initial_state"]
                assignments = hand["seat_assignments"]
                previous_assignments = hands[index - 1]["seat_assignments"]
                participant_scores = {assignments[player["seat"]]: player["score"] for player in initial["players"]}
                expected_scores = {previous_assignments[seat]: score for seat, score in previous["scores"].items()}
                if participant_scores != expected_scores:
                    _fail("INTER_HAND_STATE_MISMATCH", hand_index=index, field="scores")
        result = payload["result"]
        last_transition = payload["transitions"][-1]
        if (result.get("completed_hand_id") != hands[-1]["hand_id"] or
                result.get("scores_before_deposit") != last_transition.get("scores") or
                result.get("remaining_riichi_deposits") != last_transition.get("carried_riichi_deposits")):
            _fail("MATCH_FINAL_STATE_MISMATCH")
    integrity = document["integrity"]
    _fields(integrity, {"algorithm", "document_hash"}, location="integrity")
    if integrity["algorithm"] != INTEGRITY_ALGORITHM or not _HASH.fullmatch(str(integrity["document_hash"])):
        _fail("MISSING_OR_UNSUPPORTED_INTEGRITY")
    if _document_digest(document) != integrity["document_hash"]:
        _fail("RECORD_HASH_MISMATCH")


def read_record(path: Path, *, root: Path = ROOT, allow_evaluation: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CanonicalRecordError("RECORD_READ_FAILED", {"path": str(path)}) from error
    document = _strict_load(raw)
    validate_record(document, root=root, allow_evaluation=allow_evaluation)
    return deepcopy(document)


def read_record_bytes(raw: bytes, *, root: Path = ROOT,
                      allow_evaluation: bool = False) -> dict[str, Any]:
    """Read an uploaded record without creating or modifying a filesystem entry."""
    if not isinstance(raw, bytes):
        _fail("MALFORMED_RECORD", reason="INPUT_NOT_BYTES")
    document = _strict_load(raw)
    validate_record(document, root=root, allow_evaluation=allow_evaluation)
    return deepcopy(document)


def write_record(document: Mapping[str, Any], path: Path, *, root: Path = ROOT,
                 allow_evaluation: bool = False) -> None:
    validate_record(document, root=root, allow_evaluation=allow_evaluation)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_canonical(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _viewer_hand(record: Mapping[str, Any], hand: Mapping[str, Any], observer: Observer) -> dict[str, Any]:
    """Hydrate the established Viewer input without restoring an old Reader."""
    ref = record["rules"]["selected_ref"]
    initial = hand["authoritative_initial_state"]
    hydrated = deepcopy(dict(hand)) | {
        "schema_version": "chombo.hand_record.v0.3",
        "record_format": "semantic-delta.v2",
        "rule_profile_id": ref["profile_id"],
        "rule_snapshot_id": ref["snapshot_id"],
        "participants": deepcopy(record["participants"]),
        "initial_public_state": _public_state(initial, hand_index=0, sequence=-1),
    }
    return viewer_document(hydrated, observer)


def hand_record_viewer_document(record: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER,
                                *, allow_evaluation: bool = False) -> dict[str, Any]:
    validate_record(record, allow_evaluation=allow_evaluation)
    if record["document_kind"] != "HAND_RECORD":
        _fail("DOCUMENT_KIND_NOT_ALLOWED", document_kind=record["document_kind"])
    return _viewer_hand(record, record["payload"]["hand"], observer)


def match_record_viewer_document(record: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER,
                                 *, allow_evaluation: bool = False) -> dict[str, Any]:
    """Project v0.3 match data into the unchanged match Replay UI contract."""
    validate_record(record, allow_evaluation=allow_evaluation)
    if record["document_kind"] != "MATCH_RECORD":
        _fail("DOCUMENT_KIND_NOT_ALLOWED", document_kind=record["document_kind"])
    payload = record["payload"]
    participants = {item["participant_id"]: item for item in record["participants"]}
    observed_participant_id = None
    if observer.kind is ObserverKind.HUMAN_SEAT and observer.seat is not None:
        observed_participant_id = payload["hands"][0]["seat_assignments"][observer.seat.value]
    hands = []
    for index, hand in enumerate(payload["hands"]):
        hand_observer = observer
        if observed_participant_id is not None:
            observed_seat = next(seat for seat, participant_id in hand["seat_assignments"].items()
                                 if participant_id == observed_participant_id)
            hand_observer = human_observer(Seat(observed_seat))
        projected = _viewer_hand(record, hand, hand_observer)
        projected["transition_after"] = deepcopy(payload["transitions"][index])
        hands.append(projected)
    players = [deepcopy(dict(item)) | {"display_name": participants[item["participant_id"]]["display_name"]}
               for item in payload["result"]["players"]]
    return {
        "match_id": payload["match_id"], "schema_version": record["format_version"],
        "observer": hands[0]["observer"], "participants": deepcopy(record["participants"]),
        "initial_seat_assignments": deepcopy(payload["hands"][0]["seat_assignments"]),
        "hands": hands,
        "result": {key: deepcopy(value) for key, value in payload["result"].items()
                   if key != "applied_concept_ids"} | {"players": players},
    }
