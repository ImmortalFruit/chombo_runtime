"""Structured, explicit condition inputs shared by the local UI and callers.

This adapter builds existing v0.4 documents; it does not calculate rules or
promote a single-match profile into an aggregate competition profile.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from engine.condition_documents import (
    FORMAT_VERSION, INTEGRITY_ALGORITHM, SCHEMA_IDS, canonical_bytes,
    document_hash, load_condition_document,
)
from engine.condition_skill import calculate_condition_documents, qualified_concept_ids
from engine.navigator import NavigatorError
from engine.rule_editor import rule_management_model
from engine.rule_inputs import RuleSelection, load_selected_profile


CONCEPT_IDS = (
    "game.condition.single_match_rank_condition",
    "game.condition.reachability", "game.condition.minimal_condition",
    "game.condition.candidate_dominance",
)
AGGREGATE_CONCEPT_IDS = (
    "game.condition.aggregate_rank_condition",
    *(f"game.condition.aggregate_profile.{name}" for name in (
        "identity", "value_and_addition", "comparison_and_tie", "rounding",
        "penalty_boundary", "application_order",
    )),
)
AGGREGATE_REASON = "AGGREGATE_PROFILE_NOT_REGISTERED"


def form_options(store):
    model = rule_management_model(store)
    items = [item for item in model["official"]
             if item["execution_support"]["match"]["status"] == "SUPPORTED"]
    items += model["confirmed"]
    return {"rules": [{"name": item["name"], "ref": item["ref"],
                       "points_per_player": item["points_per_player"],
                       "sudden_death": bool(load_selected_profile(RuleSelection(reference=item["ref"],
                           store_directory=store.directory if item["ref"]["kind"] == "CUSTOM_CONFIRMED" else None),
                           route="match").value("match.end.extension").get("sudden_death", False))} for item in items],
            "aggregate_status": "RULES_INCOMPLETE", "aggregate_reason": AGGREGATE_REASON}


def _hash(value):
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _seal(document):
    document["integrity"] = {"algorithm": INTEGRITY_ALGORITHM,
                             "document_hash": "sha256:" + "0" * 64}
    document["integrity"]["document_hash"] = document_hash(document)
    return document


def prepare_state(inputs, profile, *, producer):
    """Validate explicit participants/counters and build the existing state document."""
    players = inputs["players"]
    if (len(players) != 4 or {p["participant_id"] for p in players} != {"p0", "p1", "p2", "p3"}
            or {p["seat"] for p in players} != {"east", "south", "west", "north"}):
        raise NavigatorError("INVALID_STATE")
    allowed = inputs["allowed_result_kinds"]
    if not allowed or len(allowed) != len(set(allowed)):
        raise NavigatorError("INVALID_REQUEST")
    # Fixed participant IDs remain distinct from the seat mapping.
    players = sorted(players, key=lambda p: p["participant_id"])
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    def envelope(kind):
        return {"document_kind": kind, "schema_id": SCHEMA_IDS[kind],
                "format_version": FORMAT_VERSION, "created_at": created_at,
                "producer": dict(producer)}
    source_hash = _hash(inputs)
    state = _seal({
        **envelope("CONDITION_STATE"), "state_id": "manual:" + source_hash[7:],
        "position": {"match_id": "manual-condition", "hand_id": "manual-hand",
                     "event_id": "manual-input", "event_sequence": 0, "state_digest": source_hash},
        "participants": [{"participant_id": p["participant_id"], "seat": p["seat"].upper()} for p in players],
        "scores": [{"participant_id": p["participant_id"], "value": p["score"]} for p in players],
        "round_state": {"round_wind": inputs["round_wind"].upper(),
                        "hand_number": inputs["hand_number"],
                        "dealer_participant_id": inputs["dealer_participant_id"],
                        "honba": inputs["honba"], "riichi_deposits": inputs["riichi_deposits"],
                        "match_completion_state": "IN_PROGRESS",
                        "initial_score_total": profile.value("match.start.points")["points_per_player"] * 4},
    })
    load_condition_document(json.dumps(state))
    return state


def prepare_query(inputs, profile, *, producer, request_id):
    """Normalize rank inputs without manufacturing a point target."""
    state = prepare_state(inputs, profile, producer=producer)
    players = inputs["players"]
    allowed = inputs["allowed_result_kinds"]
    source_hash = state["position"]["state_digest"]
    def envelope(kind):
        return {"document_kind": kind, "schema_id": SCHEMA_IDS[kind],
                "format_version": FORMAT_VERSION, "created_at": state["created_at"],
                "producer": dict(producer)}
    if inputs["ranking_scope"] == "AGGREGATE_RANK":
        if any(p["aggregate_points"] is None for p in players):
            raise NavigatorError("INPUT_INCOMPLETE")
        return None
    request = _seal({
        **envelope("CONDITION_REQUEST"), "request_id": str(uuid5(NAMESPACE_URL, request_id)),
        "calculation_basis": "INPUT_CONSTRAINED_COMEBACK", "ranking_scope": inputs["ranking_scope"],
        "target": {"participant_id": inputs["target_participant_id"], "target_rank": inputs["target_rank"],
                   "comeback_tie_policy": inputs["comeback_tie_policy"]},
        "position_source": {"kind": "INLINE_STATE", "state": state},
        "input_constraints": {"evaluation_point": inputs["evaluation_point"], "allowed_result_kinds": allowed,
                              "force_match_end": inputs.get("force_match_end", False)},
        "match_rule_ref": {"profile_id": profile.profile_id, "version": "1.0.0",
                           "snapshot_id": profile.snapshot_id, "content_hash": profile.snapshot_id},
        "concept_refs": list(CONCEPT_IDS) + (["game.condition.explicit_final_hand_cutoff"] if inputs.get("force_match_end") else []),
        "provenance": {"source": "EXPLICIT_STRUCTURED_INPUT", "input_digest": source_hash, "selected_rule_ref": inputs["rule_ref"]},
        "search_scope": {"allowed_result_kinds": allowed, "max_depth": 1,
                         "max_candidates": 10000, "max_transitions": 4096, "max_wall_time_ms": 60000},
    })
    load_condition_document(json.dumps(request))
    return request


def selected_profile(inputs, store):
    selected = next((item for item in form_options(store)["rules"] if item["ref"] == inputs["rule_ref"]), None)
    if selected is None:
        raise NavigatorError("REVIEW_REQUIRED")
    ref = selected["ref"]
    profile = load_selected_profile(RuleSelection(reference=ref,
        store_directory=store.directory if ref["kind"] == "CUSTOM_CONFIRMED" else None), route="match")
    return profile


def query(inputs, *, store, concepts_dir, producer, request_id):
    profile = selected_profile(inputs, store)
    request = prepare_query(inputs, profile, producer=producer, request_id=request_id)
    qualified = qualified_concept_ids(concepts_dir)
    required = set(CONCEPT_IDS)
    if inputs.get("force_match_end"):
        required.add("game.condition.explicit_final_hand_cutoff")
    if inputs["ranking_scope"] == "AGGREGATE_RANK":
        required.update(AGGREGATE_CONCEPT_IDS)
    if not required <= qualified:
        return {"status": "KNOWLEDGE_UNQUALIFIED", "reason_code": "UNQUALIFIED_CONCEPT", "response": None}
    if request is None:
        return {"status": "RULES_INCOMPLETE", "reason_code": AGGREGATE_REASON, "response": None}
    response = dict(calculate_condition_documents(request, profile, qualified_concepts=qualified))
    # Keep presentation additions outside the sealed Engine response and candidate IDs.
    starting = {p["participant_id"]: p["aggregate_points"] for p in inputs["players"]}
    display_points = {}
    for candidate in response.get("candidates", response.get("partial_candidates", ())):
        final = candidate["evidence"].get("final_points") or candidate["evidence"].get("provisional_points", {})
        if final:
            display_points[candidate["candidate_id"]] = {
                participant: {
                    "before": value,
                    "addition": final[participant]["final_competition_points"],
                    "after": str(Decimal(value) + Decimal(final[participant]["final_competition_points"])),
                }
                for participant, value in starting.items() if value is not None and participant in final
            }
    return {"status": response["status"], "reason_code": response.get("limit_reason"),
            "response": response, "display_points": display_points}
