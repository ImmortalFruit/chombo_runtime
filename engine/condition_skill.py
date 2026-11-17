"""U40-01 document-to-Engine boundary for condition-calculation skills."""

from __future__ import annotations

from engine.knowledge_store import concept_documents

from dataclasses import replace
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from engine.condition_documents import (
    CALCULATION_STOPS,
    FORMAT_VERSION,
    INTEGRITY_ALGORITHM,
    SCHEMA_IDS,
    ConditionDocumentError,
    canonical_bytes,
    document_hash,
    load_condition_document,
    validate_condition_bundle,
)
from engine.condition_candidates import CandidateError
from engine.condition_search import CALCULATOR_VERSION, calculate_conditions
from engine.condition_single_match import SingleMatchConditionError
from engine.game_state import SEATS, Seat
from engine.hand_log import Participant
from engine.match_state import MatchState, MatchStateValidationError
from engine.rule_profiles import ResolvedRuleProfile
from engine.setup import SetupError, create_initial_state


SKILL_VERSION = "chombo.condition-skill.v0.4.0"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def qualified_concept_ids(concepts_dir: Path) -> frozenset[str]:
    """Collect only explicitly QUALIFIED Concept IDs from repository sources."""
    qualified: set[str] = set()
    for _, document in concept_documents(concepts_dir):
        if document.get("status") != "QUALIFIED":
            continue
        qualified.update(
            item["concept_id"]
            for item in document.get("concepts", ())
            if isinstance(item, dict) and isinstance(item.get("concept_id"), str)
        )
    return frozenset(qualified)


def load_match_profile(profile_path: Path, concepts_dir: Path) -> ResolvedRuleProfile:
    """Load the exact profile snapshot needed by condition projections."""
    from engine.rule_profiles import load_resolved_profile

    return load_resolved_profile(profile_path, concepts_dir)


def match_from_condition_state(
    state: Mapping[str, Any], profile: ResolvedRuleProfile,
) -> MatchState:
    """Build the immutable Engine projection represented by CONDITION_STATE."""
    try:
        by_seat = {
            Seat(item["seat"].lower()): item["participant_id"]
            for item in state["participants"]
        }
        scores_by_participant = {
            item["participant_id"]: item["value"] for item in state["scores"]
        }
        scores = {seat: scores_by_participant[participant] for seat, participant in by_seat.items()}
        round_state = state["round_state"]
        dealer_id = round_state["dealer_participant_id"]
        dealer = next(seat for seat, participant in by_seat.items() if participant == dealer_id)
        round_wind = Seat(round_state["round_wind"].lower())
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        raise ConditionDocumentError(
            "INVALID_STATE", "CONDITION_STATE", "", "CANNOT_PROJECT_MATCH_STATE"
        ) from error

    try:
        setup = create_initial_state(
            f'condition:{state["state_id"]}', profile, dealer=dealer,
            round_wind=round_wind, hand_number=round_state["hand_number"],
        ).state
        hand = replace(
            setup,
            players=tuple(
                replace(player, score=scores[player.seat]) for player in setup.players
            ),
            honba=round_state["honba"],
            riichi_deposits=round_state["riichi_deposits"] * 1000,
        )
        participants = tuple(
            Participant(by_seat[seat], by_seat[seat], "condition-skill")
            for seat in SEATS
        )
        position = state["position"]
        return MatchState(
            match_id=position["match_id"], participants=participants,
            seat_assignments=by_seat, initial_dealer=Seat.EAST, dealer=dealer,
            round_wind=round_wind, hand_number=round_state["hand_number"],
            honba=round_state["honba"],
            riichi_deposits=round_state["riichi_deposits"] * 1000,
            scores=scores, rule_profile_id=profile.profile_id,
            rule_snapshot_id=profile.snapshot_id,
            current_hand_id=position["hand_id"], current_hand=hand,
        )
    except (KeyError, SetupError, MatchStateValidationError) as error:
        raise ConditionDocumentError(
            "INVALID_STATE", "CONDITION_STATE", "/round_state",
            "CANNOT_PROJECT_MATCH_STATE",
        ) from error


def _input_digest(
    request: Mapping[str, Any], state: Mapping[str, Any] | None,
    support_ref: Mapping[str, Any] | None,
    aggregate_profile: Mapping[str, Any] | None,
) -> str:
    value = {
        "request": _plain(request), "state": _plain(state),
        "support_ref": _plain(support_ref),
        "aggregate_profile": _plain(aggregate_profile),
    }
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    document["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "document_hash": "sha256:" + "0" * 64,
    }
    document["integrity"]["document_hash"] = document_hash(document)
    return document


def _response_base(
    request: Mapping[str, Any], *, state: Mapping[str, Any] | None,
    support_ref: Mapping[str, Any] | None,
    aggregate_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evaluation_point = (
        request["position_source"].get("evaluation_point")
        if request["position_source"]["kind"] == "SUPPORT_REF"
        else request["input_constraints"]["evaluation_point"]
    )
    return {
        "document_kind": "CONDITION_RESPONSE",
        "schema_id": SCHEMA_IDS["CONDITION_RESPONSE"],
        "format_version": FORMAT_VERSION,
        "created_at": request["created_at"],
        "producer": {
            "app_id": "chombo-condition-skill",
            "app_version": "0.4.0",
            "build_revision": request["producer"]["build_revision"],
            "engine_version": CALCULATOR_VERSION,
        },
        "integrity": {},
        "request_id": request["request_id"],
        "calculation_basis": request["calculation_basis"],
        "ranking_scope": request["ranking_scope"],
        "evaluation_point": evaluation_point,
        "calculator_version": CALCULATOR_VERSION,
        "input_digest": _input_digest(request, state, support_ref, aggregate_profile),
        "match_rule_ref": _plain(request["match_rule_ref"]),
        "aggregate_profile_ref": (
            _plain(request.get("aggregate_context", {}).get("profile_ref"))
            if request["ranking_scope"] == "AGGREGATE_RANK" else None
        ),
        "concept_refs": list(request["concept_refs"]),
        "evidence": [{"boundary": SKILL_VERSION}],
        "usage": {"depth": 0, "transitions": 0, "candidates": 0},
    }


def calculate_condition_documents(
    request: Mapping[str, Any], profile: ResolvedRuleProfile, *,
    qualified_concepts: frozenset[str], state: Mapping[str, Any] | None = None,
    support_ref: Mapping[str, Any] | None = None,
    aggregate_profile: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Validate documents, invoke the Engine, and return one sealed response."""
    base = _response_base(
        request, state=state, support_ref=support_ref,
        aggregate_profile=aggregate_profile,
    )
    try:
        bundle = validate_condition_bundle(
            request, state=state, support_ref=support_ref,
            aggregate_profile=aggregate_profile,
            qualified_concept_ids=qualified_concepts,
        )
        match = match_from_condition_state(bundle.state, profile)
        result = calculate_conditions(bundle, match, profile)
    except (ConditionDocumentError, SingleMatchConditionError, CandidateError) as error:
        if error.code not in CALCULATION_STOPS:
            raise
        base.update({
            "status": error.code,
            "evidence": [{
                "boundary": SKILL_VERSION,
                "pointer": getattr(error, "pointer", ""),
                "reason": error.reason,
            }],
        })
        return _seal(base)

    base.update({
        "status": result.status,
        "usage": _plain(result.usage),
        "evidence": [{"boundary": SKILL_VERSION, "search_status": result.status}],
    })
    if result.status == "ACHIEVABLE":
        base["candidates"] = _plain(result.candidates)
        base["complete_search_evidence"] = _plain(result.complete_search_evidence)
    elif result.status == "UNREACHABLE":
        base["candidates"] = []
        base["complete_search_evidence"] = _plain(result.complete_search_evidence)
    else:
        base["partial_candidates"] = _plain(result.partial_candidates)
        base["limit_reason"] = result.limit_reason
    return _seal(base)


def load_document_path(path: Path, expected_kind: str) -> Mapping[str, Any]:
    document = load_condition_document(path.read_bytes())
    if document["document_kind"] != expected_kind:
        raise ConditionDocumentError(
            "UNSUPPORTED_SCHEMA", document["document_kind"], "",
            f"EXPECTED_{expected_kind}",
        )
    return document
