#!/usr/bin/env python3
"""CLI boundary for the deterministic scoring Engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from engine.scoring import (  # noqa: E402
    calculate_score,
    select_highest_score,
    validate_profile_snapshot,
)


def score_declaration(result: dict) -> str:
    """Format the conventional spoken score, retaining the honba transition."""
    inputs = result["input"]
    base = result["base_payments"]
    total = result["total_payments"]
    has_honba = inputs["honba_count"] > 0
    if inputs["method"] == "ron":
        before, after = base["discarder"], total["discarder"]
        return f"{before}は{after}" if has_honba else str(after)
    if inputs["dealer"]:
        before, after = base["each_non_winner"], total["each_non_winner"]
        return f"{before}は{after}オール" if has_honba else f"{after}オール"
    before = f'{base["each_non_dealer"]}・{base["dealer"]}'
    after = f'{total["each_non_dealer"]}・{total["dealer"]}'
    return f"{before}は{after}" if has_honba else after


def add_score_declarations(result: dict) -> dict:
    if "candidates" in result:
        for candidate in result["candidates"]:
            candidate["score"]["score_declaration"] = score_declaration(candidate["score"])
    else:
        result["score_declaration"] = score_declaration(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate a qualified Japanese-mahjong score")
    parser.add_argument("--fu", type=int, default=0)
    parser.add_argument("--han", type=int, default=0)
    parser.add_argument("--yakuman-units", type=int, default=0)
    parser.add_argument("--method", choices=("ron", "tsumo"))
    role = parser.add_mutually_exclusive_group()
    role.add_argument("--dealer", action="store_true")
    role.add_argument("--non-dealer", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--honba", type=int, default=0)
    parser.add_argument("--candidates-json", help="JSON list of public numeric scoring candidates; internal overrides are forbidden")
    args = parser.parse_args()

    try:
        qualification = validate_profile_snapshot(repository_root=ROOT)
        if not qualification["valid"]:
            raise ValueError(
                "UNQUALIFIED_PROFILE_SNAPSHOT: "
                + ", ".join(qualification["invalid_sources"])
            )
        if args.candidates_json:
            if args.method or args.profile or args.dealer or args.non_dealer or args.fu or args.han or args.yakuman_units or args.honba:
                parser.error("single-candidate arguments cannot be combined with --candidates-json")
            candidates = json.loads(args.candidates_json)
            if not isinstance(candidates, list):
                parser.error("--candidates-json must contain a JSON list")
            result = add_score_declarations(select_highest_score(candidates))
        else:
            if not args.method or not args.profile or not (args.dealer or args.non_dealer):
                parser.error("--method, --profile, and exactly one dealer role are required")
            result = add_score_declarations(calculate_score(
                fu=args.fu,
                han=args.han,
                yakuman_units=args.yakuman_units,
                method=args.method,
                dealer=args.dealer,
                profile_id=args.profile,
                honba_count=args.honba,
            ))
    except (ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
