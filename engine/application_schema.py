"""Closed application wire types, shared by Python validation and OpenAPI.

This is a deliberately small schema vocabulary, not a general JSON Schema
implementation. Unsupported validation keywords fail closed.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any


CONTRACT_ID = "chombo.application"
CONTRACT_VERSION = "1.0.0"
CONTRACT_VERSIONS = (CONTRACT_VERSION, "1.1.0")
STRING = {"type": "string", "minLength": 1, "maxLength": 16384}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
BOOLEAN = {"type": "boolean"}
NULL = {"type": "null"}


def obj(properties, required=None, *, additional=False):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": additional}


def array(items):
    return {"type": "array", "items": items, "maxItems": 4096}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def nullable(schema):
    return {"anyOf": [schema, NULL]}


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


SEAT = enum("east", "south", "west", "north")
SESSION_REF = obj({"session_id": STRING, "lineage_id": STRING, "boundary_revision": INTEGER})
REPLAY_REF = obj({"replay_id": STRING, "record_id": STRING, "document_hash": STRING,
                  "hand_index": INTEGER, "event_index": INTEGER})
SESSION_INPUT = obj({"session_ref": SESSION_REF})
REVISION_INPUT = {"interaction_id": STRING, "expected_revision": INTEGER}

# Deferred operations are still named and typed; their result is explicitly
# unavailable until the corresponding adapter implements the existing contract.
# No success payload is promised for an unconnected operation.
INPUTS = {
    "hand.analyze": obj({"notation": STRING, "fixed_meld_count": {"type": "integer", "minimum": 0, "maximum": 4},
                         "fixed_tiles": {"type": "string", "maxLength": 16384}}, ["notation"]),
    "position.snapshot": obj({"conversation_ref_id": STRING}),
    "position.analyze": obj({"conversation_ref_id": STRING}),
    "session.read": obj({"session_id": STRING}),
    "session.action": obj({"session_ref": SESSION_REF, "action_id": STRING}),
    **{name: SESSION_INPUT for name in ("session.leave", "session.rematch", "session.save_record")},
    **{name: obj({}) for name in ("checkpoint.list", "recovery.list", "rules.list")},
    "checkpoint.save": obj({"session_ref": SESSION_REF, "display_name": STRING, "expected_generation": INTEGER}),
    "checkpoint.rename": obj({"checkpoint_id": STRING, "display_name": STRING, "expected_generation": INTEGER}),
    "checkpoint.delete": obj({"checkpoint_id": STRING, "expected_generation": INTEGER}),
    "checkpoint.restore": obj({"checkpoint_id": STRING, "expected_active_session_id": nullable(STRING), "expected_generation": INTEGER}),
    **{name: obj({"operation_id": STRING, "expected_generation": INTEGER,
                 "expected_active_session_id": nullable(STRING)})
       for name in ("recovery.retry", "recovery.branch")},
    "recovery.discard": obj({"operation_id": STRING, "expected_generation": INTEGER}),
    "replay.open": obj({"filename": {"type": "string", "minLength": 1, "maxLength": 255},
                        "record_base64": {"type": "string", "minLength": 1, "maxLength": 89478488}}),
    "replay.read": obj({"replay_ref": REPLAY_REF, "perspective": SEAT, "reveal_all": BOOLEAN}),
    "replay.seek": obj({"replay_ref": REPLAY_REF, "hand_index": INTEGER, "event_index": INTEGER}),
    "replay.grant": obj({"replay_ref": REPLAY_REF, "scope": enum("SELF_SEAT", "ALL_HANDS"), "seat": SEAT}, ["replay_ref", "scope"]),
    "replay.revoke": obj({"permission_grant_id": STRING}),
    "position.issue": obj({"source_kind": enum("LIVE_SESSION", "ACTIVE_SESSION_REPLAY", "COMPLETED_REPLAY"),
                           "session_ref": SESSION_REF, "replay_ref": REPLAY_REF,
                           "requested_subject": enum("SELF", "OTHER_SEAT", "PUBLIC_TABLE", "AMBIGUOUS"),
                           "subject_seat": SEAT, "permission_grant_id": STRING}, ["source_kind", "requested_subject"]),
    "rules.draft_confirm": obj({"draft_id": STRING, "expected_draft_digest": STRING}),
    "rules.draft_read": obj({"draft_id": STRING}),
    "rules.draft_discard": obj({"draft_id": STRING, "expected_draft_digest": STRING}),
    "rules.custom_discard": obj({"profile_id": STRING, "expected_snapshot_id": STRING}),
    "hand.render": obj({"notation": STRING}),
    "operation.read": obj({"operation_id": STRING}),
    "interaction.create": obj({"runtime_instance_id": STRING, "channel": enum("LOCAL_TEMPLATE", "CODEX", "STANDALONE_BYOK"),
                               "conversation_ref_id": STRING, "intent": enum("SHAPE_ANALYSIS", "PUBLIC_TABLE"), "question": STRING},
                              ["runtime_instance_id", "channel", "conversation_ref_id", "intent"]),
    "interaction.read": obj({"interaction_id": STRING}),
    "interaction.confirm": obj({**REVISION_INPUT, "confirmation_id": STRING, "choice_id": STRING}),
    "interaction.cancel": obj(REVISION_INPUT),
    "interaction.authorize_send": obj({**REVISION_INPUT, "send_preview_id": STRING, "decision": enum("ALLOW", "DENY")}),
    "interaction.retry": obj({**REVISION_INPUT, "failure_id": STRING}),
}

# Complex legacy documents remain referenced by their exact existing schema.
RULE_REFERENCE = obj({"kind": enum("OFFICIAL", "CUSTOM_CONFIRMED"), "ruleset_id": STRING, "ruleset_version": {"anyOf": [STRING, INTEGER]},
                      "profile_id": STRING, "snapshot_id": STRING})
RULE_SELECTOR = {"oneOf": [obj({"kind": enum("OFFICIAL"), "alias": STRING}),
                           obj({"kind": enum("CUSTOM_CONFIRMED"), "reference": RULE_REFERENCE})]}
CONDITION_PLAYER = enum("p0", "p1", "p2", "p3")
CONDITION_RESULTS = enum("TARGET_RON", "TARGET_TSUMO", "TARGET_DEALT_IN", "OTHER_WIN", "DRAW")
INPUTS["condition.options"] = obj({})
INPUTS["condition.query"] = obj({
    "rule_ref": RULE_REFERENCE,
    "ranking_scope": enum("SINGLE_MATCH_RANK", "AGGREGATE_RANK"),
    "players": {"type": "array", "maxItems": 4, "items": obj({
        "participant_id": CONDITION_PLAYER, "seat": SEAT,
        "score": {"type": "integer", "minimum": -10000000, "maximum": 10000000},
        "aggregate_points": nullable({"type": "string", "pattern": r"^-?(0|[1-9][0-9]{0,8})(\.[0-9]{1,4})?$", "maxLength": 15}),
    })},
    "target_participant_id": CONDITION_PLAYER,
    "target_rank": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Rank N or better: numeric rank <= N; maintenance of an already better rank qualifies."},
    "comeback_tie_policy": enum("REQUIRE_STRICT_LEAD", "CHASER_ADVANTAGE"),
    "round_wind": SEAT,
    "hand_number": {"type": "integer", "minimum": 1, "maximum": 4},
    "dealer_participant_id": CONDITION_PLAYER,
    "honba": {"type": "integer", "minimum": 0, "maximum": 999},
    "riichi_deposits": {"type": "integer", "minimum": 0, "maximum": 1000},
    "evaluation_point": enum("HAND_END", "MATCH_END"),
    "allowed_result_kinds": {"type": "array", "maxItems": 5, "items": CONDITION_RESULTS},
})
POINT_DECIMAL = {"type": "string", "pattern": r"^-?(0|[1-9][0-9]{0,8})(\.[0-9]{1,4})?$", "maxLength": 15}
INPUTS["condition.points_query"] = obj({
    **{key: deepcopy(value) for key, value in INPUTS["condition.query"]["properties"].items()
       if key not in {"ranking_scope", "target_rank", "comeback_tie_policy", "evaluation_point"}},
    "target_points": POINT_DECIMAL,
    "comparison": enum("GT", "GTE"),
    "aggregation_policy": enum("EXACT_SUM_NO_EXTRA_ADJUSTMENTS"),
})
for condition_op in ("condition.query", "condition.points_query"):
    INPUTS[condition_op]["properties"]["force_match_end"] = BOOLEAN
INPUTS.update({
    "session.start": obj({"format": enum("single_hand", "east_south"), "rules": RULE_SELECTOR,
                          "seed": {"type": "string", "pattern": "^(0|[1-9][0-9]*)$", "maxLength": 128},
                          "position": obj({"round_wind": SEAT, "hand_number": INTEGER, "honba": INTEGER,
                                           "riichi_deposits": INTEGER, "human_seat": nullable(SEAT),
                                           "seat_scores": obj({seat: {"type": "integer", "minimum": -9007199254740991,
                                                                      "maximum": 9007199254740991}
                                                               for seat in SEAT["enum"]})}, [])}, ["format", "rules"]),
    "hand.generate": obj({"rules": RULE_SELECTOR, "seed": {"type": "string", "pattern": "^(0|[1-9][0-9]*)$", "maxLength": 128}}, ["rules"]),
    "rules.draft_create": obj({"base_ref": RULE_REFERENCE, "display_name": STRING}),
    "rules.draft_save": obj({"draft_id": STRING, "expected_draft_digest": STRING, "display_name": STRING,
                             "overrides": {"$ref": "custom_rule_draft.v1.schema.json#/properties/overrides"},
                             "request_summary": STRING}),
    "condition.calculate": obj({key: {"$ref": f"chombo.{filename}.v0.4.schema.json"}
                                for key, filename in (("request", "condition_request"), ("state", "condition_state"),
                                                      ("support_ref", "condition_support_ref"), ("aggregate_profile", "aggregate_rank_profile"))}, ["request"]),
})
WIN_CONTEXT = obj({**{key: BOOLEAN for key in ("concealed", "riichi_established", "double_riichi_established",
                   "ippatsu_right_active", "same_turn_missed_win", "riichi_missed_win", "declared")},
                   **{key: INTEGER for key in ("concealed_triplet_or_quad_count", "quad_count")},
                   "prevailing_wind": STRING, "seat_wind": STRING, "winning_event": STRING, "player": STRING,
                   "own_discards": array(STRING), "fixed_melds": array(obj({"tiles": array(STRING)}))}, [])
INPUTS["hand.win"] = obj({"notation": STRING, "win_tile": STRING, "method": enum("ron", "tsumo"), "context": WIN_CONTEXT})
INPUTS["hand.resolve_ron"] = obj({"claimants": array(obj({"player": STRING, "can_win": BOOLEAN,
    "relative_to_discarder": enum("next_player", "opposite_player", "previous_player") })),
    "multiple_ron_profile": {"$ref": "chombo.multiple_ron_profile.v1.schema.json"}}, ["claimants"])
SCORE_CANDIDATE = obj({"fu": INTEGER, "han": INTEGER, "yakuman_units": INTEGER,
                       "method": enum("ron", "tsumo"), "dealer": BOOLEAN, "profile_id": STRING,
                       "honba_count": INTEGER}, ["method", "dealer", "profile_id"])
INPUTS["score.calculate"] = {"oneOf": [obj({"candidate": SCORE_CANDIDATE}), obj({"candidates": array(SCORE_CANDIDATE)})]}

CONNECTED = frozenset({"hand.analyze", "position.snapshot", "position.analyze"})


def json_tree(value: Any, depth=0, path=()) -> None:
    """Reject Python-only values, nonfinite numbers and excessive nesting."""
    if depth > 48:
        raise ValueError("RESOURCE_LIMIT")
    if type(value) is dict:
        if len(value) > 256 or any(type(key) is not str for key in value):
            raise ValueError("INVALID_REQUEST")
        for key, child in value.items():
            json_tree(child, depth + 1, path + (key,))
    elif type(value) is list:
        if len(value) > 4096:
            raise ValueError("RESOURCE_LIMIT")
        for child in value:
            json_tree(child, depth + 1, path + (None,))
    elif type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("INVALID_REQUEST")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("INVALID_REQUEST")
    elif type(value) is str:
        value.encode("utf-8")
        preview_text = any(path[index:index + 2] == ('send_preview', 'payload') for index in range(len(path) - 1))
        if len(value) > (89478488 if path == ("input", "record_base64") else 262144 if preview_text else 16384):
            raise ValueError("RESOURCE_LIMIT")
    elif type(value) not in (bool, type(None)):
        raise ValueError("INVALID_REQUEST")


def validate(value, schema, schemas=None):
    """Validate only the vocabulary used by the connected wire schemas."""
    supported = {"$ref", "type", "properties", "required", "additionalProperties", "items", "maxItems",
                 "minLength", "maxLength", "minimum", "maximum", "const", "enum", "pattern",
                 "oneOf", "anyOf", "description"}
    if type(schema) is dict and schema.keys() - supported:
        raise ValueError("UNSUPPORTED_SCHEMA_KEYWORD")
    if schema is False:
        raise ValueError("INVALID_REQUEST")
    if "$ref" in schema:
        return validate(value, schemas[schema["$ref"].rsplit("/", 1)[-1]], schemas)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = 0
            for candidate in schema[keyword]:
                try:
                    validate(value, candidate, schemas)
                    matches += 1
                except ValueError:
                    pass
            if not matches or (keyword == "oneOf" and matches != 1):
                raise ValueError("INVALID_REQUEST")
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kind and type(value) is not types[kind]:
        raise ValueError("INVALID_REQUEST")
    if "const" in schema and value != schema["const"]:
        raise ValueError("INVALID_REQUEST")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("INVALID_REQUEST")
    if type(value) is dict:
        props = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            raise ValueError("INVALID_REQUEST")
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in props:
                validate(child, props[key], schemas)
            elif additional is False:
                raise ValueError("INVALID_REQUEST")
            elif type(additional) is dict:
                validate(child, additional, schemas)
    if type(value) is list:
        if len(value) > schema.get("maxItems", 4096):
            raise ValueError("RESOURCE_LIMIT")
        for child in value:
            validate(child, schema["items"], schemas)
    if type(value) is str:
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16384):
            raise ValueError("INVALID_REQUEST")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError("INVALID_REQUEST")
    if type(value) is int and not schema.get("minimum", -9007199254740991) <= value <= schema.get("maximum", 9007199254740991):
        raise ValueError("INVALID_REQUEST")


POSITION_TARGET = obj({"kind": enum("POSITION"), "conversation_ref_id": STRING,
                       "intent": enum("SHAPE_ANALYSIS", "PUBLIC_TABLE")})
KNOWLEDGE_TARGET = obj({"kind": enum("KNOWLEDGE"), "lesson_id": STRING})
INPUTS_V11 = {**deepcopy(INPUTS), "interaction.list": obj({"runtime_instance_id": STRING})}
def dialogue_create_input():
    branches = []
    for legacy in (True, False):
        for byok in (False, True):
            properties = {"runtime_instance_id": STRING,
                "channel": enum("STANDALONE_BYOK") if byok else enum("LOCAL_TEMPLATE", "CODEX")}
            if legacy:
                properties.update(conversation_ref_id=STRING, intent=enum("SHAPE_ANALYSIS", "PUBLIC_TABLE"))
            else:
                properties["target"] = {"oneOf": [POSITION_TARGET, KNOWLEDGE_TARGET]}
            required = list(properties)
            properties["question"] = {**STRING, "description": "UTF-8で16KiB以内。BYOKは必須。"}
            if byok:
                required.append("question")
            branches.append(obj(properties, required))
    return {"oneOf": branches}


INPUTS_V11["interaction.create"] = dialogue_create_input()


def inputs_for(version):
    return INPUTS_V11 if version == "1.1.0" else INPUTS


def request_schema(operation, version=CONTRACT_VERSION):
    return obj({"contract_id": {"const": CONTRACT_ID}, "contract_version": {"const": version},
                "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
                "operation": {"const": operation}, "input": inputs_for(version)[operation]})


def capabilities():
    return {"contract_id": CONTRACT_ID, "contract_versions": list(CONTRACT_VERSIONS),
            "operations": [{"operation": name, "input_type": name + ".input", "result_type": name + ".result",
                            "type_version": CONTRACT_VERSION, "available": name in CONNECTED,
                            "reason_code": None if name in CONNECTED else "CAPABILITY_UNAVAILABLE"}
                           for name in sorted(INPUTS)]}


def openapi_document():
    """Generate a Swagger-compatible 3.1 document without claiming HTTP exists."""
    schemas = {name + ".input": deepcopy(schema) for name, schema in INPUTS.items()}
    schemas.update({name + ".request": request_schema(name) for name in INPUTS})
    for name in INPUTS:
        schemas[name + ".request"]["properties"]["input"] = ref(name + ".input")
    for name in INPUTS:
        if name not in CONNECTED:
            schemas[name + ".result"] = {"not": {}, "description": "未接続。成功結果型は接続時に既存結果から具体化する。"}
    schemas["Request"] = {"oneOf": [ref(name + ".request") for name in INPUTS]}
    schemas.update(result_schemas())
    for name in INPUTS_V11:
        schemas["v1_1." + name + ".input"] = deepcopy(INPUTS_V11[name])
        schemas["v1_1." + name + ".request"] = request_schema(name, "1.1.0")
        schemas["v1_1." + name + ".request"]["properties"]["input"] = ref("v1_1." + name + ".input")
    schemas["Request"]["oneOf"] += [ref("v1_1." + name + ".request") for name in INPUTS_V11]
    return {"openapi": "3.1.0", "info": {"title": "Chombo application boundary", "version": CONTRACT_VERSION,
            "description": "I50-01: Python共通境界。HTTP経路・認証・Swagger UI配信はI50-02以降。未接続操作は503相当で停止。"},
            "paths": {}, "components": {"schemas": schemas, "examples": {
                "OtherSeatDenied": {"summary": "業務拒否は外側SUCCEEDED・内側DENIED", "value": {
                    "contract_id": CONTRACT_ID, "contract_version": CONTRACT_VERSION, "request_id": "position-001",
                    "operation": "position.analyze", "status": "SUCCEEDED",
                    "data": {"status": "DENIED", "reason_code": "OTHER_SEAT_DURING_ACTIVE_PLAY"}, "error": None,
                    "meta": {"engine_version": None, "rule_ref": None, "applied_concept_ids": [], "position_ref": None}}},
                "CapabilityUnavailable": {"summary": "未接続操作", "value": {
                    "contract_id": CONTRACT_ID, "contract_version": CONTRACT_VERSION, "request_id": "start-001",
                    "operation": "session.start", "status": "REJECTED", "data": None,
                    "error": {"code": "CAPABILITY_UNAVAILABLE", "source_code": None, "retryable": False, "details": {}},
                    "meta": {"engine_version": None, "rule_ref": None, "applied_concept_ids": [], "position_ref": None}}}}},
            "x-application-capabilities": capabilities(),
            "x-examples": {"shape": {"contract_id": CONTRACT_ID, "contract_version": CONTRACT_VERSION,
                           "request_id": "shape-001", "operation": "hand.analyze", "input": {"notation": "123456m789p5567s"}}}}


def result_schemas():
    """Type the existing results without changing the Engine payloads."""
    number = {"type": "integer", "minimum": -1, "maximum": 9007199254740991}
    strings = array(STRING)
    decomposition = obj({"melds": strings, "head": STRING})
    wait = obj({"tile": STRING, "remaining_copies": INTEGER, "complete_forms": strings,
                "wait_types": strings, "decompositions": array(decomposition)})
    ukeire = obj({"tile": STRING, "remaining_copies": INTEGER, "advanced_shanten": number, "advanced_forms": strings},
                 ["tile", "remaining_copies", "advanced_shanten"])
    shape = obj({"input": STRING, "tile_count": INTEGER, "scope": enum("shape_only"),
                 "shanten_by_form": obj({name: number for name in ("hand.complete.standard", "hand.complete.seven_pairs", "hand.complete.thirteen_orphans")}),
                 "overall_shanten": number, "complete_forms": strings, "excluded_judgments": strings,
                 "tenpai": BOOLEAN, "waits": array(wait), "wait_tile_type_count": INTEGER,
                 "total_in_hand_based_wait_copy_count": INTEGER, "ukeire": array(ukeire),
                 "ukeire_tile_type_count": INTEGER, "total_in_hand_based_ukeire_copy_count": INTEGER},
                ["input", "tile_count", "scope", "shanten_by_form", "overall_shanten", "complete_forms", "excluded_judgments"])
    fixed = obj({"input": STRING, "fixed_meld_count": INTEGER, "fixed_tiles": STRING,
                 "scope": enum("shape_only_standard_waits_with_fixed_melds"), "waits": strings,
                 "wait_tile_type_count": INTEGER, "excluded_judgments": strings})
    metadata = {key: nullable(STRING) for key in ("session_id", "lineage_id", "checkpoint_id", "event_id", "observer_seat", "subject_seat")}
    metadata.update({key: STRING for key in ("conversation_ref_id", "source_kind", "record_id", "hand_id", "mode", "observer_kind", "requested_subject", "rule_profile_id", "rule_snapshot_id")})
    metadata["event_sequence"] = INTEGER
    # The existing projection is a versioned observer display model; its internal
    # fields remain owned by observer_projection, not redefined as API authority.
    projection = {"type": "object", "description": "既存observer投影。ゲートで開示範囲を限定する。"}
    stopped = obj({"status": enum("DENIED", "CLARIFICATION_REQUIRED", "POSITION_UNAVAILABLE", "REFERENCE_INVALID"),
                   "reason_code": STRING, **metadata}, ["status", "reason_code"])
    common = {"status": enum("ALLOWED"), **metadata, "position_input_digest": STRING}
    snapshot = obj({**common, "visible_seat": nullable(STRING), "state": projection})
    fixed_shape = obj({"overall_shanten": number, "ukeire": array(ukeire), "total_in_hand_based_ukeire_copy_count": INTEGER})
    answer = obj({**common, "subject": STRING, "engine_results": obj({"shape": {"anyOf": [shape, fixed_shape]}}, []),
                  "explanation": STRING, "public_state": projection, "applied_concept_ids": strings},
                 list(common) + ["subject", "engine_results", "applied_concept_ids"])
    error = obj({"code": STRING, "source_code": nullable(STRING), "retryable": BOOLEAN, "details": obj({})})
    meta = obj({"engine_version": nullable(STRING), "rule_ref": nullable(obj({"kind": enum("MATCH"), "profile_id": STRING, "snapshot_id": STRING})),
                "applied_concept_ids": strings, "position_ref": nullable(obj({**metadata, "projection_digest": STRING, "expires_at": STRING}))})
    result = {"hand.analyze.result": {"anyOf": [shape, fixed]},
              "position.snapshot.result": {"anyOf": [snapshot, stopped]}, "position.analyze.result": {"anyOf": [answer, stopped]}}
    base = {"contract_id": {"const": CONTRACT_ID}, "contract_version": {"const": CONTRACT_VERSION},
            "request_id": nullable(STRING), "operation": nullable(STRING), "meta": meta}
    result["Response"] = {"oneOf": [
        obj({**base, "operation": {"const": name}, "status": enum("SUCCEEDED"), "data": ref(name + ".result"), "error": NULL})
        for name in sorted(CONNECTED)] + [obj({**base, "status": enum("REJECTED", "FAILED"), "data": NULL, "error": error})]}
    variants = deepcopy(result["Response"]["oneOf"])
    for variant in variants:
        variant["properties"]["contract_version"] = {"const": "1.1.0"}
    result["Response"]["oneOf"] += variants
    return result
