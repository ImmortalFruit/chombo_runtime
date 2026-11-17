"""Deterministic v0.4 semantic-result candidate generation and normalization.

This module owns the E40-02 boundary.  It enumerates semantic result frames,
normalizes forward-verified candidates, and classifies Pareto dominance.  It
does not score a hand, settle points, decide a rank, or claim search
completeness; those remain later Engine responsibilities.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
import hashlib
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from engine.condition_documents import ConditionBundle, canonical_bytes


RESULT_KINDS = (
    "TARGET_RON",
    "TARGET_DEALT_IN",
    "TARGET_TSUMO",
    "OTHER_WIN",
    "DRAW",
)
QUANTITY_AXES = (
    "required_realizable_winning_score",
    "required_target_score_gain_after_honba_and_deposit",
    "required_opponent_score_loss_after_honba",
)
SIGNATURE_FIELDS = (
    "calculation_basis",
    "ranking_scope",
    "evaluation_point",
    "target_participant_id",
    "target_rank",
    "semantic_result_type",
    "winner_participant_id",
    "payer_participant_ids",
    "rule_profile_refs",
    "fixed_constraint_set",
)


class CandidateError(ValueError):
    """A structured E40-02 contract rejection."""

    def __init__(self, code: str, pointer: str, reason: str):
        self.code = code
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"{code} at {pointer}: {reason}")


def _fail(code: str, pointer: str, reason: str) -> None:
    raise CandidateError(code, pointer, reason)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _participant_order(bundle: ConditionBundle) -> tuple[str, ...]:
    seats = {"EAST": 0, "SOUTH": 1, "WEST": 2, "NORTH": 3}
    participants = tuple(bundle.state["participants"])
    try:
        ordered = sorted(participants, key=lambda item: seats[item["seat"]])
    except (KeyError, TypeError) as error:
        raise CandidateError("INVALID_STATE", "/participants", "INVALID_SEAT") from error
    return tuple(item["participant_id"] for item in ordered)


def _allowed_result_kinds(request: Mapping[str, Any]) -> tuple[str, ...]:
    requested = tuple(request["search_scope"]["allowed_result_kinds"])
    constraints = request.get("input_constraints")
    if constraints is not None:
        constrained = tuple(constraints["allowed_result_kinds"])
        unknown = set(constrained) - set(RESULT_KINDS)
        if unknown:
            _fail("INVALID_REQUEST", "/input_constraints/allowed_result_kinds", "UNKNOWN_RESULT_KIND")
        requested = tuple(kind for kind in requested if kind in set(constrained))
    return tuple(kind for kind in RESULT_KINDS if kind in set(requested))


def _evaluation_point(request: Mapping[str, Any]) -> str:
    if request["calculation_basis"] == "CURRENT_HAND_COMEBACK":
        return request["position_source"]["evaluation_point"]
    return request["input_constraints"]["evaluation_point"]


def _fixed_constraints(request: Mapping[str, Any]) -> dict[str, Any]:
    constraints = deepcopy(dict(request.get("input_constraints", {})))
    constraints.pop("allowed_result_kinds", None)
    return constraints


def _signature(
    bundle: ConditionBundle, result_kind: str, winner: str | None,
    payers: Iterable[str],
) -> dict[str, Any]:
    request = bundle.request
    signature = {
        "calculation_basis": request["calculation_basis"],
        "ranking_scope": request["ranking_scope"],
        "evaluation_point": _evaluation_point(request),
        "target_participant_id": request["target"]["participant_id"],
        "semantic_result_type": result_kind,
        "winner_participant_id": winner,
        "payer_participant_ids": list(payers),
        "rule_profile_refs": [deepcopy(dict(request["match_rule_ref"]))],
        "fixed_constraint_set": _fixed_constraints(request),
    }
    if request["ranking_scope"] == "AGGREGATE_POINTS":
        signature.update({key: request["target"][key] for key in ("target_points", "comparison")})
    else:
        signature["target_rank"] = request["target"]["target_rank"]
    if request["ranking_scope"] == "AGGREGATE_RANK":
        signature["rule_profile_refs"].append(
            deepcopy(dict(request["aggregate_context"]["profile_ref"]))
        )
    return signature


def _frame(bundle: ConditionBundle, result_kind: str, semantic_input: dict[str, Any]) -> dict[str, Any]:
    winner = semantic_input.get("winner_participant_id")
    payers = semantic_input.get("payer_participant_ids", ())
    signature = _signature(bundle, result_kind, winner, payers)
    identity = {"result_kind": result_kind, "semantic_input": semantic_input, "comparability_signature": signature}
    return {
        "candidate_id": "candidate:" + _canonical_hash(identity).split(":", 1)[1],
        **identity,
    }


def generate_semantic_candidates(bundle: ConditionBundle) -> tuple[Mapping[str, Any], ...]:
    """Enumerate every allowed participant-level result frame in stable order.

    Draw candidates cover all sixteen declared-tenpai subsets.  Win frames are
    scoring queries rather than invented scores; E40-03 must feed qualified
    Engine results back through :func:`normalize_verified_candidates`.
    """
    participants = _participant_order(bundle)
    target = bundle.request["target"]["participant_id"]
    if target not in participants:
        _fail("INVALID_REQUEST", "/target/participant_id", "UNKNOWN_PARTICIPANT")
    others = tuple(item for item in participants if item != target)
    frames: list[dict[str, Any]] = []
    for result_kind in _allowed_result_kinds(bundle.request):
        if result_kind == "TARGET_RON":
            for payer in others:
                frames.append(_frame(bundle, result_kind, {
                    "method": "ron", "winner_participant_id": target,
                    "payer_participant_ids": [payer],
                }))
        elif result_kind == "TARGET_DEALT_IN":
            for winner in others:
                frames.append(_frame(bundle, result_kind, {
                    "method": "ron", "winner_participant_id": winner,
                    "payer_participant_ids": [target],
                }))
        elif result_kind == "TARGET_TSUMO":
            frames.append(_frame(bundle, result_kind, {
                "method": "tsumo", "winner_participant_id": target,
                "payer_participant_ids": list(others),
            }))
        elif result_kind == "OTHER_WIN":
            for winner in others:
                for payer in others:
                    if payer != winner:
                        frames.append(_frame(bundle, result_kind, {
                            "method": "ron", "winner_participant_id": winner,
                            "payer_participant_ids": [payer],
                        }))
        else:
            for count in range(len(participants) + 1):
                for tenpai in combinations(participants, count):
                    frames.append(_frame(bundle, result_kind, {
                        "method": "draw", "winner_participant_id": None,
                        "payer_participant_ids": [],
                        "declared_tenpai_participant_ids": list(tenpai),
                    }))
    limit = bundle.request["search_scope"]["max_candidates"]
    if len(frames) > limit:
        _fail("INDETERMINATE_LIMIT", "/search_scope/max_candidates", "FRAME_LIMIT_REACHED")
    return tuple(_freeze(frame) for frame in frames)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def normalize_verified_candidates(
    frames: Iterable[Mapping[str, Any]], candidates: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Validate, deduplicate, and canonically order forward-verified results."""
    frame_by_id = {frame["candidate_id"]: _plain(frame) for frame in frames}
    normalized: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(candidates):
        pointer = f"/candidates/{index}"
        frame_id = raw.get("frame_id")
        if frame_id not in frame_by_id:
            _fail("INVALID_REQUEST", pointer + "/frame_id", "UNKNOWN_FRAME")
        if raw.get("forward_verified") is not True:
            _fail("INVALID_STATE", pointer + "/forward_verified", "FORWARD_VERIFICATION_REQUIRED")
        axes = raw.get("quantity_axes")
        if not isinstance(axes, Mapping) or set(axes) != set(QUANTITY_AXES):
            _fail("INVALID_STATE", pointer + "/quantity_axes", "COMPLETE_AXES_REQUIRED")
        if any(type(axes[name]) is not int or axes[name] < 0 for name in QUANTITY_AXES):
            _fail("INVALID_STATE", pointer + "/quantity_axes", "NORMALIZED_NONNEGATIVE_INTEGER_REQUIRED")
        engine_refs = raw.get("engine_result_refs")
        if not isinstance(engine_refs, list) or not engine_refs or any(not isinstance(item, str) or not item for item in engine_refs):
            _fail("INVALID_STATE", pointer + "/engine_result_refs", "FORWARD_TRACE_REQUIRED")
        item = {
            **frame_by_id[frame_id],
            "forward_verified": True,
            "target_satisfied": raw.get("target_satisfied") is True,
            "quantity_axes": {name: axes[name] for name in QUANTITY_AXES},
            "engine_result_refs": list(engine_refs),
        }
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, Mapping):
            _fail("INVALID_STATE", pointer + "/evidence", "EXPECTED_OBJECT")
        item["evidence"] = _plain(evidence)
        identity = {key: item[key] for key in (
            "result_kind", "semantic_input", "comparability_signature",
            "quantity_axes", "engine_result_refs", "target_satisfied", "evidence",
        )}
        item["candidate_id"] = "candidate:" + _canonical_hash(identity).split(":", 1)[1]
        normalized[_canonical_hash(identity)] = item
    ordered = sorted(normalized.values(), key=lambda item: (
        _canonical_hash(item["comparability_signature"]),
        tuple(item["quantity_axes"][axis] for axis in QUANTITY_AXES),
        item["candidate_id"],
    ))
    return tuple(_freeze(item) for item in ordered)


def classify_dominance(candidates: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Classify Pareto dominance only among comparable successful candidates."""
    items = [_plain(item) for item in candidates]
    for item in items:
        item["dominance"] = {"status": "INCOMPARABLE", "dominated_by": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("forward_verified") is True and item.get("target_satisfied") is True:
            axes = item.get("quantity_axes")
            if isinstance(axes, Mapping) and set(axes) == set(QUANTITY_AXES):
                groups.setdefault(_canonical_hash(item["comparability_signature"]), []).append(item)
    for group in groups.values():
        for candidate in group:
            dominated_by = []
            for other in group:
                if other is candidate:
                    continue
                left = tuple(other["quantity_axes"][axis] for axis in QUANTITY_AXES)
                right = tuple(candidate["quantity_axes"][axis] for axis in QUANTITY_AXES)
                if all(a <= b for a, b in zip(left, right)) and any(a < b for a, b in zip(left, right)):
                    dominated_by.append(other["candidate_id"])
            candidate["dominance"] = {
                "status": "DOMINATED" if dominated_by else "NON_DOMINATED",
                "dominated_by": sorted(dominated_by),
            }
    return tuple(_freeze(item) for item in items)
