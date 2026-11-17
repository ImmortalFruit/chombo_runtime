#!/usr/bin/env python3
"""CLI boundary for deterministic rank-condition calculation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from engine.condition_documents import ConditionDocumentError, _strict_parse  # noqa: E402
from engine.condition_skill import (  # noqa: E402
    calculate_condition_documents,
    load_document_path,
    load_match_profile,
    qualified_concept_ids,
)
from engine.rule_profiles import RuleProfileError  # noqa: E402
from engine.navigator import NavigatorError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate a qualified Japanese-mahjong rank condition"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--request", type=Path)
    inputs.add_argument("--point-input", type=Path, help="Structured condition.points_query input")
    parser.add_argument("--match-profile", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--support-ref", type=Path)
    parser.add_argument("--aggregate-profile", type=Path)
    args = parser.parse_args()

    try:
        if args.point_input:
            from engine.condition_points import calculate_point_conditions, CALCULATOR_VERSION
            from engine.runtime_storage import build_metadata
            if args.state or args.support_ref or args.aggregate_profile:
                raise NavigatorError("INVALID_REQUEST")
            metadata = build_metadata(ROOT)
            value = _strict_parse(args.point_input.read_bytes())
            profile = load_match_profile(args.match_profile, ROOT / "concepts")
            response = calculate_point_conditions(value, profile,
                producer={"app_id": "chombo-condition-skill", "app_version": metadata["version"],
                          "build_revision": metadata["source_revision"], "engine_version": CALCULATOR_VERSION},
                qualified_concepts=qualified_concept_ids(ROOT / "concepts"))
            print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        request = load_document_path(args.request, "CONDITION_REQUEST")
        state = load_document_path(args.state, "CONDITION_STATE") if args.state else None
        support_ref = (
            load_document_path(args.support_ref, "CONDITION_SUPPORT_REF")
            if args.support_ref else None
        )
        aggregate_profile = (
            load_document_path(args.aggregate_profile, "AGGREGATE_RANK_PROFILE")
            if args.aggregate_profile else None
        )
        profile = load_match_profile(args.match_profile, ROOT / "concepts")
        response = calculate_condition_documents(
            request, profile,
            qualified_concepts=qualified_concept_ids(ROOT / "concepts"),
            state=state, support_ref=support_ref,
            aggregate_profile=aggregate_profile,
        )
    except (OSError, ValueError, NavigatorError) as error:
        if isinstance(error, ConditionDocumentError):
            payload = error.as_dict()
        elif isinstance(error, RuleProfileError):
            payload = {"code": error.code, "details": error.details}
        elif getattr(error, "code", None):
            payload = {"code": error.code}
        elif isinstance(error, ValueError):
            payload = {"code": "INVALID_REQUEST"}
        else:
            payload = {"code": type(error).__name__, "reason": str(error)}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
