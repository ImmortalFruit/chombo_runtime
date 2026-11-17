"""U40-04 Navigator presentation boundary for rank-condition calculation."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping


DISPLAY_STATUSES = {
    "CALCULATING": ("計算中", "固定した局面を検証しています。"),
    "ACHIEVABLE": ("達成可能", "条件を満たす検証済み候補があります。"),
    "UNREACHABLE": ("到達不能", "指定範囲の完全探索で候補がありません。"),
    "INPUT_INCOMPLETE": ("入力不足", "計算に必要な入力を確認してください。"),
    "RULES_INCOMPLETE": ("規則不足", "資格済み規則が揃うまで計算を停止します。"),
    "REVIEW_REQUIRED": ("要審査", "このルールセットは審査完了まで実行できません。"),
}


def condition_rule_options(rule_model: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only official or confirmed-custom profiles, never Drafts."""
    options: list[dict[str, Any]] = []
    for item in rule_model.get("official", ()):
        support = item.get("execution_support", {}).get("match", {}).get("status")
        if support != "SUPPORTED":
            continue
        options.append({
            "kind": "OFFICIAL", "name": item["name"],
            "version": item["version"], "ref": deepcopy(item["ref"]),
            "competition_format": "NORMAL_FINAL",
        })
    for item in rule_model.get("confirmed", ()):
        options.append({
            "kind": "CUSTOM_CONFIRMED", "name": item["name"],
            "version": item["version"], "ref": deepcopy(item["ref"]),
            "competition_format": "NORMAL_FINAL",
        })
    return options


def condition_tool_model(
    rule_model: Mapping[str, Any], *, source_kind: str = "STANDALONE",
    fixed_rule_ref: Mapping[str, Any] | None = None,
    response: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a display model without altering a Live, Replay, or rule snapshot."""
    if source_kind not in {"STANDALONE", "LIVE_SESSION", "COMPLETED_REPLAY"}:
        raise ValueError("CONDITION_SOURCE_INVALID")
    locked = source_kind != "STANDALONE"
    if locked and not fixed_rule_ref:
        raise ValueError("RULE_SNAPSHOT_REQUIRED")
    if response and response.get("document_kind", "CONDITION_RESPONSE") != "CONDITION_RESPONSE":
        raise ValueError("CONDITION_DOCUMENT_UNSUPPORTED")
    result = condition_result_model(response, state=state) if response else None
    return {
        "source_kind": source_kind,
        "rule_locked": locked,
        "fixed_rule_ref": deepcopy(dict(fixed_rule_ref)) if fixed_rule_ref else None,
        "rule_options": [] if locked else condition_rule_options(rule_model),
        "result": result,
    }


def _ranks(scores: Mapping[str, int]) -> dict[str, int]:
    ordered = sorted(scores, key=lambda participant: (-scores[participant], participant))
    return {participant: index + 1 for index, participant in enumerate(ordered)}


def condition_result_model(
    response: Mapping[str, Any] | None, *, state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce a CONDITION_RESPONSE to traceable, non-strategic display fields."""
    if response is None:
        status = "INPUT_INCOMPLETE"
        response = {}
    else:
        status = str(response.get("status", "INPUT_INCOMPLETE"))
    if status not in DISPLAY_STATUSES:
        status = "REVIEW_REQUIRED"
    label, summary = DISPLAY_STATUSES[status]
    candidates = response.get("candidates") or response.get("partial_candidates") or ()
    candidate = deepcopy(candidates[0]) if candidates else None
    before_scores = {
        str(item["participant_id"]): int(item["value"])
        for item in (state or {}).get("scores", ())
    }
    after_scores = {
        str(key): int(value)
        for key, value in (candidate or {}).get("evidence", {}).get("scores_after", {}).items()
    }
    return {
        "status": status, "label": label, "summary": summary,
        "minimum_condition": (candidate or {}).get("quantity_axes"),
        "before_scores": before_scores,
        "before_ranks": _ranks(before_scores) if before_scores else {},
        "after_scores": after_scores,
        "after_ranks": _ranks(after_scores) if after_scores else {},
        "evidence": deepcopy(response.get("evidence", ())),
        "rules": deepcopy(response.get("match_rule_ref")),
    }
