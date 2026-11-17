"""Explicit application rule selection and shared execution-boundary validation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Any

from engine.rule_drafts import DraftStore, DraftValidator, new_draft, strict_json
from engine.rule_profiles import ResolvedRuleProfile, RuleProfileError, _freeze, resolve_registered_profile_path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RuleSelection:
    alias: str | None = None
    reference: Mapping[str, Any] | None = None
    store_directory: Path | None = None


def load_selected_profile(selection: RuleSelection, *, route: str) -> ResolvedRuleProfile:
    """Revalidate an explicit official or confirmed custom selection, without fallback."""
    if not isinstance(selection, RuleSelection) or (selection.alias is None) == (selection.reference is None):
        raise RuleProfileError("RULE_PROFILE_INCOMPLETE", {"reason": "ONE_PROFILE_SELECTOR_REQUIRED"})
    validator = DraftValidator(ROOT)
    reference = selection.reference
    if selection.alias is not None:
        path = resolve_registered_profile_path(
            ROOT / "engine/profiles/registry.v1.json", ROOT / "engine/profiles",
            alias=selection.alias,
        )
        _, _, _, _, profiles, _ = validator.catalogs()
        reference = next((entry["ref"] for entry, _ in profiles if entry["path"] == path.name), None)
        if reference is None:
            raise RuleProfileError("REVIEW_REQUIRED", {"reason": "PROFILE_NOT_APPROVED"})
    if not isinstance(reference, Mapping):
        raise RuleProfileError("REVIEW_REQUIRED", {"reason": "INVALID_REFERENCE"})
    if reference.get("kind") == "CUSTOM_CONFIRMED":
        if selection.store_directory is None:
            raise RuleProfileError("RULE_PROFILE_INCOMPLETE", {"reason": "CUSTOM_STORE_REQUIRED"})
        return DraftStore(selection.store_directory, validator).execution_profile(reference, route=route)
    if selection.store_directory is not None or reference.get("kind") != "OFFICIAL":
        raise RuleProfileError("REVIEW_REQUIRED", {"reason": "OFFICIAL_OR_CONFIRMED_REFERENCE_REQUIRED"})
    result = validator.validate(new_draft(reference, "execution selection"))
    if result["status"] != "VALIDATED":
        raise RuleProfileError(result["status"], {"issues": result["issues"]})
    if result["execution_support"].get(route, {}).get("status") != "SUPPORTED":
        raise RuleProfileError("UNSUPPORTED_RULE_VALUE", {"route": route})
    return ResolvedRuleProfile(reference["profile_id"], reference["snapshot_id"],
                               _freeze(result["resolved_preview"]["dimensions"]))


def add_rule_arguments(parser: argparse.ArgumentParser) -> None:
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument("--rule-alias", help="explicit registered rule alias")
    selectors.add_argument("--rule-ref", type=Path, help="JSON official or confirmed custom reference")
    parser.add_argument("--rule-store", type=Path, help="store containing the confirmed custom rule")


def selection_from_arguments(args: argparse.Namespace) -> RuleSelection:
    return RuleSelection(args.rule_alias,
                         strict_json(args.rule_ref.read_bytes()) if args.rule_ref else None,
                         args.rule_store)
