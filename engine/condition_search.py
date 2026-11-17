"""E40-05 bounded, terminating, deterministic condition-search orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns
from typing import Any, Callable, Mapping

from engine.condition_aggregate import calculate_aggregate_conditions
from engine.condition_candidates import CandidateError
from engine.condition_documents import ConditionBundle, canonical_bytes
from engine.condition_limits import SearchBudget, SearchLimitReached
from engine.condition_single_match import calculate_single_match_conditions
from engine.match_state import MatchState
from engine.rule_profiles import ResolvedRuleProfile


CALCULATOR_VERSION = "chombo.condition-search.v0.4.7"


@dataclass(frozen=True)
class ConditionSearchResult:
    status: str
    candidates: tuple[Mapping[str, Any], ...]
    partial_candidates: tuple[Mapping[str, Any], ...]
    limit_reason: str | None
    usage: Mapping[str, int]
    complete_search_evidence: Mapping[str, Any] | None


def _digest_candidates(candidates: tuple[Mapping[str, Any], ...]) -> str:
    import hashlib

    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(child) for key, child in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(child) for child in value]
        return value

    return "sha256:" + hashlib.sha256(canonical_bytes(plain(candidates))).hexdigest()


def calculate_conditions(
    bundle: ConditionBundle, match: MatchState, profile: ResolvedRuleProfile,
    *, clock_ns: Callable[[], int] = monotonic_ns,
) -> ConditionSearchResult:
    """Run one bounded search and distinguish complete absence from truncation."""

    budget = SearchBudget(bundle.request["search_scope"], clock_ns=clock_ns)
    try:
        budget.enter_depth()
        if bundle.request["ranking_scope"] == "SINGLE_MATCH_RANK":
            projected = calculate_single_match_conditions(bundle, match, profile, budget=budget)
        else:
            projected = calculate_aggregate_conditions(bundle, match, profile, budget=budget)
        successful = tuple(item for item in projected if item["target_satisfied"])
        evidence = {
            "search_complete": True,
            "calculator_version": CALCULATOR_VERSION,
            "candidate_set_digest": _digest_candidates(successful),
            "stable_order": "comparability-signature-quantity-axes-candidate-id",
        }
        return ConditionSearchResult(
            status="ACHIEVABLE" if successful else "UNREACHABLE",
            candidates=successful, partial_candidates=(), limit_reason=None,
            usage=budget.usage(), complete_search_evidence=evidence,
        )
    except SearchLimitReached as error:
        partial = tuple(item for item in error.partial_candidates if item["target_satisfied"])
        return ConditionSearchResult(
            status="INDETERMINATE_LIMIT", candidates=(), partial_candidates=partial,
            limit_reason=error.reason, usage=error.usage,
            complete_search_evidence=None,
        )
    except CandidateError as error:
        if error.code != "INDETERMINATE_LIMIT":
            raise
        return ConditionSearchResult(
            status="INDETERMINATE_LIMIT", candidates=(), partial_candidates=(),
            limit_reason=error.reason, usage=budget.usage(),
            complete_search_evidence=None,
        )
