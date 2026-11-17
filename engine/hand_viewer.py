"""Observer-safe replay model and compatibility HTML renderer."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine.game_state import Seat
from engine.ui_compat import ui_document, export_view
from engine.hand_ui import HandUIError, embedded_assets
from engine.observer_projection import AUTHORIZED_REPLAY_OBSERVER, PUBLIC_OBSERVER, Observer, human_observer, project_hand_log


class HandViewerError(ValueError):
    def __init__(self, code: str, details: Mapping[str, Any]):
        self.code, self.details = code, dict(details)
        super().__init__(f"{code}: {self.details}")


@dataclass(frozen=True)
class ViewerFrame:
    sequence: int
    event_id: str
    kind: str
    actor: str | None
    description: str
    state: Mapping[str, Any]
    result: Mapping[str, Any] | None = None
    revealed_seats: tuple[str, ...] = ()
    win_detail: Mapping[str, Any] | None = None
    win_details: tuple[Mapping[str, Any], ...] = ()
    sound_event: Mapping[str, Any] | None = None
    visible_waits_by_seat: Mapping[str, Any] | None = None
    special_result_family: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "event_id": self.event_id, "kind": self.kind,
                "actor": self.actor, "description": self.description,
                "state": deepcopy(dict(self.state)),
                "result": deepcopy(dict(self.result)) if self.result is not None else None,
                "revealed_seats": list(self.revealed_seats),
                "win_detail": deepcopy(dict(self.win_detail)) if self.win_detail is not None else None,
                "win_details": [deepcopy(dict(item)) for item in self.win_details],
                "sound_event": deepcopy(dict(self.sound_event)) if self.sound_event is not None else None,
                "visible_waits_by_seat": deepcopy(dict(self.visible_waits_by_seat)) if self.visible_waits_by_seat is not None else {},
                "special_result_family": self.special_result_family}


def _describe(event: Mapping[str, Any]) -> str:
    data, kind, actor = event.get("public_data", {}), event["kind"], event.get("actor")
    if kind == "hand_started": return "一局を開始"
    if kind == "action_applied": return f"{actor}: {data.get('kind', 'action')}"
    if kind == "response_resolved":
        adopted = data.get("adopted_actions", [])
        return "応答なし" if not adopted else "応答確定: " + ", ".join(item["kind"] for item in adopted)
    if kind == "tenpai_declared": return f"{actor}: {data.get('declaration', 'noten')}"
    if kind == "hand_ended": return f"終局: {data.get('reason', 'unknown')}"
    if kind == "settlement_applied": return "局精算を反映"
    return str(kind)


class HandViewerModel:
    """Common cursor model. Live callers append; replay callers preload and seek."""
    def __init__(self, initial_public_state: Mapping[str, Any]):
        self._initial_state = deepcopy(dict(initial_public_state))
        self._frames: list[ViewerFrame] = []
        self._cursor = -1
        self._revealed_seats: set[str] = set()
        self._win_detail: Mapping[str, Any] | None = None
        self._special_hand_end: dict[str, Any] | None = None

    @classmethod
    def replay(cls, document: Mapping[str, Any]) -> "HandViewerModel":
        model = cls(document["initial_public_state"])
        events = document.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise HandViewerError("INVALID_EVENT_LOG", {})
        for event in events: model.append_confirmed_event(event)
        model.first()
        return model

    @property
    def frames(self) -> tuple[ViewerFrame, ...]: return tuple(self._frames)
    @property
    def cursor(self) -> int: return self._cursor
    @property
    def current(self) -> ViewerFrame | None: return self._frames[self._cursor] if self._cursor >= 0 else None

    def append_confirmed_event(self, event: Mapping[str, Any]) -> ViewerFrame:
        expected = len(self._frames)
        if event.get("sequence") != expected:
            raise HandViewerError("NONCONTIGUOUS_LIVE_EVENT", {"expected": expected, "actual": event.get("sequence")})
        public_data = event.get("public_data")
        if not isinstance(public_data, Mapping): raise HandViewerError("INVALID_PUBLIC_EVENT", {"sequence": expected})
        state = public_data.get("state")
        if state is None: state = self._frames[-1].state if self._frames else self._initial_state
        if not isinstance(state, Mapping): raise HandViewerError("INVALID_PUBLIC_STATE", {"sequence": expected})
        result = None
        if event.get("kind") == "settlement_applied":
            result = {"point_deltas": deepcopy(public_data.get("point_deltas", {})), "final_scores": deepcopy(public_data.get("final_scores", {}))}
            if self._special_hand_end is not None:
                special = deepcopy(self._special_hand_end)
                special.update(result)
                result["end_reason"] = special["reason"]
                result["winners"] = deepcopy(special["qualifier_seats"])
                result["special_hand_end"] = special
        if (
            event.get("kind") == "tenpai_declared"
            and public_data.get("declaration") == "tenpai"
            and event.get("actor") is not None
        ):
            self._revealed_seats.add(str(event["actor"]))
        if event.get("kind") == "hand_ended":
            detail = public_data.get("win_detail")
            self._win_detail = deepcopy(dict(detail)) if isinstance(detail, Mapping) else None
            self._revealed_seats.update(str(seat) for seat in public_data.get("winners", []))
            self._revealed_seats.update(
                str(seat) for seat in public_data.get("declared_tenpai_player_ids", [])
            )
            if public_data.get("special_result_family"):
                self._special_hand_end = {
                    "family": str(public_data["special_result_family"]),
                    "reason": str(public_data.get("reason", "")),
                    "qualifier_seats": deepcopy(public_data.get("qualifier_seats", [])),
                }
        sound_event = {
            "event_id": str(event.get("event_id", "")),
            "kind": str(event.get("kind", "")),
            "actor": event.get("actor"),
            "action_kind": public_data.get("kind"),
            "adopted_actions": deepcopy(public_data.get("adopted_actions", [])),
            "reason": public_data.get("reason"),
            "declared_tenpai_player_ids": deepcopy(
                public_data.get("declared_tenpai_player_ids", [])
            ),
            "declaration": public_data.get("declaration"),
        }
        waits = public_data.get("visible_waits_by_seat", {})
        details = tuple(deepcopy(dict(item)) for item in public_data.get("win_details", []) if isinstance(item, Mapping))
        frame = ViewerFrame(
            expected, str(event.get("event_id", "")), str(event.get("kind", "")),
            event.get("actor"), _describe(event), deepcopy(dict(state)), result,
            tuple(sorted(self._revealed_seats)), self._win_detail, details,
            sound_event, deepcopy(dict(waits)) if isinstance(waits, Mapping) else {},
            str(public_data["special_result_family"])
            if "special_result_family" in public_data else None,
        )
        self._frames.append(frame); self._cursor = expected
        return frame

    def seek(self, sequence: int) -> ViewerFrame:
        if not 0 <= sequence < len(self._frames): raise HandViewerError("CURSOR_OUT_OF_RANGE", {"sequence": sequence})
        self._cursor = sequence; return self._frames[sequence]
    def first(self) -> ViewerFrame: return self.seek(0)
    def last(self) -> ViewerFrame: return self.seek(len(self._frames) - 1)
    def previous(self) -> ViewerFrame: return self.seek(max(0, self._cursor - 1))
    def next(self) -> ViewerFrame: return self.seek(min(len(self._frames) - 1, self._cursor + 1))


def viewer_document(hand_document: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER) -> dict[str, Any]:
    projected = project_hand_log(hand_document, observer)
    model = HandViewerModel.replay(projected)
    frames = [frame.to_dict() for frame in model.frames]
    for frame in frames:
        special = (frame.get("result") or {}).get("special_hand_end")
        if special is not None:
            special["rule_profile_id"] = projected.get("rule_profile_id", "")
            special["rule_snapshot_id"] = projected.get("rule_snapshot_id", "")
    return {"hand_id": hand_document.get("hand_id", ""), "schema_version": hand_document.get("schema_version", ""),
            "observer": projected["observer"],
            "participants": deepcopy(hand_document.get("participants", [])),
            "seat_assignments": deepcopy(hand_document.get("seat_assignments", {})),
            "rule_profile_id": projected.get("rule_profile_id", ""),
            "rule_snapshot_id": projected.get("rule_snapshot_id", ""),
            "frames": frames}


def _embedded_tile_assets() -> dict[str, str]:
    """Compatibility alias for callers predating the shared UI."""
    try: return embedded_assets()
    except HandUIError as error: raise HandViewerError(error.code, error.details) from error


def render_viewer_html(hand_document: Mapping[str, Any], observer: Observer = PUBLIC_OBSERVER) -> str:
    """Compatibility export; the application now opens the JSON directly."""
    viewer_document(hand_document, observer)
    return ui_document()


def write_viewer_html(hand_document: Mapping[str, Any], path: Path, observer: Observer = PUBLIC_OBSERVER) -> None:
    """Deprecated compatibility writer. Prefer ``python -m engine.hand_app replay``."""
    export_view(path, {"mode": "replay", "replay": viewer_document(hand_document, observer)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Compatibility HTML export; prefer `python -m engine.hand_app replay LOG`")
    parser.add_argument("hand_log", type=Path); parser.add_argument("output", type=Path)
    access = parser.add_mutually_exclusive_group(); access.add_argument("--human-seat", choices=[seat.value for seat in Seat]); access.add_argument("--authorized-replay", action="store_true"); access.add_argument("--public-replay", action="store_true")
    args = parser.parse_args(); document = json.loads(args.hand_log.read_text(encoding="utf-8"))
    observer = PUBLIC_OBSERVER if args.public_replay else human_observer(Seat(args.human_seat)) if args.human_seat else AUTHORIZED_REPLAY_OBSERVER
    write_viewer_html(document, args.output, observer)


if __name__ == "__main__": main()
