#!/usr/bin/env python3
"""CLI boundary for generating one autonomous Chombo hand log."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from engine.canonical_record import (custom_rules_for_selection, development_producer,
                                     hand_record_document, write_record)  # noqa: E402
from engine.hand_orchestrator import run_autonomous_hand  # noqa: E402
from engine.rule_inputs import RuleSelection, add_rule_arguments, load_selected_profile, selection_from_arguments  # noqa: E402
from engine.seed import resolve_seed  # noqa: E402


def generate(seed: int, output: Path, *, selection: RuleSelection, force: bool = False) -> dict:
    destination = output if output.is_absolute() else ROOT / output
    destination = destination.resolve()
    if destination.exists() and not force:
        raise FileExistsError(f"OUTPUT_EXISTS: {destination}")

    profile = load_selected_profile(selection, route="hand")
    hand_run = run_autonomous_hand(seed, profile)
    producer = development_producer(ROOT)
    from uuid import uuid4
    record = hand_record_document(
        hand_run.log, app_version=producer["app_version"],
        build_revision=producer["build_revision"], engine_version=producer["engine_version"],
        source_session_id=str(uuid4()), rules=custom_rules_for_selection(selection),
    )
    write_record(record, destination, allow_evaluation=True)
    result = hand_run.log.result
    return {
        "status": "GENERATED",
        "path": str(destination),
        "hand_id": hand_run.log.hand_id,
        "seed": seed,
        "rule_profile_id": hand_run.log.rule_profile_id,
        "rule_snapshot_id": hand_run.log.rule_snapshot_id,
        "end_reason": result.end_reason.value,
        "winners": [seat.value for seat in result.winners],
        "final_scores": {
            seat.value: score for seat, score in result.final_scores.items()
        },
        "event_count": len(hand_run.log.events),
        "decision_count": hand_run.decision_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate one autonomous Chombo v0.3 evaluation hand record"
    )
    add_rule_arguments(parser)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    seed = resolve_seed(args.seed)
    output = args.output or Path(f"outputs/autonomous-hand-seed-{seed}.json")

    try:
        summary = generate(seed, output, selection=selection_from_arguments(args), force=args.force)
    except (FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
