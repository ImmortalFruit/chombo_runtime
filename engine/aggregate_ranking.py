"""Exact participant-level aggregate ranking for E40-04."""

from __future__ import annotations

from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
)
from typing import Any, Mapping


class AggregateRankingError(ValueError):
    """A structured refusal to infer an incomplete aggregate rule."""

    def __init__(self, code: str, pointer: str, reason: str):
        self.code = code
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"{code} at {pointer}: {reason}")


_ROUNDING = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "FLOOR": ROUND_FLOOR,
    "CEILING": ROUND_CEILING,
    "TRUNCATE_TOWARD_ZERO": ROUND_DOWN,
}


def _fail(pointer: str, reason: str) -> None:
    raise AggregateRankingError("RULES_INCOMPLETE", pointer, reason)


def _quantize(value: Decimal, scale: int, mode: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-scale), rounding=_ROUNDING[mode])


def _adjustments(
    profile: Mapping[str, Any], participant_ids: set[str],
) -> tuple[str | None, dict[str, int]]:
    penalty = profile["penalty_boundary"]
    if penalty["mode"] == "EXCLUDED":
        return None, {participant: 0 for participant in participant_ids}
    values = penalty.get("participant_adjustments")
    if not isinstance(values, (list, tuple)):
        _fail("/penalty_boundary/participant_adjustments", "INVALID_LEDGER")
    try:
        mapped = {item["participant_id"]: item["value"] for item in values}
    except (KeyError, TypeError):
        _fail("/penalty_boundary/participant_adjustments", "INVALID_LEDGER")
    if set(mapped) != participant_ids or any(type(value) is not int for value in mapped.values()):
        _fail("/penalty_boundary/participant_adjustments", "PARTICIPANT_SET_OR_VALUE_MISMATCH")
    return penalty["application_stage"], mapped


def rank_aggregate_values(
    *, pre_values: Mapping[str, int], final_points: Mapping[str, Decimal | int | float | str],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Add one completed match exactly once and rank with the independent profile."""

    participant_ids = set(pre_values)
    if len(participant_ids) != 4 or set(final_points) != participant_ids:
        _fail("/aggregate_context/values", "PARTICIPANT_SET_MISMATCH")
    if any(type(value) is not int for value in pre_values.values()):
        _fail("/aggregate_context/values", "INTEGER_MINOR_UNITS_REQUIRED")
    representation = profile["value_representation"]
    scale = 0 if representation["kind"] == "INTEGER" else representation["scale"]
    unit = Decimal(10) ** scale
    before = {participant: Decimal(value) / unit for participant, value in pre_values.items()}
    additions = {participant: Decimal(str(value)) for participant, value in final_points.items()}
    stage, adjustments_minor = _adjustments(profile, participant_ids)
    adjustments = {participant: Decimal(value) / unit for participant, value in adjustments_minor.items()}
    rounding = profile["rounding"]

    if rounding["stage"] == "EACH_ADDITION":
        additions = {
            participant: _quantize(value, rounding["output_scale"], rounding["mode"])
            for participant, value in additions.items()
        }
    working = {
        participant: before[participant]
        + (adjustments[participant] if stage == "BEFORE_MATCH_ADDITION" else Decimal(0))
        + additions[participant]
        for participant in participant_ids
    }
    if stage == "AFTER_MATCH_ADDITION_BEFORE_RANKING":
        working = {participant: value + adjustments[participant] for participant, value in working.items()}
    if rounding["stage"] == "AFTER_AGGREGATION":
        working = {
            participant: _quantize(value, rounding["output_scale"], rounding["mode"])
            for participant, value in working.items()
        }
    comparison = dict(working)
    display = dict(working)
    if rounding["stage"] == "DISPLAY_ONLY":
        display = {
            participant: _quantize(value, rounding["output_scale"], rounding["mode"])
            for participant, value in working.items()
        }

    reverse = profile["comparison_direction"] == "HIGHER_IS_BETTER"
    value_groups: dict[Decimal, list[str]] = {}
    for participant, value in comparison.items():
        value_groups.setdefault(value, []).append(participant)
    ordered_values = sorted(value_groups, reverse=reverse)
    ranks: dict[str, int] = {}
    ordered: list[str] = []
    tie = profile["tie_policy"]
    previous_rank = 0
    for value in ordered_values:
        group = value_groups[value]
        if len(group) > 1 and tie["mode"] == "ORDERED_PRIORITY":
            for criterion in profile["priority_criteria"]:
                if criterion != "PRE_ADDITION_AGGREGATE_RANK":
                    _fail("/priority_criteria", f"{criterion}_VALUES_UNAVAILABLE")
                group.sort(
                    key=lambda participant: (
                        (-before[participant], participant)
                        if reverse else (before[participant], participant)
                    )
                )
                if len({before[participant] for participant in group}) == len(group):
                    break
            else:
                _fail("/priority_criteria", "CRITERIA_DO_NOT_RESOLVE_TIE")
            for participant in group:
                ordered.append(participant)
                ranks[participant] = len(ordered)
            continue
        rank = len(ordered) + 1 if tie.get("rank_numbering") == "COMPETITION" else previous_rank + 1
        previous_rank = rank
        for participant in sorted(group):
            ordered.append(participant)
            ranks[participant] = rank

    def minor(value: Decimal) -> int:
        scaled = value * unit
        if scaled != scaled.to_integral_value():
            _fail("/rounding", "VALUE_NOT_REPRESENTABLE_AT_DECLARED_SCALE")
        return int(scaled)

    return {
        "ordered_participant_ids": tuple(ordered),
        "ranks": ranks,
        "components": {
            participant: {
                "pre_addition_value": pre_values[participant],
                "single_match_final_points": str(additions[participant]),
                "penalty_adjustment": adjustments_minor[participant],
                "comparison_value": minor(comparison[participant]),
                "display_value": str(display[participant]),
            }
            for participant in sorted(participant_ids)
        },
        "comparison_direction": profile["comparison_direction"],
        "tie_policy": dict(tie),
        "priority_criteria": tuple(profile["priority_criteria"]),
    }
