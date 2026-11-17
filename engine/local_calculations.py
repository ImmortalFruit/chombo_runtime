"""Shared deterministic calculations for agents and observer-scoped displays."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import json
from typing import Any

from engine.hand_completion import analyze_hand, standard_ukeire_with_fixed_melds


CALCULATOR_VERSION = "chombo.local-calculations.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=8192)
def _shape_analysis_cached(
    calculator_version: str,
    rule_snapshot_id: str,
    normalized_notation: str,
    fixed_meld_count: int,
    normalized_fixed_tiles: str,
) -> str:
    """Cache a qualified shape calculation with an explicit version/rule identity."""
    if calculator_version != CALCULATOR_VERSION:
        raise ValueError("unsupported calculator version")
    if fixed_meld_count:
        result = standard_ukeire_with_fixed_melds(
            normalized_notation, fixed_meld_count, normalized_fixed_tiles
        )
    else:
        result = analyze_hand(normalized_notation)
    return _canonical(result)


def analyze_shape(
    notation: str,
    *,
    rule_snapshot_id: str,
    fixed_meld_count: int = 0,
    fixed_tiles: str = "",
) -> dict[str, Any]:
    """Return an isolated result from the shared, snapshot-separated shape cache."""
    if not rule_snapshot_id:
        raise ValueError("rule_snapshot_id is required")
    normalized = notation.replace("0", "5")
    normalized_fixed = fixed_tiles.replace("0", "5")
    return json.loads(
        _shape_analysis_cached(
            CALCULATOR_VERSION,
            rule_snapshot_id,
            normalized,
            fixed_meld_count,
            normalized_fixed,
        )
    )


@lru_cache(maxsize=4096)
def _conversation_shape_cached(
    calculator_version: str,
    rule_profile_id: str,
    rule_snapshot_id: str,
    mode: str,
    observer_kind: str,
    observer_seat: str,
    subject_seat: str,
    input_digest: str,
    notation: str,
    fixed_meld_count: int,
    fixed_tiles: str,
) -> str:
    """Keep conversation results apart across every authority boundary."""
    if calculator_version != CALCULATOR_VERSION:
        raise ValueError("unsupported calculator version")
    return _canonical(analyze_shape(notation, rule_snapshot_id=rule_snapshot_id,
                                    fixed_meld_count=fixed_meld_count, fixed_tiles=fixed_tiles))


def conversation_shape_analysis(notation: str, *, rule_profile_id: str,
                                rule_snapshot_id: str, mode: str,
                                observer_kind: str, observer_seat: str | None,
                                subject_seat: str, input_digest: str,
                                fixed_meld_count: int = 0,
                                fixed_tiles: str = "") -> dict[str, Any]:
    """Calculate one permitted hand with identity-complete cache separation."""
    if not all((rule_profile_id, rule_snapshot_id, mode, observer_kind, subject_seat, input_digest)):
        raise ValueError("complete conversation calculation identity is required")
    return json.loads(_conversation_shape_cached(
        CALCULATOR_VERSION, rule_profile_id, rule_snapshot_id, mode,
        observer_kind, observer_seat or "", subject_seat, input_digest,
        notation, fixed_meld_count, fixed_tiles,
    ))


@lru_cache(maxsize=4096)
def _observer_result_cached(
    calculator_version: str,
    rule_profile_id: str,
    rule_snapshot_id: str,
    observer_seat: str,
    calculation: str,
    input_json: str,
) -> str:
    """Cache display calculations without allowing identities to cross boundaries."""
    if calculator_version != CALCULATOR_VERSION:
        raise ValueError("unsupported calculator version")
    if calculation != "visible_waits":
        raise ValueError("unsupported observer calculation")
    from engine.visible_waits import calculate_visible_waits

    inputs = json.loads(input_json)
    result = calculate_visible_waits(
        inputs["public_state"], inputs["authoritative_state"], observer_seat,
        rule_profile_id=rule_profile_id, rule_snapshot_id=rule_snapshot_id,
    )
    return _canonical(result)


def observer_visible_waits(
    public_state: Mapping[str, Any],
    authoritative_state: Mapping[str, Any],
    seat: str,
    *,
    observer_seat: str,
    rule_profile_id: str,
    rule_snapshot_id: str,
) -> list[dict[str, Any]]:
    """Compute waits only when the requested hand belongs to the observer."""
    if seat != observer_seat:
        raise ValueError("observer may calculate only its permitted seat")
    if not rule_profile_id or not rule_snapshot_id:
        raise ValueError("complete rule identity is required")
    payload = {
        "public_state": deepcopy(dict(public_state)),
        "authoritative_state": deepcopy(dict(authoritative_state)),
    }
    return json.loads(
        _observer_result_cached(
            CALCULATOR_VERSION,
            rule_profile_id,
            rule_snapshot_id,
            observer_seat,
            "visible_waits",
            _canonical(payload),
        )
    )


def cache_clear() -> None:
    """Clear shared calculation caches for deterministic tests and diagnostics."""
    _shape_analysis_cached.cache_clear()
    _observer_result_cached.cache_clear()
    _conversation_shape_cached.cache_clear()


def cache_info() -> dict[str, Any]:
    return {
        "calculator_version": CALCULATOR_VERSION,
        "shape": _shape_analysis_cached.cache_info()._asdict(),
        "observer": _observer_result_cached.cache_info()._asdict(),
        "conversation": _conversation_shape_cached.cache_info()._asdict(),
    }
