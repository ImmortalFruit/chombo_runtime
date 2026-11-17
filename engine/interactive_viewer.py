"""Compatibility entry point for the unified hand application."""

from __future__ import annotations

import argparse
from pathlib import Path

from engine.hand_app import HandAppHandler as InteractiveViewerHandler
from engine.hand_app import HandAppServer, play_server
from engine.ui_compat import ui_document
from engine.seed import resolve_seed
from engine.rule_inputs import add_rule_arguments, selection_from_arguments


def interactive_html() -> str:
    """Compatibility wrapper around the shared play/replay UI."""
    return ui_document()


InteractiveViewerServer = HandAppServer


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Play one hand; prefer `python -m engine.hand_app play`"
    )
    add_rule_arguments(parser)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path, default=Path("hand.json"))
    args = parser.parse_args()
    seed = resolve_seed(args.seed)
    server = play_server(seed, args.port, args.log, selection=selection_from_arguments(args))
    print(
        f"Chombo hand app: http://127.0.0.1:{args.port}/ "
        f"(human={server.human_seat.value})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
