"""Deterministic score and payment calculation for qualified scoring rules."""

from __future__ import annotations

from engine.knowledge_store import KnowledgeStore

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


PROFILE_PATH = Path(__file__).with_name("profiles") / "chombo_qualified_snapshot_v1.json"


def load_profile(profile_id: str, *, path: Path = PROFILE_PATH) -> dict[str, Any]:
    """Load one immutable profile from the qualified-Concept snapshot."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    try:
        profile = snapshot["profiles"][profile_id]
    except KeyError as exc:
        raise ValueError(f"unsupported scoring profile: {profile_id}") from exc
    return {"snapshot_id": snapshot["snapshot_id"], "profile_id": profile_id, **profile}


@lru_cache(maxsize=None)
def _load_default_profile(profile_id: str) -> dict[str, Any]:
    """Load one bundled immutable profile once for the process lifetime."""
    return load_profile(profile_id)


def validate_profile_snapshot(*, path: Path = PROFILE_PATH, repository_root: Path | None = None) -> dict[str, Any]:
    """Verify that every Concept source recorded by the snapshot is still qualified."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    root = repository_root or Path(__file__).resolve().parents[1]
    invalid = []
    for source in snapshot["source_concepts"]:
        payload = KnowledgeStore(root).read_json(source["path"])
        if payload.get("status") != source["status"] or source["status"] != "QUALIFIED":
            invalid.append(source["path"])
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "valid": not invalid,
        "invalid_sources": invalid,
        "source_count": len(snapshot["source_concepts"]),
    }


@lru_cache(maxsize=1)
def validate_default_profile_snapshot() -> dict[str, Any]:
    """Validate the bundled immutable snapshot once for runtime scoring."""
    return validate_profile_snapshot()


def _round_up(value: int, unit: int = 100) -> int:
    return ((value + unit - 1) // unit) * unit


def _basic_points(fu: int, han: int, yakuman_units: int, profile: Mapping[str, Any]) -> tuple[int, int | None, str]:
    if profile.get("scoring_mode") == "aotenjo":
        uncapped = fu * (2 ** (han + 2))
        return uncapped, uncapped, "aotenjo"
    if yakuman_units:
        units = min(yakuman_units, profile.get("maximum_multiple_yakuman", yakuman_units))
        return 8000 * units, None, "yakuman" if units == 1 else "multiple_yakuman"
    if han >= 13:
        treatment = profile["ordinary_13_plus"]
        if treatment == "sanbaiman_cap":
            return 6000, None, "sanbaiman"
        if treatment in {"counted_yakuman", "counted_yakuman_or_four_times_mangan"}:
            return 8000, None, "counted_yakuman"
        if treatment in {"yonbaiman", "four_baiman"}:
            return 8000, None, treatment
        raise ValueError("selected profile does not qualify ordinary scoring at 13 or more han")
    if han >= 11:
        treatment = profile.get("ordinary_11_to_12", "sanbaiman")
        if treatment == "baiman":
            return 4000, None, "baiman"
        if treatment == "sanbaiman":
            return 6000, None, "sanbaiman"
        raise ValueError("selected profile does not qualify ordinary scoring at 11 or 12 han")
    if han >= 8:
        return 4000, None, "baiman"
    if han >= 6:
        return 3000, None, "haneman"
    uncapped = fu * (2 ** (han + 2))
    if han == 5 or uncapped >= 2000 or [fu, han] in profile["kiriage_mangan"]:
        return 2000, uncapped, "mangan"
    return uncapped, uncapped, "normal"


def calculate_score(
    *,
    fu: int,
    han: int,
    method: str,
    dealer: bool,
    profile_id: str,
    honba_count: int = 0,
    yakuman_units: int = 0,
    profile_override: Mapping[str, Any] | None = None,
    snapshot_id_override: str | None = None,
) -> dict[str, Any]:
    """Calculate one already-qualified candidate's score and payer shares.

    Yaku evaluation and fu-component evaluation belong to preceding Engine
    boundaries. This function consumes their final numeric results.
    """
    if method not in {"ron", "tsumo"}:
        raise ValueError("method must be ron or tsumo")
    for name, value in {"fu": fu, "han": han, "honba_count": honba_count, "yakuman_units": yakuman_units}.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if yakuman_units == 0 and (fu == 0 or han == 0):
        raise ValueError("ordinary scoring requires positive fu and han")

    profile = dict(profile_override) if profile_override is not None else _load_default_profile(profile_id)
    if profile.get("scoring_mode") == "aotenjo":
        qualification = validate_default_profile_snapshot()
        if not qualification["valid"]:
            raise ValueError("UNQUALIFIED_PROFILE_SNAPSHOT")
        if yakuman_units:
            raise ValueError("aotenjo does not support specific yakuman conversion")
        if honba_count:
            raise ValueError("aotenjo does not support honba")
    basic, uncapped, score_class = _basic_points(fu, han, yakuman_units, profile)
    rounding_unit = profile.get("payment_rounding_unit", 100)
    honba_ron = profile.get("honba_ron", 300)
    honba_tsumo = profile.get("honba_tsumo_per_non_winner", 100)
    base_payments: dict[str, Any]
    honba_payments: dict[str, Any]
    if method == "ron":
        base_payments = {"discarder": _round_up(basic * (6 if dealer else 4), rounding_unit)}
        honba_payments = {"discarder": honba_count * honba_ron}
    elif dealer:
        base_payments = {"each_non_winner": _round_up(basic * 2, rounding_unit), "payer_count": 3}
        honba_payments = {"each_non_winner": honba_count * honba_tsumo, "payer_count": 3}
    else:
        base_payments = {
            "dealer": _round_up(basic * 2, rounding_unit),
            "each_non_dealer": _round_up(basic, rounding_unit),
            "non_dealer_payer_count": 2,
        }
        honba_payments = {
            "dealer": honba_count * honba_tsumo,
            "each_non_dealer": honba_count * honba_tsumo,
            "non_dealer_payer_count": 2,
        }

    def receipt(payments: dict[str, Any]) -> int:
        if "discarder" in payments:
            return payments["discarder"]
        if "each_non_winner" in payments:
            return payments["each_non_winner"] * payments["payer_count"]
        return payments["dealer"] + payments["each_non_dealer"] * payments["non_dealer_payer_count"]

    total_payments = {
        key: base_payments.get(key, 0) + honba_payments.get(key, 0)
        for key in base_payments
        if not key.endswith("count")
    }
    for key in ("payer_count", "non_dealer_payer_count"):
        if key in base_payments:
            total_payments[key] = base_payments[key]
    base_receipt = receipt(base_payments)
    honba_receipt = receipt(honba_payments)
    return {
        "judgment_status": "SCORED",
        "profile": {"snapshot_id": snapshot_id_override or profile.get("snapshot_id"), "profile_id": profile_id},
        "input": {
            "fu": fu,
            "han": han,
            "method": method,
            "dealer": dealer,
            "honba_count": honba_count,
            "yakuman_units": yakuman_units,
        },
        "score_class": score_class,
        "formula_exponent": han + 2 if score_class == "aotenjo" else (None if yakuman_units or han >= 5 else han + 2),
        "uncapped_basic_points": uncapped,
        "basic_points": basic,
        "base_payments": base_payments,
        "honba_payments": honba_payments,
        "total_payments": total_payments,
        "winner_base_receipt": base_receipt,
        "winner_honba_receipt": honba_receipt,
        "winner_total_receipt": base_receipt + honba_receipt,
        "excluded_judgments": ["yaku_evaluation", "fu_components", "deposits", "dealer_continuation", "next_hand_transition"],
    }


def select_highest_score(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Score public numeric candidates, excluding trusted internal overrides."""
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("at least one scoring candidate is required")
    required = {"fu", "han", "method", "dealer", "profile_id"}
    allowed = required | {"candidate_id", "honba_count", "yakuman_units"}
    validated = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each scoring candidate must be an object")
        unknown = candidate.keys() - allowed
        missing = required - candidate.keys()
        if unknown:
            raise ValueError("unsupported scoring candidate fields: " + ", ".join(sorted(map(str, unknown))))
        if missing:
            raise ValueError("missing scoring candidate fields: " + ", ".join(sorted(missing)))
        if not isinstance(candidate["dealer"], bool):
            raise ValueError("candidate dealer must be a boolean")
        if not isinstance(candidate["method"], str) or candidate["method"] not in {"ron", "tsumo"}:
            raise ValueError("candidate method must be ron or tsumo")
        if not isinstance(candidate["profile_id"], str) or not candidate["profile_id"].strip():
            raise ValueError("candidate profile_id must be a non-empty string")
        for name in ("fu", "han", "honba_count", "yakuman_units"):
            value = candidate.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"candidate {name} must be a non-negative integer")
        validated.append(dict(candidate))
    scored = []
    for index, candidate in enumerate(validated):
        candidate_id = candidate.get("candidate_id", str(index))
        inputs = {key: value for key, value in candidate.items() if key != "candidate_id"}
        scored.append({"candidate_id": candidate_id, "score": calculate_score(**inputs)})
    highest = max(item["score"]["winner_total_receipt"] for item in scored)
    selected = [item["candidate_id"] for item in scored if item["score"]["winner_total_receipt"] == highest]
    return {
        "judgment_status": "SCORED",
        "candidates": scored,
        "selected_candidate_ids": selected,
        "highest_winner_receipt": highest,
        "selection_rule": "highest_winner_receipt_with_stable_ties",
    }
