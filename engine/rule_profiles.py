"""Validation and immutable snapshot loading for resolved rule profiles."""

from __future__ import annotations

from engine.knowledge_store import concept_documents

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


RULE_PROFILE_INCOMPLETE = "RULE_PROFILE_INCOMPLETE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
RULE_PROFILE_AMBIGUOUS = "RULE_PROFILE_AMBIGUOUS"
MALFORMED_PROFILE_SCOPE = "MALFORMED_PROFILE_SCOPE"
MALFORMED_CONCEPT_AUDIT = "MALFORMED_CONCEPT_AUDIT"
MALFORMED_SPECIAL_HAND_END_CAPABILITY = "MALFORMED_SPECIAL_HAND_END_CAPABILITY"
SPECIAL_HAND_END_NOT_EXECUTABLE = "SPECIAL_HAND_END_NOT_EXECUTABLE"

COMPLETE_PROFILE_STAGES = frozenset(
    {
        "match_start",
        "hand_progress",
        "hand_settlement",
        "hand_transition",
        "match_end",
        "final_points",
    }
)
SCOPE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
DIMENSION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

REQUIRED_V0_1_DIMENSIONS = frozenset(
    {
        "match.start.procedure",
        "match.start.points",
        "tile.red_five.composition",
        "dora.profile",
        "response.call.priority",
        "response.post_call_discard_restriction",
        "kan.count_limit",
        "kan.profile_boundaries",
        "kan.last_draw_restrictions",
        "riichi.score_eligibility",
        "riichi.post_establishment_kan",
        "riichi.first_turn_and_ippatsu",
        "win.multiple_ron_resolution",
        "hand.abortive_draws",
        "yaku.ordinary_catalog_and_values",
        "yaku.yakuman_catalog_and_values",
        "scoring.fu",
        "scoring.engine_profile",
        "scoring.limit_and_rounding",
        "settlement.honba",
        "settlement.riichi_deposit",
        "settlement.exhaustive_draw_tenpai",
    }
)


class RuleProfileError(ValueError):
    """A structured validation failure safe for Engine callers."""

    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code = code
        self.details = dict(details)
        super().__init__(f"{code}: {json.dumps(self.details, ensure_ascii=False, sort_keys=True)}")


@dataclass(frozen=True)
class ResolvedRuleProfile:
    profile_id: str
    snapshot_id: str
    dimensions: Mapping[str, Mapping[str, Any]]

    def execution_metadata(self) -> dict[str, str]:
        """Metadata that every execution result and hand log must retain."""
        return {"rule_profile_id": self.profile_id, "rule_snapshot_id": self.snapshot_id}

    def value(self, dimension_id: str) -> Any:
        """Return one already-resolved value without exposing profile naming to Engine code."""
        try:
            return self.dimensions[dimension_id]["value"]
        except KeyError as error:
            raise RuleProfileError(
                RULE_PROFILE_INCOMPLETE, {"missing_dimensions": [dimension_id]}
            ) from error


def require_profile_match(profile: ResolvedRuleProfile, state: Any, *, error_type=RuleProfileError) -> None:
    """Reject missing or mixed profile identities before rule-dependent processing."""
    profile_id = state.get("rule_profile_id") if isinstance(state, Mapping) else getattr(state, "rule_profile_id", None)
    snapshot_id = state.get("rule_snapshot_id") if isinstance(state, Mapping) else getattr(state, "rule_snapshot_id", None)
    if not profile_id or not snapshot_id or (profile_id, snapshot_id) != (profile.profile_id, profile.snapshot_id):
        raise error_type("RULE_SNAPSHOT_MISMATCH", {
            "state_profile_id": profile_id, "state_snapshot_id": snapshot_id,
            "resolved_profile_id": profile.profile_id, "resolved_snapshot_id": profile.snapshot_id,
        })


def require_special_hand_end_capability(
    profile: ResolvedRuleProfile,
    capability_path: Path,
    *,
    family: str,
) -> str:
    """Return the fixed family status or reject before a hand can start."""
    document = json.loads(capability_path.read_text(encoding="utf-8"))
    families = document.get("families")
    entries = document.get("profiles")
    policy = document.get("policy")
    expected_policy = {
        "EXECUTABLE": "allow_reason_specific_engine_entry",
        "NOT_ADOPTED": "return_no_candidate_without_side_effects",
        "REVIEW_REQUIRED": "reject_before_hand_start",
    }
    malformed = (
        set(document) != {
            "schema_version", "capability_id", "profile_registry", "families",
            "profiles", "policy",
        }
        or document.get("schema_version") != 1
        or document.get("capability_id") != "special_hand_end.v0_3_1"
        or document.get("profile_registry") != "registry.v1.json"
        or families != ["abortive_draw", "nagashi_mangan"]
        or not isinstance(entries, list)
        or policy != expected_policy
    )
    if malformed:
        raise RuleProfileError(
            MALFORMED_SPECIAL_HAND_END_CAPABILITY, {"path": str(capability_path)}
        )
    matching = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"profile_id", *families}
            or not isinstance(entry.get("profile_id"), str)
            or any(entry.get(item) not in expected_policy for item in families)
        ):
            raise RuleProfileError(
                MALFORMED_SPECIAL_HAND_END_CAPABILITY, {"path": str(capability_path)}
            )
        if entry["profile_id"] == profile.profile_id:
            matching.append(entry)
    if len(matching) != 1 or family not in families:
        raise RuleProfileError(
            MALFORMED_SPECIAL_HAND_END_CAPABILITY,
            {"profile_id": profile.profile_id, "family": family},
        )
    status = matching[0][family]
    if status == "REVIEW_REQUIRED":
        raise RuleProfileError(
            SPECIAL_HAND_END_NOT_EXECUTABLE,
            {"profile_id": profile.profile_id, "family": family, "status": status},
        )
    return status


def load_complete_profile_scope(scope_path: Path) -> frozenset[str]:
    """Load one closed required-dimension set without interpreting rule values."""
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "scope_id",
        "applies_to",
        "required_dimensions",
        "common_exclusions",
    }
    dimensions = scope.get("required_dimensions")
    exclusions = scope.get("common_exclusions")
    malformed = (
        set(scope) != expected_fields
        or scope.get("schema_version") != 1
        or scope.get("applies_to") != "four_player_east_south_match"
        or not isinstance(scope.get("scope_id"), str)
        or SCOPE_ID_PATTERN.fullmatch(scope["scope_id"]) is None
        or not isinstance(dimensions, list)
        or not dimensions
        or not isinstance(exclusions, list)
        or not exclusions
    )
    if malformed:
        raise RuleProfileError(MALFORMED_PROFILE_SCOPE, {"path": str(scope_path)})

    dimension_ids: list[str] = []
    for dimension in dimensions:
        if (
            not isinstance(dimension, dict)
            or set(dimension) != {"dimension_id", "stage"}
            or not isinstance(dimension.get("dimension_id"), str)
            or DIMENSION_ID_PATTERN.fullmatch(dimension["dimension_id"]) is None
            or dimension.get("stage") not in COMPLETE_PROFILE_STAGES
        ):
            raise RuleProfileError(MALFORMED_PROFILE_SCOPE, {"path": str(scope_path)})
        dimension_ids.append(dimension["dimension_id"])

    category_ids: list[str] = []
    for exclusion in exclusions:
        if (
            not isinstance(exclusion, dict)
            or set(exclusion) != {"category_id", "disposition"}
            or not isinstance(exclusion.get("category_id"), str)
            or DIMENSION_ID_PATTERN.fullmatch(exclusion["category_id"]) is None
            or exclusion.get("disposition")
            != "REJECT_AS_INPUT_OR_INVARIANT_VIOLATION"
        ):
            raise RuleProfileError(MALFORMED_PROFILE_SCOPE, {"path": str(scope_path)})
        category_ids.append(exclusion["category_id"])

    duplicate_dimensions = sorted(
        dimension_id
        for dimension_id in set(dimension_ids)
        if dimension_ids.count(dimension_id) > 1
    )
    duplicate_exclusions = sorted(
        category_id for category_id in set(category_ids) if category_ids.count(category_id) > 1
    )
    if duplicate_dimensions or duplicate_exclusions:
        raise RuleProfileError(
            MALFORMED_PROFILE_SCOPE,
            {
                "duplicate_dimensions": duplicate_dimensions,
                "duplicate_exclusions": duplicate_exclusions,
            },
        )
    return frozenset(dimension_ids)


def validate_profile_concept_audit(
    audit_path: Path,
    scope_path: Path,
    concepts_dir: Path,
) -> None:
    """Verify that one audit covers its scope using qualified Concept references."""
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "audit_id",
        "scope_id",
        "audited_at",
        "result",
        "dimensions",
    }
    dimensions = audit.get("dimensions")
    if (
        set(audit) != expected_fields
        or audit.get("schema_version") != 1
        or audit.get("result") != "READY_FOR_VERSIONED_PROFILE"
        or not isinstance(audit.get("audit_id"), str)
        or not isinstance(audit.get("scope_id"), str)
        or not isinstance(audit.get("audited_at"), str)
        or not isinstance(dimensions, list)
    ):
        raise RuleProfileError(MALFORMED_CONCEPT_AUDIT, {"path": str(audit_path)})

    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    required_dimensions = load_complete_profile_scope(scope_path)
    if audit["scope_id"] != scope["scope_id"]:
        raise RuleProfileError(
            MALFORMED_CONCEPT_AUDIT,
            {"reason": "SCOPE_ID_MISMATCH", "path": str(audit_path)},
        )

    expected_unspecified = {
        "RULESET_INVARIANT": "USE_QUALIFIED_VALUE",
        "RULESET_DEPENDENT": "RULE_PROFILE_INCOMPLETE",
        "RULESET_UNRESOLVED": "REVIEW_REQUIRED",
    }
    audited_ids: list[str] = []
    referenced_ids: set[str] = set()
    for dimension in dimensions:
        if not isinstance(dimension, dict) or set(dimension) != {
            "dimension_id",
            "classification",
            "concept_ids",
            "on_unspecified",
        }:
            raise RuleProfileError(MALFORMED_CONCEPT_AUDIT, {"path": str(audit_path)})
        dimension_id = dimension.get("dimension_id")
        classification = dimension.get("classification")
        concept_ids = dimension.get("concept_ids")
        if (
            not isinstance(dimension_id, str)
            or DIMENSION_ID_PATTERN.fullmatch(dimension_id) is None
            or classification not in expected_unspecified
            or dimension.get("on_unspecified") != expected_unspecified[classification]
            or not isinstance(concept_ids, list)
            or not concept_ids
            or not all(isinstance(item, str) for item in concept_ids)
            or len(concept_ids) != len(set(concept_ids))
        ):
            raise RuleProfileError(MALFORMED_CONCEPT_AUDIT, {"path": str(audit_path)})
        audited_ids.append(dimension_id)
        referenced_ids.update(concept_ids)

    audited_set = set(audited_ids)
    missing = sorted(required_dimensions - audited_set)
    unexpected = sorted(audited_set - required_dimensions)
    duplicates = sorted(item for item in audited_set if audited_ids.count(item) > 1)
    if missing or unexpected or duplicates:
        raise RuleProfileError(
            MALFORMED_CONCEPT_AUDIT,
            {
                "missing_dimensions": missing,
                "unexpected_dimensions": unexpected,
                "duplicate_dimensions": duplicates,
            },
        )

    unqualified = sorted(referenced_ids - _qualified_concept_ids(concepts_dir))
    if unqualified:
        raise RuleProfileError(REVIEW_REQUIRED, {"unqualified_concept_ids": unqualified})


def resolve_registered_profile_path(
    registry_path: Path,
    profiles_dir: Path,
    *,
    ruleset_id: str | None = None,
    ruleset_version: str | None = None,
    alias: str | None = None,
) -> Path:
    """Resolve an approved profile file without interpreting competition names in Engine code."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = registry.get("profiles")
    if registry.get("schema_version") != 1 or not isinstance(entries, list):
        raise RuleProfileError(REVIEW_REQUIRED, {"reason": "MALFORMED_PROFILE_REGISTRY"})
    required_entry_fields = {
        "ruleset_id",
        "ruleset_version",
        "profile_id",
        "path",
        "aliases",
    }
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != required_entry_fields
            or not all(
                isinstance(entry[field], str) and entry[field]
                for field in ("ruleset_id", "ruleset_version", "profile_id", "path")
            )
            or not isinstance(entry["aliases"], list)
            or not all(isinstance(item, str) and item for item in entry["aliases"])
            or len(entry["aliases"]) != len(set(entry["aliases"]))
        ):
            raise RuleProfileError(
                REVIEW_REQUIRED, {"reason": "MALFORMED_PROFILE_REGISTRY_ENTRY"}
            )

    exact_selector = ruleset_id is not None or ruleset_version is not None
    if exact_selector and (not ruleset_id or not ruleset_version):
        missing = "ruleset_id" if not ruleset_id else "ruleset_version"
        raise RuleProfileError(REVIEW_REQUIRED, {"missing_fields": [missing]})
    if exact_selector and alias is not None:
        raise RuleProfileError(
            REVIEW_REQUIRED, {"reason": "MULTIPLE_PROFILE_SELECTORS"}
        )
    if not exact_selector and alias is None:
        raise RuleProfileError(REVIEW_REQUIRED, {"reason": "MISSING_PROFILE_SELECTOR"})

    if exact_selector:
        matches = [
            entry
            for entry in entries
            if entry.get("ruleset_id") == ruleset_id
            and entry.get("ruleset_version") == ruleset_version
        ]
    else:
        matches = [entry for entry in entries if alias in entry["aliases"]]

    if not matches:
        details = {"reason": "PROFILE_NOT_REGISTERED"}
        if exact_selector:
            details.update({"ruleset_id": ruleset_id, "ruleset_version": ruleset_version})
        else:
            details["alias"] = alias
        raise RuleProfileError(REVIEW_REQUIRED, details)
    if len(matches) != 1:
        raise RuleProfileError(
            RULE_PROFILE_AMBIGUOUS,
            {"matching_profile_ids": sorted(entry["profile_id"] for entry in matches)},
        )

    entry = matches[0]
    profile_path = (profiles_dir / entry["path"]).resolve()
    if profile_path.parent != profiles_dir.resolve() or not profile_path.is_file():
        raise RuleProfileError(
            REVIEW_REQUIRED,
            {"reason": "REGISTERED_PROFILE_FILE_UNAVAILABLE", "profile_id": entry["profile_id"]},
        )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if profile.get("profile_id") != entry["profile_id"]:
        raise RuleProfileError(
            REVIEW_REQUIRED,
            {"reason": "REGISTERED_PROFILE_ID_MISMATCH", "profile_id": entry["profile_id"]},
        )
    return profile_path


def load_registered_profile(
    registry_path: Path,
    profiles_dir: Path,
    concepts_dir: Path,
    *,
    required_dimensions: frozenset[str],
    ruleset_id: str | None = None,
    ruleset_version: str | None = None,
    alias: str | None = None,
) -> ResolvedRuleProfile:
    """Resolve, validate, snapshot, and freeze one registered rule profile."""
    path = resolve_registered_profile_path(
        registry_path,
        profiles_dir.resolve(),
        ruleset_id=ruleset_id,
        ruleset_version=ruleset_version,
        alias=alias,
    )
    return load_resolved_profile(path, concepts_dir, required_dimensions=required_dimensions)


def _qualified_concept_ids(concepts_dir: Path) -> set[str]:
    qualified: set[str] = set()
    for _, document in concept_documents(concepts_dir):
        if document.get("status") == "QUALIFIED":
            qualified.update(item["concept_id"] for item in document.get("concepts", []))
    return qualified


def _canonical_snapshot_id(profile: Mapping[str, Any]) -> str:
    snapshot_source = {
        key: value
        for key, value in profile.items()
        if key not in {"snapshot_id", "deferred_implementation"}
    }
    canonical = json.dumps(
        snapshot_source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def load_resolved_profile(
    profile_path: Path,
    concepts_dir: Path,
    *,
    required_dimensions: frozenset[str] = REQUIRED_V0_1_DIMENSIONS,
) -> ResolvedRuleProfile:
    """Validate a profile against qualified Concepts and freeze its resolved values."""
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if "custom_ruleset_id" in profile or "draft_id" in profile:
        raise RuleProfileError(
            REVIEW_REQUIRED, {"reason": "CUSTOM_PROFILE_REQUIRES_DRAFT_STORE_VALIDATION"}
        )
    dimensions = profile.get("dimensions")
    if not isinstance(dimensions, dict):
        raise RuleProfileError(RULE_PROFILE_INCOMPLETE, {"missing_fields": ["dimensions"]})

    missing = sorted(required_dimensions - dimensions.keys())
    malformed = sorted(
        dimension_id
        for dimension_id, dimension in dimensions.items()
        if not isinstance(dimension, dict)
        or "value" not in dimension
        or not dimension.get("concept_ids")
    )
    if missing or malformed:
        raise RuleProfileError(
            RULE_PROFILE_INCOMPLETE,
            {"missing_dimensions": missing, "malformed_dimensions": malformed},
        )

    qualified = _qualified_concept_ids(concepts_dir)
    unqualified = sorted(
        {
            concept_id
            for dimension in dimensions.values()
            for concept_id in dimension["concept_ids"]
            if concept_id not in qualified
        }
    )
    if unqualified:
        raise RuleProfileError(REVIEW_REQUIRED, {"unqualified_concept_ids": unqualified})

    snapshot_id = _canonical_snapshot_id(profile)
    frozen_dimensions = _freeze(dimensions)
    return ResolvedRuleProfile(
        profile_id=profile["profile_id"],
        snapshot_id=snapshot_id,
        dimensions=frozen_dimensions,
    )
