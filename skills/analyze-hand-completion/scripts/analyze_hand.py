#!/usr/bin/env python3
"""CLI boundary for the deterministic hand-completion engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from engine.codex_adapter import analyze_hand_for_codex  # noqa: E402
from engine.winning import analyze_win, resolve_multiple_ron  # noqa: E402


MULTIPLE_RON_PROFILE_PATH = ROOT / "engine/profiles/multiple_ron_profiles.v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic hand-completion analysis")
    parser.add_argument("notation", nargs="?", help="compact tile notation")
    parser.add_argument("--fixed-meld-count", type=int, default=0)
    parser.add_argument("--fixed-tiles", default="", help="physical tiles in fixed melds or quads")
    parser.add_argument("--win-tile", help="winning tile for win qualification")
    parser.add_argument("--method", choices=("tsumo", "ron"), help="winning method")
    parser.add_argument("--context-json", default="{}", help="JSON object with approved hand and event state")
    parser.add_argument("--claimants-json", help="JSON list of independently evaluated ron claimants")
    parser.add_argument(
        "--multiple-ron-profile",
        choices=("m_league_head_bump", "ema_multiple_winners"),
        help="explicit multiple-ron profile; defaults to Chombo Agent policy",
    )
    args = parser.parse_args()
    try:
        if args.claimants_json:
            if args.notation or args.win_tile or args.method:
                parser.error("notation and win arguments cannot be combined with --claimants-json")
            claimants = json.loads(args.claimants_json)
            if not isinstance(claimants, list):
                parser.error("--claimants-json must contain a JSON list")
            profile = args.multiple_ron_profile or "m_league_head_bump"
            profiles = json.loads(MULTIPLE_RON_PROFILE_PATH.read_text())["profiles"]
            result = resolve_multiple_ron(claimants, profiles[profile])
            result["profile"] = profile
            result["profile_source"] = "explicit" if args.multiple_ron_profile else "agent_default"
        elif args.win_tile or args.method:
            if not args.notation or not args.win_tile or not args.method:
                parser.error("notation, --win-tile and --method are all required for win analysis")
            context = json.loads(args.context_json)
            if not isinstance(context, dict):
                parser.error("--context-json must contain a JSON object")
            result = analyze_win(args.notation, args.win_tile, args.method, context=context)
        elif args.fixed_meld_count:
            if not args.notation:
                parser.error("notation is required for fixed-meld analysis")
            if not args.fixed_tiles:
                parser.error("--fixed-tiles is required when --fixed-meld-count is used")
            result = analyze_hand_for_codex(
                args.notation, fixed_meld_count=args.fixed_meld_count, fixed_tiles=args.fixed_tiles,
            )
        else:
            if not args.notation:
                parser.error("notation is required")
            if args.fixed_tiles:
                parser.error("--fixed-meld-count is required when --fixed-tiles is used")
            result = analyze_hand_for_codex(args.notation)
    except (ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
