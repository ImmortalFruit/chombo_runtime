"""Local four-Agent observation; presentation never controls match progress."""
from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from time import monotonic
from typing import Any

from engine.ui_compat import serve_ui
from engine.interactive_hand import InteractiveHandSession
from engine.match_orchestrator import InteractiveMatchSession
from engine.rule_inputs import add_rule_arguments, load_selected_profile, selection_from_arguments
from engine.seed import resolve_seed


class AutonomousViewerServer(ThreadingHTTPServer):
    """Keep one latest projection, without frame queues or browser acknowledgments."""

    def __init__(self, address, session, *, display: str = "closed"):
        if display not in {"closed", "open"}:
            raise ValueError("INVALID_DISPLAY_MODE")
        hand = getattr(session, "current_hand", session)
        if hand.human_seats:
            raise ValueError("SPECTATOR_REQUIRES_ALL_AGENTS")
        super().__init__(address, AutonomousViewerHandler)
        self.session = session
        self.display = display
        self.state_lock = threading.Lock()
        self.done = threading.Event()
        self.worker: threading.Thread | None = None
        self.error: str | None = None
        self._next_publication = 0.0
        self.published_state: dict[str, Any] = {}
        self.publish(force=True)
        session.on_event = self.publish

    def publish(self, *, force: bool = False) -> None:
        # Snapshot work is bounded; slow/absent HTTP consumers never queue frames.
        now = monotonic()
        if not force and now < self._next_publication:
            return
        value = self.session.spectator_snapshot(reveal_all=self.display == "open")
        with self.state_lock:
            self.published_state = value
        self._next_publication = now + 0.25

    def state(self) -> dict[str, Any]:
        with self.state_lock:
            return deepcopy(self.published_state) | {
                "processing": not self.done.is_set(), "error": self.error,
                "display": self.display,
            }

    def run(self) -> None:
        try:
            while not self.session.complete:
                self.session.advance()
        except Exception as error:
            self.error = getattr(error, "code", type(error).__name__)
        finally:
            try:
                self.publish(force=True)
            finally:
                self.done.set()

    def start(self) -> None:
        if self.worker is not None:
            raise RuntimeError("AUTONOMOUS_SESSION_ALREADY_STARTED")
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()


class AutonomousViewerHandler(BaseHTTPRequestHandler):
    server: AutonomousViewerServer

    def do_GET(self) -> None:
        if self.path == "/api/state":
            payload = json.dumps(self.server.state(), ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        elif self.path == "/index.view.json":
            payload = json.dumps({"mode": "spectate", "display": self.server.display,
                                  "format": "east_south" if isinstance(self.server.session, InteractiveMatchSession) else "single_hand"}).encode()
            content_type = "application/json; charset=utf-8"
        elif serve_ui(self):
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe four Chombo agents without display waits")
    add_rule_arguments(parser)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--format", choices=("single_hand", "east_south"), default="single_hand")
    parser.add_argument("--display", choices=("closed", "open"), default="closed")
    args = parser.parse_args()
    profile = load_selected_profile(
        selection_from_arguments(args), route="hand" if args.format == "single_hand" else "match"
    )
    seed = resolve_seed(args.seed)
    session = (
        InteractiveHandSession(seed, profile, (), auto_advance=False, agent_delay_seconds=1.0)
        if args.format == "single_hand" else
        InteractiveMatchSession(seed, profile, human_count=0, auto_advance=False, agent_delay_seconds=1.0)
    )
    server = AutonomousViewerServer(("127.0.0.1", args.port), session, display=args.display)
    server.start()
    print(f"Chombo observation: http://127.0.0.1:{server.server_port}/ (seed={seed}, display={args.display})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
