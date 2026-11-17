"""Unified local application for playing and replaying a Chombo hand."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from engine.game_state import Seat
from engine.canonical_record import (custom_rules_for_selection, development_producer,
                                     hand_record_document, hand_record_viewer_document,
                                     read_record, write_record)
from engine.ui_compat import serve_ui, ui_document
from engine.interactive_hand import InteractiveHandError, InteractiveHandSession, random_human_seat
from engine.observer_projection import AUTHORIZED_REPLAY_OBSERVER, PUBLIC_OBSERVER, Observer, human_observer
from engine.seed import resolve_seed
from engine.rule_inputs import RuleSelection, add_rule_arguments, load_selected_profile, selection_from_arguments


ROOT = Path(__file__).resolve().parents[1]


class HandAppServer(ThreadingHTTPServer):
    """Serve one play session or one already-projected replay document."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        replay_document: Mapping[str, Any] | None = None,
        session: Any | None = None,
        human_seat: Seat | None = None,
        log_path: Path | None = None,
        rematch_factory: Callable[[], Any] | None = None,
        test_session_factories: Mapping[str, Callable[[], Any]] | None = None,
        completion_writer: Callable[[Any], None] | None = None,
        hand_log_writer: Callable[[Any], None] | None = None,
    ):
        super().__init__(address, HandAppHandler)
        self.replay_document = replay_document
        self.session = session
        self.human_seat = human_seat
        self.log_path = log_path
        self.rematch_factory = rematch_factory
        self.test_session_factories = dict(test_session_factories or {})
        self.completion_writer = completion_writer
        self.hand_log_writer = hand_log_writer
        self.lock = threading.Lock()
        self.busy = False
        self.published_state = (
            session.snapshot(human_seat)
            if session is not None and human_seat is not None
            else None
        )
        if session is not None:
            session.on_event = self.publish

    def start_session(self, factory: Callable[[], Any] | None) -> None:
        if factory is None:
            raise InteractiveHandError("REMATCH_NOT_AVAILABLE", {})
        self.session = factory()
        self.human_seat = getattr(self.session, "human_seat", None)
        self.published_state = self.session.snapshot(self.human_seat)
        self.session.on_event = self.publish

    @property
    def is_play_mode(self) -> bool:
        return self.session is not None

    def publish(self) -> None:
        observer = self._active_human_seat()
        if self.session is not None and observer is not None:
            self.human_seat = observer
            self.published_state = self.session.snapshot(observer)

    def _active_human_seat(self) -> Seat | None:
        dynamic = getattr(self.session, "human_seat", None)
        return dynamic if dynamic is not None else self.human_seat

    def apply_action(self, action_id: str) -> None:
        assert self.session is not None
        observer = self._active_human_seat()
        assert observer is not None
        try:
            with self.lock:
                self.session.submit(observer, action_id)
                log = getattr(self.session, "log", None)
                if log is not None and self.log_path is not None:
                    if self.hand_log_writer is None:
                        raise InteractiveHandError("RECORD_WRITER_NOT_CONFIGURED", {})
                    self.hand_log_writer(log)
                result = getattr(self.session, "result", None)
                if result is not None and self.completion_writer is not None:
                    self.completion_writer(result)
                self.publish()
        finally:
            self.busy = False


class HandAppHandler(BaseHTTPRequestHandler):
    server: HandAppServer

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/rematch" and self.server.rematch_factory is not None:
            with self.server.lock:
                self.server.start_session(self.server.rematch_factory)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path in self.server.test_session_factories:
            with self.server.lock:
                self.server.start_session(
                    self.server.test_session_factories[self.path]
                )
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path == "/api/state" and self.server.is_play_mode:
            self._json(200, {**self.server.published_state, "processing": self.server.busy})
            return
        if self.path == "/index.view.json":
            self._json(200, {"mode": "play" if self.server.is_play_mode else "replay",
                             "replay": self.server.replay_document,
                             "format": "east_south" if hasattr(self.server.session, "current_hand") else "single_hand",
                             "rematch": self.server.rematch_factory is not None})
            return
        if serve_ui(self):
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/api/action" or not self.server.is_play_mode:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise InteractiveHandError("INVALID_ACTION_BODY", {})
            action_id = body.get("action_id")
            if not isinstance(action_id, str):
                raise InteractiveHandError("MISSING_ACTION_ID", {})
            if self.server.busy:
                raise InteractiveHandError("ACTION_IN_PROGRESS", {})
            legal_ids = {
                action["action_id"]
                for action in self.server.published_state["legal_actions"]
            }
            if action_id not in legal_ids:
                raise InteractiveHandError(
                    "ACTION_NOT_IN_LEGAL_SET", {"action_id": action_id}
                )
            self.server.busy = True
            threading.Thread(
                target=self.server.apply_action, args=(action_id,), daemon=True
            ).start()
            self._json(202, {"accepted": True})
        except (InteractiveHandError, json.JSONDecodeError) as error:
            self._json(400, {"error": getattr(error, "code", "INVALID_JSON")})

    def log_message(self, format: str, *args: Any) -> None:
        return


def play_server(seed: int, port: int, log_path: Path, *, selection: RuleSelection) -> HandAppServer:
    profile = load_selected_profile(selection, route="hand")
    producer = development_producer(ROOT)
    source_session_id = str(uuid4())
    record_rules = custom_rules_for_selection(selection)
    human = random_human_seat(seed)
    session = InteractiveHandSession(
        seed, profile, (human,), agent_delay_seconds=1.0
    )
    return HandAppServer(
        ("127.0.0.1", port),
        session=session,
        human_seat=human,
        log_path=log_path,
        hand_log_writer=lambda log: write_record(
            hand_record_document(log, app_version=producer["app_version"],
                                 build_revision=producer["build_revision"],
                                 engine_version=producer["engine_version"],
                                 source_session_id=source_session_id, rules=record_rules),
            log_path, allow_evaluation=True,
        ),
    )


def replay_server(
    hand_log_path: Path,
    port: int,
    observer: Observer = PUBLIC_OBSERVER,
    *,
    allow_evaluation: bool = False,
) -> HandAppServer:
    return HandAppServer(
        ("127.0.0.1", port),
        replay_document=replay_document(hand_log_path, observer, allow_evaluation=allow_evaluation),
    )


def replay_document(hand_log_path: Path, observer: Observer = PUBLIC_OBSERVER, *, allow_evaluation=False):
    document = read_record(hand_log_path, allow_evaluation=allow_evaluation)
    return hand_record_viewer_document(document, observer, allow_evaluation=allow_evaluation)


def replay_html(hand_log_path: Path, observer: Observer = PUBLIC_OBSERVER, *, allow_evaluation=False) -> str:
    """Compatibility reader: validate access; return the unmodified Nuxt shell."""
    replay_document(hand_log_path, observer, allow_evaluation=allow_evaluation)
    return ui_document()


def _observer(args: argparse.Namespace) -> Observer:
    if args.public_replay:
        return PUBLIC_OBSERVER
    if args.authorized_replay:
        return AUTHORIZED_REPLAY_OBSERVER
    if args.human_seat:
        return human_observer(Seat(args.human_seat))
    return AUTHORIZED_REPLAY_OBSERVER


def main() -> None:
    parser = argparse.ArgumentParser(description="Play or replay one Chombo hand")
    subcommands = parser.add_subparsers(dest="mode", required=True)
    play = subcommands.add_parser("play", help="play against three Chombo seats")
    add_rule_arguments(play)
    play.add_argument("--seed", type=int)
    play.add_argument("--port", type=int, default=8765)
    play.add_argument("--log", type=Path, default=Path("hand.json"))
    replay = subcommands.add_parser("replay", help="open a saved hand-log JSON")
    replay.add_argument("hand_log", type=Path)
    replay.add_argument("--port", type=int, default=8765)
    access = replay.add_mutually_exclusive_group()
    access.add_argument("--human-seat", choices=[seat.value for seat in Seat])
    access.add_argument("--authorized-replay", action="store_true")
    access.add_argument("--public-replay", action="store_true")
    replay.add_argument("--evaluation-record", action="store_true")
    args = parser.parse_args()

    if args.mode == "play":
        seed = resolve_seed(args.seed)
        server = play_server(seed, args.port, args.log, selection=selection_from_arguments(args))
        detail = f"human={server.human_seat.value}"
    else:
        server = replay_server(args.hand_log, args.port, _observer(args),
                               allow_evaluation=args.evaluation_record)
        detail = f"log={args.hand_log}"
    print(f"Chombo hand app: http://127.0.0.1:{args.port}/ ({detail})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
