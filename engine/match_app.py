"""Local live application for an east-south Chombo match."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from engine.game_state import Seat
from engine.hand_app import HandAppServer, ROOT
from engine.ui_compat import ui_document
from engine.match_orchestrator import InteractiveMatchSession
from engine.canonical_record import (custom_rules_for_selection, development_producer, match_record_v03_document,
                                     match_record_viewer_document, read_record, write_record)
from engine.observer_projection import AUTHORIZED_REPLAY_OBSERVER, PUBLIC_OBSERVER, human_observer
from engine.rule_inputs import RuleSelection, add_rule_arguments, load_selected_profile, selection_from_arguments
from engine.seed import resolve_seed


SOUTH_4_TEST_ROUTE = "/test/south-4"
SOUTH_4_TEST_SCORES = {
    "chombo-agent-1": 16_700,
    "chombo-agent-2": 28_900,
    "chombo-agent-3": 48_200,
    "human-1": 6_200,
}


@dataclass(frozen=True)
class ConfiguredMatchStart:
    round_wind: Seat
    hand_number: int
    honba: int
    riichi_deposits: int
    human_score: int
    chombo_scores: tuple[int, int, int]

    @property
    def participant_scores(self) -> dict[str, int]:
        return {
            "human-1": self.human_score,
            **{
                f"chombo-agent-{index}": score
                for index, score in enumerate(self.chombo_scores, start=1)
            },
        }


def hanchan_server(
    seed: int,
    port: int,
    configured_start: ConfiguredMatchStart | None = None,
    log_path: Path = Path("match.json"),
    *, selection: RuleSelection,
) -> HandAppServer:
    profile = load_selected_profile(selection, route="match")
    producer = development_producer(ROOT)
    source_session_id = str(uuid4())
    record_rules = custom_rules_for_selection(selection)
    def create_session(match_seed: int | None = None) -> InteractiveMatchSession:
        return InteractiveMatchSession(
            resolve_seed(match_seed), profile, human_count=1, agent_delay_seconds=1.0
        )

    def create_south_4_test_session() -> InteractiveMatchSession:
        return InteractiveMatchSession(
            seed,
            profile,
            human_count=1,
            agent_delay_seconds=1.0,
            start_round_wind=Seat.SOUTH,
            start_hand_number=4,
            start_honba=0,
            start_riichi_deposits=0,
            participant_scores=SOUTH_4_TEST_SCORES,
        )

    def create_configured_session() -> InteractiveMatchSession:
        assert configured_start is not None
        return InteractiveMatchSession(
            seed,
            profile,
            human_count=1,
            agent_delay_seconds=1.0,
            start_round_wind=configured_start.round_wind,
            start_hand_number=configured_start.hand_number,
            start_honba=configured_start.honba,
            start_riichi_deposits=configured_start.riichi_deposits,
            participant_scores=configured_start.participant_scores,
        )

    session = create_configured_session() if configured_start else create_session(seed)
    assert session.human_seat is not None
    return HandAppServer(
        ("127.0.0.1", port),
        session=session,
        human_seat=session.human_seat,
        rematch_factory=create_session,
        test_session_factories={SOUTH_4_TEST_ROUTE: create_south_4_test_session},
        completion_writer=lambda result: write_record(
            match_record_v03_document(
                result, app_version=producer["app_version"],
                build_revision=producer["build_revision"],
                engine_version=producer["engine_version"],
                source_session_id=source_session_id, rules=record_rules,
            ), log_path, allow_evaluation=True,
        ),
    )


def replay_html(match_log_path: Path, observer=AUTHORIZED_REPLAY_OBSERVER, *, allow_evaluation: bool = False) -> str:
    record = read_record(match_log_path, allow_evaluation=allow_evaluation)
    match_record_viewer_document(record, observer, allow_evaluation=allow_evaluation)
    return ui_document()


def replay_server(match_log_path: Path, port: int, observer=AUTHORIZED_REPLAY_OBSERVER, *, allow_evaluation: bool = False) -> HandAppServer:
    record = read_record(match_log_path, allow_evaluation=allow_evaluation)
    return HandAppServer(("127.0.0.1", port), replay_document=match_record_viewer_document(record, observer, allow_evaluation=allow_evaluation))


def main() -> None:
    parser = argparse.ArgumentParser(description="Play one Chombo east-south match")
    add_rule_arguments(parser)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path, default=Path("match.json"))
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--allow-evaluation-records", action="store_true",
                        help="explicitly allow development EVALUATION records with --replay")
    replay_access = parser.add_mutually_exclusive_group()
    replay_access.add_argument("--human-seat", choices=[seat.value for seat in Seat])
    replay_access.add_argument("--public-replay", action="store_true")
    parser.add_argument("--start-round", choices=("east", "south"))
    parser.add_argument("--start-hand", type=int, choices=range(1, 5))
    parser.add_argument("--honba", type=int, default=0)
    parser.add_argument("--riichi-deposits", type=int, default=0)
    parser.add_argument("--human-score", type=int)
    parser.add_argument("--chombo-scores", type=int, nargs=3)
    args = parser.parse_args()
    if args.allow_evaluation_records and args.replay is None:
        parser.error("--allow-evaluation-records requires --replay")
    if args.replay is not None:
        if args.rule_alias is not None or args.rule_ref is not None or args.rule_store is not None:
            parser.error("Replay uses recorded rules; rule selection is not allowed")
        observer = PUBLIC_OBSERVER if args.public_replay else human_observer(Seat(args.human_seat)) if args.human_seat else AUTHORIZED_REPLAY_OBSERVER
        server = replay_server(args.replay, args.port, observer, allow_evaluation=args.allow_evaluation_records)
        print(f"Chombo hanchan replay: http://127.0.0.1:{args.port}/", flush=True)
        server.serve_forever()
        return
    configured_values = (args.start_round, args.start_hand, args.human_score, args.chombo_scores)
    if any(value is not None for value in configured_values) and not all(
        value is not None for value in configured_values
    ):
        parser.error(
            "configured start requires --start-round, --start-hand, "
            "--human-score, and three --chombo-scores"
        )
    if args.honba < 0 or args.riichi_deposits < 0:
        parser.error("--honba and --riichi-deposits must be non-negative")
    configured_start = (
        ConfiguredMatchStart(
            Seat(args.start_round),
            args.start_hand,
            args.honba,
            args.riichi_deposits,
            args.human_score,
            tuple(args.chombo_scores),
        )
        if all(value is not None for value in configured_values)
        else None
    )
    seed = resolve_seed(args.seed)
    server = hanchan_server(seed, args.port, configured_start, args.log, selection=selection_from_arguments(args))
    print(
        f"Chombo hanchan app: http://127.0.0.1:{args.port}/ "
        f"(human={server.human_seat.value}, seed={seed})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
