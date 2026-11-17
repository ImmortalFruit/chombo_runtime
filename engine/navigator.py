"""Navigator start flow and durable, observer-safe live checkpoints (U3-04)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4
from engine.operation_journal import OperationJournal
from engine.recovery_classifier import StartupRecoveryClassifier

from engine.canonical_record import (CanonicalRecordError, hand_record_document,
                                     hand_record_viewer_document, match_record_v03_document,
                                     match_record_viewer_document, read_record_bytes,
                                     write_record)
from engine.condition_navigator import condition_tool_model
from engine.game_state import Seat, GameState
from engine.hand_log import HandEvent
from engine.hand_orchestrator import _authoritative_state
from engine.interactive_hand import InteractiveHandSession, random_human_seat
from engine.match_orchestrator import InteractiveMatchSession, NEXT_HAND_ACTION_ID
from engine.observer_projection import AUTHORIZED_REPLAY_OBSERVER, PUBLIC_OBSERVER, human_observer
from engine.position_conversation import PositionConversationGate
from engine.application import Application
from engine.codex_adapter import CodexAdapter
from engine.codex_schema import openapi_document as codex_openapi_document
from engine.rule_drafts import DraftStore, DraftValidator
from engine.rule_editor import (
    RuleEditorSession,
    rule_management_model,
)
from engine.rule_inputs import RuleSelection, load_selected_profile
from engine.rule_profiles import ResolvedRuleProfile, RuleProfileError
from engine.seed import resolve_seed
from engine.setup import create_initial_state


CHECKPOINT_SCHEMA_ID = "https://chombo.local/schemas/chombo.session_checkpoint.v0.3.schema.json"
CHECKPOINT_FORMAT_VERSION = "1.0.0"
CHECKPOINT_V031_SCHEMA_ID = "https://chombo.local/schemas/chombo.session_checkpoint.v0.3.1.schema.json"
CHECKPOINT_V031_FORMAT_VERSION = "1.1.0"
INDEX_SCHEMA_VERSION = 1
MAX_CHECKPOINTS = 256
MAX_OPERATIONS = 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 64
ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = (
    _RUNTIME_ROOT / "assets" / "chombo-icon.png"
    if (_RUNTIME_ROOT / "assets" / "chombo-icon.png").is_file()
    else _RUNTIME_ROOT / "docs" / "assets" / "chombo-icon.png"
)


class NavigatorError(RuntimeError):
    def __init__(self, code: str, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {self.details}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: Any) -> Any:
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, Mapping):
        return {str(key.value if isinstance(key, Seat) else key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise NavigatorError("CHECKPOINT_STATE_UNSUPPORTED", {"type": type(value).__name__})


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise NavigatorError("CHECKPOINT_STATE_UNSUPPORTED") from error


def _hash_document(document: Mapping[str, Any]) -> str:
    body = deepcopy(dict(document))
    body.get("integrity", {}).pop("document_hash", None)
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


def _strict_json_file(path: Path) -> Any:
    try:
        payload = path.read_bytes()
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise NavigatorError("RESOURCE_LIMIT", {"path": str(path)})
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise NavigatorError("DUPLICATE_JSON_KEY", {"key": key})
                value[key] = item
            return value
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda item: (_ for _ in ()).throw(NavigatorError("INVALID_JSON_NUMBER", {"value": item})))
    except NavigatorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NavigatorError("CHECKPOINT_READ_FAILED", {"path": str(path)}) from error
    def depth(item, level=0):
        if level > MAX_JSON_DEPTH:
            raise NavigatorError("RESOURCE_LIMIT", {"reason": "JSON_DEPTH"})
        if isinstance(item, dict):
            for child in item.values(): depth(child, level + 1)
        elif isinstance(item, list):
            for child in item: depth(child, level + 1)
    depth(value)
    return value


def _fork_engine(engine, *, auto_advance=True):
    """Isolate a transaction from the committed state without replaying its history.

    Frozen rule/state/event values are safe to share. Mutable containers and
    mapping-proxy contents are copied, including controller and random state.
    No process-global deepcopy hooks or live callbacks are changed.
    """
    memo = {id(engine.profile): engine.profile}
    visited = set()
    def prepare(value):
        if value is None or isinstance(value, (str, bytes, int, float, bool, Enum, type)) or callable(value):
            return
        identity = id(value)
        if identity in memo or identity in visited:
            return
        visited.add(identity)
        if isinstance(value, (GameState, HandEvent)):
            memo[identity] = value
        elif isinstance(value, MappingProxyType):
            for key, child in value.items():
                prepare(key); prepare(child)
            memo[identity] = MappingProxyType({deepcopy(key, memo): deepcopy(child, memo) for key, child in value.items()})
        elif isinstance(value, dict):
            for key, child in value.items():
                prepare(key); prepare(child)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                prepare(child)
        elif hasattr(value, "__dict__"):
            for child in vars(value).values():
                prepare(child)
    prepare(engine)
    fork = deepcopy(engine, memo)
    fork.on_event = None
    fork.agent_delay_seconds = 0
    if isinstance(fork, InteractiveMatchSession):
        fork.auto_advance = auto_advance
        fork.current_hand.on_event = fork._publish
        fork.current_hand.agent_delay_seconds = 0
    return fork


def _state_digest(session: Any) -> str:
    if isinstance(session, LiveSession):
        session = session.engine
    hand = session.current_hand if isinstance(session, InteractiveMatchSession) else session
    state = {
        "game_state": _authoritative_state(hand.state),
        "context": _plain(hand.context.__dict__),
        "decision_count": hand.decision_count,
        "event_count": len(hand.events),
        "events": [_plain(event.__dict__) for event in hand.events],
        "pending_action_ids": [action.action_id for item in hand.pending for action in item.actions],
        "completed_hands": [log.to_dict() for log in getattr(session, "hand_logs", ())],
        "transitions": [_plain(item.__dict__) for item in getattr(session, "transitions", ())],
        "pending_next": getattr(session, "_pending_next", None) is not None,
    }
    return "sha256:" + hashlib.sha256(_canonical(state)).hexdigest()


@dataclass(frozen=True)
class StartPosition:
    round_wind: Seat = Seat.EAST
    hand_number: int = 1
    honba: int = 0
    riichi_deposits: int = 0
    human_seat: Seat | None = None
    seat_scores: Mapping[Seat, int] | None = None

    def is_mid_match_start(self) -> bool:
        """Return whether play was configured to begin after the normal opening."""
        return (
            self.round_wind != Seat.EAST
            or self.hand_number != 1
            or self.honba != 0
            or self.riichi_deposits != 0
            or self.seat_scores is not None
        )

    def validate(self) -> None:
        if self.round_wind not in {Seat.EAST, Seat.SOUTH}:
            raise NavigatorError("INVALID_START_POSITION", {"round_wind": self.round_wind.value})
        if not isinstance(self.hand_number, int) or isinstance(self.hand_number, bool) or not 1 <= self.hand_number <= 4:
            raise NavigatorError("INVALID_START_POSITION", {"hand_number": self.hand_number})
        if not isinstance(self.honba, int) or isinstance(self.honba, bool) or not 0 <= self.honba <= 99:
            raise NavigatorError("INVALID_START_POSITION", {"honba": self.honba})
        if not isinstance(self.riichi_deposits, int) or isinstance(self.riichi_deposits, bool) or not 0 <= self.riichi_deposits <= 100_000 or self.riichi_deposits % 1000:
            raise NavigatorError("INVALID_START_POSITION", {"riichi_deposits": self.riichi_deposits})
        if self.human_seat is not None and self.human_seat not in set(Seat):
            raise NavigatorError("INVALID_START_POSITION", {"human_seat": str(self.human_seat)})
        if self.seat_scores is not None and (
            set(self.seat_scores) != set(Seat)
            or any(not isinstance(score, int) or isinstance(score, bool) or score % 100 or not -1_000_000 <= score <= 10_000_000
                   for score in self.seat_scores.values())
        ):
            raise NavigatorError("INVALID_START_POSITION", {"seat_scores": _plain(self.seat_scores)})

    def to_dict(self) -> dict[str, Any]:
        return {"round_wind": self.round_wind.value, "hand_number": self.hand_number, "honba": self.honba,
                "riichi_deposits": self.riichi_deposits, "human_seat": self.human_seat.value if self.human_seat else None,
                "seat_scores": _plain(self.seat_scores) if self.seat_scores is not None else None}


@dataclass(frozen=True)
class StartRequest:
    request_id: str
    format: str
    selection: RuleSelection
    seed: int | None = None
    position: StartPosition = field(default_factory=StartPosition)


@dataclass
class LiveSession:
    session_id: str
    lineage_id: str
    record_id: str
    format: str
    profile: ResolvedRuleProfile
    engine: InteractiveHandSession | InteractiveMatchSession
    start_position: StartPosition
    owner_id: str
    input_ledger: list[dict[str, Any]]
    parent_checkpoint_id: str | None = None
    boundary_revision: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def human_seat(self) -> Seat:
        seat = self.engine.human_seat if isinstance(self.engine, InteractiveMatchSession) else next(iter(self.engine.human_seats))
        if seat is None:
            raise NavigatorError("HUMAN_SEAT_MISSING")
        return seat

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            value = self.engine.snapshot(self.human_seat)
            return value | {
                "session_id": self.session_id,
                "lineage_id": self.lineage_id,
                "rule_profile_id": self.profile.profile_id,
                "rule_snapshot_id": self.profile.snapshot_id,
                "format": self.format,
                "started_mid_match": self.start_position.is_mid_match_start(),
            }

    def submit(self, request_id: str, action_id: str) -> dict[str, Any]:
        with self.lock:
            previous = next((item for item in self.input_ledger if item["request_id"] == request_id), None)
            if previous is not None:
                if previous["action_id"] != action_id:
                    raise NavigatorError("REQUEST_ID_CONFLICT", {"request_id": request_id})
                return deepcopy(previous["result"])
            legal = {item["action_id"] for item in self.snapshot()["legal_actions"]}
            if action_id not in legal:
                raise NavigatorError("ACTION_NOT_IN_LEGAL_SET", {"action_id": action_id})
            self.engine.submit(self.human_seat, action_id)
            self.boundary_revision += 1
            result = {"accepted": True, "boundary_revision": self.boundary_revision}
            self.input_ledger.append({"request_id": request_id, "action_id": action_id, "result": result})
            return deepcopy(result)


@dataclass
class ReplaySession:
    """One validated immutable record and its Navigator-local viewing position."""

    filename: str
    record: Mapping[str, Any]
    hand_index: int = 0
    event_index: int = 0
    display_access: str = "ALL_HANDS"
    _viewer_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    @property
    def hand_count(self) -> int:
        return 1 if self.record["document_kind"] == "HAND_RECORD" else len(self.record["payload"]["hands"])

    def select_hand(self, hand_index: int) -> None:
        if not isinstance(hand_index, int) or isinstance(hand_index, bool) or not 0 <= hand_index < self.hand_count:
            raise NavigatorError("REPLAY_HAND_OUT_OF_RANGE", {"hand_index": hand_index})
        self.hand_index = hand_index
        self.event_index = 0

    def seek(self, event_index: int) -> None:
        frames = self._current_hand_view()["frames"]
        if not isinstance(event_index, int) or isinstance(event_index, bool) or not 0 <= event_index < len(frames):
            raise NavigatorError("REPLAY_EVENT_OUT_OF_RANGE", {"event_index": event_index})
        self.event_index = event_index

    def current_hand(self, observer=None) -> dict[str, Any]:
        return deepcopy(self._current_hand_view(observer))

    def _current_hand_view(self, observer=None) -> dict[str, Any]:
        """Internal cached view; callers must copy any data they expose."""
        if observer is None:
            observer = AUTHORIZED_REPLAY_OBSERVER if self.display_access == "ALL_HANDS" else PUBLIC_OBSERVER
        key = (observer.kind.value, observer.seat.value if observer.seat is not None else "")
        projected = self._viewer_cache.get(key)
        if projected is None:
            if self.record["document_kind"] == "HAND_RECORD":
                projected = {"hands": [hand_record_viewer_document(
                    self.record, observer, allow_evaluation=True
                )]}
            else:
                projected = match_record_viewer_document(
                    self.record, observer, allow_evaluation=True
                )
            self._viewer_cache[key] = projected
        return projected["hands"][self.hand_index]

    def display_snapshot(self, perspective: str, reveal_all: bool) -> dict[str, Any]:
        """Project the existing viewer frame; display access never grants analysis."""
        if reveal_all and self.display_access != "ALL_HANDS":
            raise NavigatorError("PERMISSION_DENIED")
        hand = self._current_hand_view()
        frame = deepcopy(hand["frames"][self.event_index])
        state = frame["state"]
        revealed = frame.get("revealed_seats", [])
        terminal = frame["kind"] in {"hand_ended", "settlement_applied"}
        hidden_noten = terminal and not frame.get("win_detail") and perspective not in revealed
        for seat, player in state["seats"].items():
            if not reveal_all and (seat != perspective and seat not in revealed or seat == perspective and hidden_noten):
                player.pop("concealed_tiles", None)
        waits = {seat: value for seat, value in frame.get("visible_waits_by_seat", {}).items()
                 if state["seats"][seat].get("concealed_tiles")}
        win = frame.get("win_detail")
        draw_seat = (win["winner"] if win and win["method"] == "tsumo" else
                     frame.get("actor") if frame["kind"] == "action_applied" and
                     frame["description"].endswith((": draw", ": tsumo")) else None)
        tiles = state["seats"].get(draw_seat, {}).get("concealed_tiles", [])
        raw_hands = ([self.record["payload"]["hand"]] if self.hand_count == 1 and self.record["document_kind"] == "HAND_RECORD"
                     else self.record["payload"]["hands"])
        hands = [{"hand_index": index, **{key: raw["authoritative_initial_state"][key]
                  for key in ("round_wind", "hand_number", "honba")}} for index, raw in enumerate(raw_hands)]
        result = self.metadata()["result"]
        snapshot = {**frame, "hand_id": hand["hand_id"], "human_seat": perspective,
            "format": self.metadata()["format"], "complete": terminal, "event_count": self.event_index,
            "started_mid_match": False, "legal_actions": [], "visible_waits_by_seat": waits,
            "result_frame_index": next((index for index in range(self.event_index, len(hand["frames"])) if hand["frames"][index].get("result")), None),
            "sound_events": [frame["sound_event"]] if frame.get("sound_event") else [],
            "participants": hand.get("participants", self.record["participants"]),
            "seat_assignments": hand["seat_assignments"], "match_transition": hand.get("transition_after"),
            "drawn_tile_seat": draw_seat, "drawn_tile_id": (win["winning_tile_id"] if win and draw_seat else tiles[-1]["tile_id"] if tiles else None),
            "match": {"complete": terminal and self.hand_index == self.hand_count - 1,
                      "final_points": result.get("players") if result and "players" in result else None,
                      "remaining_riichi_deposits": result.get("remaining_riichi_deposits", 0) if result else 0}}
        return {"snapshot": snapshot, "hands": hands}

    def metadata(self) -> dict[str, Any]:
        ref = self.record["rules"]["selected_ref"]
        frames = self._current_hand_view()["frames"]
        payload = self.record["payload"]
        return {
            "filename": self.filename, "document_kind": self.record["document_kind"],
            "format": "single_hand" if self.record["document_kind"] == "HAND_RECORD" else payload["format"],
            "format_version": self.record["format_version"], "app_version": self.record["producer"]["app_version"],
            "origin_kind": self.record["origin_kind"], "mutation_policy": self.record["mutation_policy"],
            "rule_profile_id": ref["profile_id"], "rule_version": ref.get("ruleset_version") or ref.get("version"),
            "rule_snapshot_id": ref["snapshot_id"], "observer": self.display_access,
            "hand_index": self.hand_index, "hand_count": self.hand_count,
            "event_index": self.event_index, "event_count": len(frames),
            "result": deepcopy(payload.get("result") if self.record["document_kind"] == "MATCH_RECORD" else payload["hand"].get("result")),
        }


class CheckpointStore:
    """Crash-resilient immutable bodies plus a generation-checked index."""

    def __init__(self, directory: Path, *, max_checkpoints: int = MAX_CHECKPOINTS, max_total_bytes: int = MAX_TOTAL_BYTES, producer: Mapping[str, Any] | None = None):
        self.root = Path(directory)
        self.checkpoints = self.root / "checkpoints"
        self.tombstones = self.root / "tombstones"
        self.restore_requests = self.root / "restore-requests"
        self.recovery_requests = self.root / "recovery-requests"
        self.diagnostics = self.root / "recovery-diagnostics"
        self.records = self.root / "records"
        self.index_path = self.root / "index.json"
        self.max_checkpoints = max_checkpoints
        self.max_total_bytes = max_total_bytes
        self.lock = threading.RLock()
        if producer is None:
            app_version = "0.3.0-dev.u3-04"
            source_root = Path(__file__).resolve().parents[1]
            try:
                revision = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=source_root,
                    check=True, capture_output=True, text=True, timeout=5,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                try:
                    manifest = json.loads((source_root / "release-manifest.json").read_text(encoding="utf-8"))
                    revision = manifest["source_revision"]
                    app_version = manifest["version"]
                except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                    raise NavigatorError("BUILD_METADATA_UNAVAILABLE") from error
            if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise NavigatorError("BUILD_METADATA_UNAVAILABLE")
            self.producer = {"app_id": "chombo", "app_version": app_version, "build_revision": revision, "engine_version": "chombo-engine.v0.3"}
        else:
            if (not isinstance(producer, Mapping) or
                    set(producer) != {"app_id", "app_version", "build_revision", "engine_version"} or
                    producer["app_id"] != "chombo" or
                    not all(isinstance(value, str) and value for value in producer.values()) or
                    re.fullmatch(r"[0-9a-f]{40}", producer["build_revision"]) is None):
                raise NavigatorError("BUILD_METADATA_UNAVAILABLE")
            self.producer = dict(producer)
        for path in (self.root, self.checkpoints, self.tombstones, self.restore_requests,
                     self.recovery_requests, self.diagnostics, self.records):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.recovery = StartupRecoveryClassifier(self)
        self._recover()

    error = NavigatorError
    utc_now = staticmethod(_utc_now)
    state_digest = staticmethod(_state_digest)
    hash_document = staticmethod(_hash_document)
    _strict_read = staticmethod(_strict_json_file)

    @staticmethod
    def _valid_id(value: str) -> bool:
        return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None

    def _write(self, path: Path, document: Mapping[str, Any]) -> None:
        payload = _canonical(document) + b"\n"
        # A fitting restore-request path must not grow by its full filename again.
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _empty_index(self) -> dict[str, Any]:
        value = {"schema_version": 1, "generation": 0, "entries": [], "integrity": {"algorithm": "canonical-json-sha256.v1"}}
        value["integrity"]["document_hash"] = _hash_document(value)
        return value

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_index()
        try:
            value = _strict_json_file(self.index_path)
            if value.get("schema_version") != 1 or value.get("integrity", {}).get("document_hash") != _hash_document(value):
                raise ValueError
            return value
        except (OSError, json.JSONDecodeError, ValueError):
            return self._empty_index()

    def _tombstoned(self) -> set[str]:
        return {path.stem for path in self.tombstones.glob("*.json") if self._valid_id(path.stem)}

    def _read_body(self, checkpoint_id: str) -> dict[str, Any]:
        if not self._valid_id(checkpoint_id):
            raise NavigatorError("INVALID_CHECKPOINT_ID")
        path = self.checkpoints / f"{checkpoint_id}.json"
        try:
            document = _strict_json_file(path)
        except NavigatorError as error:
            error.details.setdefault("checkpoint_id", checkpoint_id)
            raise
        required = {
            "document_kind", "schema_id", "format_version", "origin_kind", "record_state", "mutation_policy",
            "producer", "created_at", "checkpoint_id", "session_id", "lineage_id", "parent_checkpoint_id",
            "display_name", "save_kind", "position", "rules", "participants", "session", "game_state",
            "action_context", "hand_execution", "random_state", "controllers", "access_binding",
            "request_ledger", "integrity",
        }
        if document.get("schema_id") == CHECKPOINT_V031_SCHEMA_ID:
            required |= {"operation_journal", "outbox"}
        if not isinstance(document, dict) or set(document) != required:
            raise NavigatorError("CHECKPOINT_SCHEMA_INVALID", {"checkpoint_id": checkpoint_id})
        if document.get("checkpoint_id") != checkpoint_id or any(
            not self._valid_id(document.get(key, "")) for key in ("checkpoint_id", "session_id", "lineage_id")
        ) or (document.get("parent_checkpoint_id") is not None and not self._valid_id(document["parent_checkpoint_id"])):
            raise NavigatorError("CHECKPOINT_SCHEMA_INVALID", {"checkpoint_id": checkpoint_id})
        if document.get("integrity", {}).get("document_hash") != _hash_document(document):
            raise NavigatorError("CHECKPOINT_INTEGRITY_MISMATCH", {"checkpoint_id": checkpoint_id})
        return document

    def _recover(self) -> None:
        with self.lock:
            index = self._read_index()
            indexed = {entry["checkpoint_id"]: entry for entry in index["entries"] if self._valid_id(entry.get("checkpoint_id"))}
            tombstoned = self._tombstoned()
            changed = False
            for checkpoint_id in tuple(indexed):
                if checkpoint_id in tombstoned or not (self.checkpoints / f"{checkpoint_id}.json").is_file():
                    indexed.pop(checkpoint_id)
                    changed = True
                    continue
                try:
                    body = self._read_body(checkpoint_id)
                    if (
                        indexed[checkpoint_id].get("body_hash") != body["integrity"]["document_hash"]
                        or (body.get("schema_id") == CHECKPOINT_V031_SCHEMA_ID and not self._committed_checkpoint(body))
                    ):
                        raise NavigatorError("CHECKPOINT_NOT_RECOVERABLE")
                except NavigatorError:
                    indexed.pop(checkpoint_id)
                    changed = True
            for path in self.checkpoints.glob("*.json"):
                checkpoint_id = path.stem
                if checkpoint_id in indexed or checkpoint_id in tombstoned or not self._valid_id(checkpoint_id):
                    continue
                try:
                    body = self._read_body(checkpoint_id)
                except NavigatorError:
                    continue
                if body.get("schema_id") == CHECKPOINT_V031_SCHEMA_ID and not self._committed_checkpoint(body):
                    continue
                indexed[checkpoint_id] = self._entry(body, body.get("display_name") or "Recovered checkpoint", body.get("save_kind", "AUTO"))
                changed = True
            if changed or not self.index_path.exists():
                new_index = {"schema_version": 1, "generation": index["generation"] + 1, "entries": sorted(indexed.values(), key=lambda item: item["saved_at"]), "integrity": {"algorithm": "canonical-json-sha256.v1"}}
                new_index["integrity"]["document_hash"] = _hash_document(new_index)
                self._write(self.index_path, new_index)

    def _committed_checkpoint(self, body: Mapping[str, Any]) -> bool:
        operation_id = body.get("position", {}).get("committed_operation_id")
        try:
            decision = self.recovery.classify(str(operation_id), body["access_binding"]["owner_id"])
        except (KeyError, NavigatorError):
            return False
        return decision.result_checkpoint_id == body.get("checkpoint_id") and decision.disposition in {
            "REUSE_COMMITTED_RESULT", "COMPLETED"
        }

    def ensure_operation_capacity(self) -> None:
        operations = self.root / "operations"
        phase_files = list(operations.glob("*/*.json")) if operations.exists() else []
        operation_count = len({path.parent.name for path in phase_files})
        total = sum(path.stat().st_size for path in self.root.rglob("*.json") if path.is_file())
        if operation_count >= MAX_OPERATIONS or total >= self.max_total_bytes:
            raise NavigatorError("RESOURCE_LIMIT", {"max_operations": MAX_OPERATIONS,
                                                     "max_total_bytes": self.max_total_bytes})

    def classify_startup_recovery(self, owner_id: str) -> list[dict[str, Any]]:
        """Return observer-safe recovery decisions without mutating durable artifacts."""
        return self.recovery.classify_all(owner_id)

    def navigator_recovery_candidates(self, owner_id: str) -> list[dict[str, Any]]:
        """Project incomplete operation decisions into an observer-safe Navigator model."""
        categories = {
            "SAFE_REPLAY": ("AUTO_RECOVERABLE", "自動再開可能", "受付済み操作を安全境界から再実行できます"),
            "REUSE_COMMITTED_RESULT": ("AUTO_RECOVERABLE", "自動再開可能", "確定済み結果を再利用できます"),
            "SAFE_DISCARD": ("USER_CONFIRMATION_REQUIRED", "利用者確認待ち", "未適用の受付済み操作を破棄するか確認が必要です"),
            "QUARANTINED": ("QUARANTINED", "隔離", "正本を一意に決められないため自動再開しません"),
            "UNSUPPORTED_VERSION": ("RECOVERY_IMPOSSIBLE", "復旧不能", "この版では処理できません"),
            "RECOVERY_IMPOSSIBLE": ("RECOVERY_IMPOSSIBLE", "復旧不能", "必要な安全境界がありません"),
        }
        candidates = []
        generation = self.list(owner_id)["generation"]
        for decision in self.classify_startup_recovery(owner_id):
            if decision["disposition"] not in categories:
                continue
            category, label, summary = categories[decision["disposition"]]
            hand_label = "不明"
            checkpoint_id = decision.get("base_checkpoint_id")
            if checkpoint_id:
                try:
                    checkpoint = self.load(checkpoint_id, owner_id)
                    state = checkpoint.get("game_state", {})
                    wind = {"east": "東", "south": "南", "west": "西", "north": "北"}.get(
                        state.get("round_wind"), str(state.get("round_wind", ""))
                    )
                    hand_label = f'{wind}{state.get("hand_number", "?")}局'
                except NavigatorError:
                    pass
            candidates.append({
                "category": category, "category_label": label, "summary": summary,
                "operation_id": decision["operation_id"], "session_id": decision.get("session_id"),
                "hand_label": hand_label, "phase": decision.get("phase"),
                "last_safe_event_id": decision.get("base_event_id"),
                "last_safe_checkpoint_id": checkpoint_id,
                "pending_effect": "受付済み操作の結果が未確定です",
                "reason_code": decision["reason_code"],
                "generation": generation,
                "actions": (
                    ["RETRY"] if decision["disposition"] in {"SAFE_REPLAY", "REUSE_COMMITTED_RESULT"}
                    else ["DISCARD"] if decision["disposition"] == "SAFE_DISCARD"
                    else ["BRANCH"] if decision["disposition"] == "QUARANTINED" and checkpoint_id
                    else []
                ),
            })
        return candidates

    def quarantine_invalid_execution(self, operation_id: str, owner_id: str) -> dict[str, Any]:
        """Persist an observer-safe diagnosis while preserving every source artifact."""
        decision = self.recovery.classify(operation_id, owner_id).to_dict()
        if decision["disposition"] not in {"QUARANTINED", "UNSUPPORTED_VERSION", "RECOVERY_IMPOSSIBLE"}:
            raise NavigatorError("INVALID_EXECUTION_NOT_QUARANTINABLE",
                                 {"operation_id": operation_id})
        diagnostic = {
            "document_kind": "INVALID_EXECUTION_DIAGNOSTIC", "schema_version": 1,
            "created_at": _utc_now(), "owner_id": owner_id,
            "operation_id": operation_id, "session_id": decision["session_id"],
            "lineage_id": decision["lineage_id"], "status": "QUARANTINED",
            "reason_code": decision["reason_code"],
            "last_safe_checkpoint_id": decision["base_checkpoint_id"],
            "protected_artifacts": [f"operations/{operation_id}"],
            "permitted_actions": ["BRANCH_FROM_SAFE_BOUNDARY"] if decision["base_checkpoint_id"] else [],
            "mahjong_penalty_applied": False,
            "integrity": {"algorithm": "canonical-json-sha256.v1"},
        }
        diagnostic["integrity"]["document_hash"] = _hash_document(diagnostic)
        path = self.diagnostics / f"{operation_id}.json"
        if path.exists():
            existing = _strict_json_file(path)
            stable_existing = {key: value for key, value in existing.items()
                               if key not in {"created_at", "integrity"}}
            stable_new = {key: value for key, value in diagnostic.items()
                          if key not in {"created_at", "integrity"}}
            if stable_existing != stable_new or existing.get("integrity", {}).get("document_hash") != _hash_document(existing):
                raise NavigatorError("RECOVERY_DIAGNOSTIC_CONFLICT", {"operation_id": operation_id})
            return existing
        self._write(path, diagnostic)
        return diagnostic

    def discard_prepared_operation(self, operation_id: str, owner_id: str,
                                   request_id: str,
                                   expected_generation: int | None = None) -> dict[str, Any]:
        """Abort only an operation proven not to have changed authoritative state."""
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise NavigatorError("INVALID_REQUEST_ID")
        if (expected_generation is not None
                and self.list(owner_id)["generation"] != expected_generation):
            raise NavigatorError("REVISION_CONFLICT")
        decision = self.recovery.classify(operation_id, owner_id)
        if decision.disposition == "DISCARDED":
            return decision.to_dict()
        if decision.disposition != "SAFE_DISCARD":
            raise NavigatorError("RECOVERY_DISCARD_UNSAFE", {"operation_id": operation_id})
        prepared = OperationJournal(self).latest(operation_id)
        OperationJournal(self).terminate_prepared(prepared, "ABORTED", {
            "reason_code": "USER_DISCARDED_UNAPPLIED_OPERATION",
            "verification": {
                "base_checkpoint_id": decision.base_checkpoint_id,
                "authoritative_state_changed": False,
            },
            "discard_request_id": request_id,
        })
        return self.recovery.classify(operation_id, owner_id).to_dict()

    def _entry(self, body: Mapping[str, Any], display_name: str, save_kind: str) -> dict[str, Any]:
        return {
            "checkpoint_id": body["checkpoint_id"], "session_id": body["session_id"],
            "lineage_id": body["lineage_id"], "display_name": display_name,
            "save_kind": save_kind, "saved_at": body["created_at"],
            "format": body["session"]["format"], "human_seat": body["access_binding"]["human_seat"],
            "rule_profile_id": body["rules"]["profile_id"], "rule_snapshot_id": body["rules"]["snapshot_id"],
            "event_count": body["position"]["event_count"], "boundary_revision": body["position"]["boundary_revision"],
            "body_hash": body["integrity"]["document_hash"], "restorable": True,
        }

    def list(self, owner_id: str) -> dict[str, Any]:
        with self.lock:
            index = self._read_index()
            entries = []
            for entry in index["entries"]:
                try:
                    body = self._read_body(entry["checkpoint_id"])
                    if body["access_binding"]["owner_id"] == owner_id:
                        entries.append(deepcopy(entry))
                except NavigatorError as error:
                    value = deepcopy(entry)
                    value.update({"restorable": False, "reason": error.code})
                    entries.append(value)
            return {"generation": index["generation"], "entries": entries}

    def save(self, live: LiveSession, *, display_name: str, save_kind: str = "MANUAL", expected_generation: int | None = None, checkpoint_id: str | None = None,
             committed_operation_id: str | None = None, head_phase_hash: str | None = None,
             pending_delivery_ids: list[str] | None = None, publish: bool = True) -> dict[str, Any]:
        if save_kind not in {"MANUAL", "AUTO"} or not display_name.strip():
            raise NavigatorError("INVALID_CHECKPOINT_METADATA")
        checkpoint_id = checkpoint_id or str(uuid4())
        if not self._valid_id(checkpoint_id):
            raise NavigatorError("INVALID_CHECKPOINT_ID")
        hand = live.engine.current_hand if isinstance(live.engine, InteractiveMatchSession) else live.engine
        body: dict[str, Any] = {
            "document_kind": "SESSION_CHECKPOINT", "schema_id": CHECKPOINT_V031_SCHEMA_ID if committed_operation_id else CHECKPOINT_SCHEMA_ID,
            "format_version": CHECKPOINT_V031_FORMAT_VERSION if committed_operation_id else CHECKPOINT_FORMAT_VERSION, "origin_kind": "CHOMBO_SESSION",
            "record_state": "CHECKPOINT", "mutation_policy": "IMMUTABLE",
            "producer": deepcopy(self.producer),
            "created_at": _utc_now(), "checkpoint_id": checkpoint_id, "session_id": live.session_id,
            "lineage_id": live.lineage_id, "parent_checkpoint_id": live.parent_checkpoint_id,
            "display_name": display_name.strip(), "save_kind": save_kind,
            "position": {"record_id": live.record_id, "hand_id": hand.hand_id, "event_id": hand.last_event.event_id,
                         "event_count": len(hand.events), "boundary_revision": live.boundary_revision,
                         **({"committed_operation_id": committed_operation_id} if committed_operation_id else {})},
            "rules": {"profile_id": live.profile.profile_id, "snapshot_id": live.profile.snapshot_id,
                      "dimensions": _plain(live.profile.dimensions)},
            "participants": deepcopy(live.snapshot()["participants"]),
            "session": {"format": live.format, "seed": live.engine.seed, "start_position": live.start_position.to_dict(), "input_ledger": deepcopy(live.input_ledger),
                        "state_digest": _state_digest(live)},
            "game_state": _authoritative_state(hand.state),
            "action_context": _plain(hand.context.__dict__),
            "hand_execution": {"decision_count": hand.decision_count, "pending_action_ids": [a.action_id for p in hand.pending for a in p.actions]},
            "random_state": {"rng_id": hand.rng_id, "policy_id": next(iter(hand.agent_controllers.values())).controller_id if hand.agent_controllers else "chombo.human_input.v1", "global_decision_count": hand.decision_count},
            "controllers": [{"seat": seat.value, "kind": "HUMAN" if seat in hand.human_seats else "AGENT", "state": None} for seat in Seat],
            "access_binding": {"owner_id": live.owner_id, "human_seat": live.human_seat.value, "observer_kind": "HUMAN_SEAT"},
            "request_ledger": deepcopy(live.input_ledger),
            "integrity": {"algorithm": "canonical-json-sha256.v1"},
        }
        if committed_operation_id:
            body["session"]["input_ledger_digest"] = "sha256:" + hashlib.sha256(_canonical(live.input_ledger)).hexdigest()
            body["operation_journal"] = {"head_operation_id": committed_operation_id,
                                         "head_phase_hash": head_phase_hash,
                                         "pending_operation_ids": []}
            body["outbox"] = {"pending_delivery_ids": list(pending_delivery_ids or [])}
        body["integrity"]["document_hash"] = _hash_document(body)
        payload_size = len(_canonical(body)) + 1
        with self.lock:
            index = self._read_index()
            if expected_generation is not None and expected_generation != index["generation"]:
                raise NavigatorError("REVISION_CONFLICT", {"generation": index["generation"]})
            total = sum((self.checkpoints / f"{entry['checkpoint_id']}.json").stat().st_size for entry in index["entries"] if (self.checkpoints / f"{entry['checkpoint_id']}.json").exists())
            if len(index["entries"]) >= self.max_checkpoints or total + payload_size > self.max_total_bytes:
                raise NavigatorError("RESOURCE_LIMIT", {"max_checkpoints": self.max_checkpoints, "max_total_bytes": self.max_total_bytes})
            path = self.checkpoints / f"{checkpoint_id}.json"
            if path.exists() or checkpoint_id in self._tombstoned():
                raise NavigatorError("CHECKPOINT_ID_CONFLICT")
            self._write(path, body)
            entry = self._entry(body, display_name.strip(), save_kind)
            if not publish:
                return deepcopy(entry) | {"generation": index["generation"]}
            return self.publish_checkpoint(entry, expected_generation=index["generation"])

    def publish_checkpoint(self, entry: Mapping[str, Any], *, expected_generation: int) -> dict[str, Any]:
        with self.lock:
            index = self._read_index()
            if index["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT", {"generation": index["generation"]})
            new_index = {"schema_version": 1, "generation": index["generation"] + 1, "entries": [*index["entries"], entry], "integrity": {"algorithm": "canonical-json-sha256.v1"}}
            new_index["integrity"]["document_hash"] = _hash_document(new_index)
            try:
                self._write(self.index_path, new_index)
            except Exception as error:
                raise NavigatorError("SAVE_FAILED", {"checkpoint_id": entry.get("checkpoint_id")}) from error
            return deepcopy(dict(entry)) | {"generation": new_index["generation"]}

    def load(self, checkpoint_id: str, owner_id: str) -> dict[str, Any]:
        with self.lock:
            if checkpoint_id in self._tombstoned():
                raise NavigatorError("CHECKPOINT_DELETED")
            body = self._read_body(checkpoint_id)
            if (body.get("schema_id"), body.get("format_version")) not in {
                (CHECKPOINT_SCHEMA_ID, CHECKPOINT_FORMAT_VERSION),
                (CHECKPOINT_V031_SCHEMA_ID, CHECKPOINT_V031_FORMAT_VERSION),
            }:
                raise NavigatorError("CHECKPOINT_VERSION_UNSUPPORTED")
            if body.get("access_binding", {}).get("owner_id") != owner_id:
                raise NavigatorError("ACCESS_DENIED")
            return body

    def rename(self, checkpoint_id: str, owner_id: str, display_name: str, *, expected_generation: int) -> dict[str, Any]:
        self.load(checkpoint_id, owner_id)
        if not display_name.strip():
            raise NavigatorError("INVALID_CHECKPOINT_METADATA")
        with self.lock:
            index = self._read_index()
            if index["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT", {"generation": index["generation"]})
            entries = deepcopy(index["entries"])
            entry = next((item for item in entries if item["checkpoint_id"] == checkpoint_id), None)
            if entry is None:
                raise NavigatorError("CHECKPOINT_NOT_FOUND")
            entry["display_name"] = display_name.strip()
            new_index = {"schema_version": 1, "generation": index["generation"] + 1, "entries": entries, "integrity": {"algorithm": "canonical-json-sha256.v1"}}
            new_index["integrity"]["document_hash"] = _hash_document(new_index)
            self._write(self.index_path, new_index)
            return deepcopy(entry) | {"generation": new_index["generation"]}

    def delete(self, checkpoint_id: str, owner_id: str, request_id: str, *, expected_generation: int) -> dict[str, Any]:
        self.load(checkpoint_id, owner_id)
        with self.lock:
            index = self._read_index()
            if index["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT", {"generation": index["generation"]})
            tombstone = {"schema_version": 1, "checkpoint_id": checkpoint_id, "deleted_at": _utc_now(), "request_id": request_id, "integrity": {"algorithm": "canonical-json-sha256.v1"}}
            tombstone["integrity"]["document_hash"] = _hash_document(tombstone)
            self._write(self.tombstones / f"{checkpoint_id}.json", tombstone)
            entries = [entry for entry in index["entries"] if entry["checkpoint_id"] != checkpoint_id]
            new_index = {"schema_version": 1, "generation": index["generation"] + 1, "entries": entries, "integrity": {"algorithm": "canonical-json-sha256.v1"}}
            new_index["integrity"]["document_hash"] = _hash_document(new_index)
            self._write(self.index_path, new_index)
            path = self.checkpoints / f"{checkpoint_id}.json"
            if path.exists():
                path.unlink()
                if os.name != "nt":
                    directory = os.open(self.checkpoints, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            return {"deleted": True, "generation": new_index["generation"]}


class NavigatorSessionManager:
    def __init__(self, store: CheckpointStore, *, owner_id: str = "local-user", seed_clock: Callable[[], int] | None = None, agent_delay_seconds: float = 0,
                 allow_evaluation_records: bool = False, defer_initial_advance: bool = False):
        self.store = store
        self.owner_id = owner_id
        self.seed_clock = seed_clock
        self.agent_delay_seconds = agent_delay_seconds
        self.active: LiveSession | None = None
        self.replay: ReplaySession | None = None
        self.allow_evaluation_records = allow_evaluation_records
        self.defer_initial_advance = defer_initial_advance
        self._starts: dict[str, LiveSession] = {}
        self._presentation_snapshots: dict[str, tuple[dict[str, Any], ...]] = {}
        self.conversations = PositionConversationGate()
        self.application = Application(concepts_dir=_RUNTIME_ROOT / "concepts")
        self.codex = CodexAdapter(self.application, owner_id=owner_id, conversations=self.conversations)
        self._replay_grants: dict[str, tuple[str, str | None, str]] = {}
        self.lock = threading.RLock()
        self.journal = OperationJournal(store)

    def submit_action(self, request_id: str, action_id: str) -> dict[str, Any]:
        """Durably apply one action without mutating the published Live in place."""
        if not self._valid_request_id(request_id):
            raise NavigatorError("INVALID_REQUEST_ID")
        with self.lock:
            live = self.active
            if live is None:
                raise NavigatorError("NO_ACTIVE_SESSION")
            previous = self.journal.find_request(live.owner_id, live.session_id, request_id)
            if previous is not None:
                if previous["payload"]["input_hash"] != self.journal.input_hash(action_id):
                    raise NavigatorError("REQUEST_ID_CONFLICT", {"request_id": request_id})
                latest = self.journal.latest(previous["operation_id"])
                if latest and latest["phase"] in {"COMMITTED", "DELIVERED"}:
                    committed = latest
                    if latest["phase"] == "DELIVERED":
                        return deepcopy(latest["payload"]["response"])
                    return deepcopy(self.journal.deliver(committed)["payload"]["response"])
            legal = [item["action_id"] for item in live.snapshot()["legal_actions"]]
            if action_id not in legal:
                raise NavigatorError("ACTION_NOT_IN_LEGAL_SET", {"action_id": action_id})
            prepared = self.journal.prepare(live, request_id, action_id, legal)
            latest = self.journal.latest(prepared["operation_id"])
            if latest and latest["phase"] in {"COMMITTED", "DELIVERED"}:
                if latest["phase"] == "DELIVERED":
                    return deepcopy(latest["payload"]["response"])
                return deepcopy(self.journal.deliver(latest)["payload"]["response"])
            scratch = LiveSession(live.session_id, live.lineage_id, live.record_id, live.format,
                                  live.profile, _fork_engine(live.engine, auto_advance=not self.defer_initial_advance), live.start_position,
                                  live.owner_id, deepcopy(live.input_ledger), live.parent_checkpoint_id,
                                  live.boundary_revision)
            if _state_digest(scratch) != _state_digest(live):
                raise NavigatorError("OPERATION_BASE_MISMATCH")
            presentation_snapshots: list[dict[str, Any]] = []
            scratch.engine.on_event = lambda: presentation_snapshots.append(
                scratch.snapshot()
            )
            try:
                result = scratch.submit(request_id, action_id)
            finally:
                scratch.engine.on_event = None
            checkpoint_id = str(uuid4())
            committed = self.journal.commit(prepared, scratch, checkpoint_id, result, persist=False)
            pending_entry = self.store.save(
                scratch, display_name="操作確定境界", save_kind="AUTO",
                checkpoint_id=checkpoint_id, committed_operation_id=prepared["operation_id"],
                head_phase_hash=committed["integrity"]["document_hash"], publish=False,
                pending_delivery_ids=[item["delivery_id"] for item in committed["payload"]["outbox_items"]],
            )
            self.journal.persist_commit(committed)
            entry = {key: value for key, value in pending_entry.items() if key != "generation"}
            self.store.publish_checkpoint(entry, expected_generation=pending_entry["generation"])
            scratch.parent_checkpoint_id = checkpoint_id
            self.active = scratch
            self._presentation_snapshots[request_id] = tuple(
                presentation_snapshots
            )
            delivered = self.journal.deliver(committed)
            return deepcopy(delivered["payload"]["response"])

    def take_presentation_snapshots(
        self, request_id: str
    ) -> tuple[dict[str, Any], ...]:
        """Return committed live frames once for post-commit UI playback."""
        with self.lock:
            return self._presentation_snapshots.pop(request_id, ())

    def retry_recovery_operation(self, operation_id: str, *,
                                 expected_active_session_id: str | None,
                                 expected_generation: int) -> dict[str, Any]:
        """Resume one verified operation without creating another operation or effect."""
        with self.lock:
            decision = self.store.recovery.classify(operation_id, self.owner_id)
            if decision.disposition == "COMPLETED":
                latest = self.journal.latest(operation_id)
                if latest is None or latest.get("phase") != "DELIVERED":
                    raise NavigatorError("RECOVERY_RESULT_UNAVAILABLE", {"operation_id": operation_id})
                return deepcopy(latest["payload"]["response"])
            if decision.disposition not in {"SAFE_REPLAY", "REUSE_COMMITTED_RESULT"}:
                raise NavigatorError("RECOVERY_RETRY_UNSAFE", {"operation_id": operation_id})
            if (self.active.session_id if self.active else None) != expected_active_session_id:
                raise NavigatorError("ACTIVE_SESSION_CONFLICT")
            if self.store.list(self.owner_id)["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT")
            prepared = self.store._strict_read(
                self.store.root / "operations" / operation_id / "000-prepared.json"
            )
            if (prepared.get("operation_id") != operation_id
                    or prepared.get("integrity", {}).get("document_hash")
                    != self.store.hash_document(prepared)):
                raise NavigatorError("OPERATION_CHAIN_INVALID", {"operation_id": operation_id})
            checkpoint_id = (
                decision.result_checkpoint_id
                if decision.disposition == "REUSE_COMMITTED_RESULT"
                else decision.base_checkpoint_id
            )
            if checkpoint_id is None:
                raise NavigatorError("RECOVERY_SAFE_BOUNDARY_UNAVAILABLE", {"operation_id": operation_id})
            body = self.store.load(checkpoint_id, self.owner_id)
            profile = ResolvedRuleProfile(
                body["rules"]["profile_id"], body["rules"]["snapshot_id"],
                MappingProxyType({key: MappingProxyType(value) for key, value in body["rules"]["dimensions"].items()}),
            )
            position_body = body["session"].get("start_position", {})
            score_body = position_body.get("seat_scores")
            position = StartPosition(
                Seat(position_body.get("round_wind", "east")), int(position_body.get("hand_number", 1)),
                int(position_body.get("honba", 0)), int(position_body.get("riichi_deposits", 0)),
                Seat(position_body["human_seat"]) if position_body.get("human_seat") else None,
                {Seat(seat): int(score) for seat, score in score_body.items()} if score_body is not None else None,
            )
            live = LiveSession(
                prepared["session_id"], prepared["lineage_id"], str(uuid4()), body["session"]["format"],
                profile, self._engine(body["session"]["format"], body["session"]["seed"], profile, position),
                position, self.owner_id, [], checkpoint_id, 0,
            )
            for item in body["session"]["input_ledger"]:
                live.submit(item["request_id"], item["action_id"])
            if _state_digest(live) != body["session"]["state_digest"]:
                raise NavigatorError("CHECKPOINT_STATE_MISMATCH")
            live.boundary_revision = body["position"]["boundary_revision"]
            if decision.disposition == "REUSE_COMMITTED_RESULT":
                indexed = self.store.list(self.owner_id)
                if not any(
                    entry["checkpoint_id"] == checkpoint_id
                    for entry in indexed["entries"]
                ):
                    entry = self.store._entry(
                        body, body["display_name"], body["save_kind"]
                    )
                    self.store.publish_checkpoint(
                        entry, expected_generation=expected_generation
                    )
            previous_active = self.active
            self.active = live
            try:
                if decision.disposition == "REUSE_COMMITTED_RESULT":
                    committed = self.journal.latest(operation_id)
                    if committed is None:
                        raise NavigatorError("RECOVERY_RESULT_UNAVAILABLE", {"operation_id": operation_id})
                    return deepcopy(self.journal.deliver(committed)["payload"]["response"])
                action_id = prepared["payload"]["normalized_input"]["action_id"]
                return self.submit_action(prepared["request_id"], action_id)
            except Exception:
                self.active = previous_active
                raise

    def _engine(
        self,
        format: str,
        seed: int,
        profile: ResolvedRuleProfile,
        position: StartPosition,
        *,
        agent_delay_seconds: float | None = None,
    ):
        position.validate()
        delay = self.agent_delay_seconds if agent_delay_seconds is None else agent_delay_seconds
        if format == "single_hand":
            human = position.human_seat or random_human_seat(seed)
            setup = create_initial_state(str(seed), profile, round_wind=position.round_wind, hand_number=position.hand_number)
            players = setup.state.players if position.seat_scores is None else tuple(
                replace(player, score=position.seat_scores[player.seat]) for player in setup.state.players
            )
            setup = replace(setup, state=replace(setup.state, players=players, honba=position.honba, riichi_deposits=position.riichi_deposits))
            return InteractiveHandSession(seed, profile, (human,), setup=setup, agent_delay_seconds=delay,
                                          auto_advance=not self.defer_initial_advance)
        if format == "east_south":
            return InteractiveMatchSession(seed, profile, human_count=1, agent_delay_seconds=delay,
                                           start_round_wind=position.round_wind, start_hand_number=position.hand_number,
                                           start_honba=position.honba, start_riichi_deposits=position.riichi_deposits,
                                           start_human_seat=position.human_seat, start_seat_scores=position.seat_scores,
                                           auto_advance=not self.defer_initial_advance)
        raise NavigatorError("UNSUPPORTED_MATCH_FORMAT", {"format": format})

    def start(self, request: StartRequest) -> LiveSession:
        with self.lock:
            if request.request_id in self._starts:
                return self._starts[request.request_id]
            if not self._valid_request_id(request.request_id):
                raise NavigatorError("INVALID_REQUEST_ID")
            route = "hand" if request.format == "single_hand" else "match"
            profile = load_selected_profile(request.selection, route=route)
            seed = resolve_seed(request.seed, **({"clock_ns": self.seed_clock} if self.seed_clock else {}))
            request.position.validate()
            if request.position.seat_scores is not None:
                expected_total = int(profile.value("match.start.points")["points_per_player"]) * 4
                if sum(request.position.seat_scores.values()) + request.position.riichi_deposits != expected_total:
                    raise NavigatorError("START_POINT_TOTAL_MISMATCH", {"expected": expected_total,
                                                                         "actual": sum(request.position.seat_scores.values()) + request.position.riichi_deposits})
            live = LiveSession(str(uuid4()), str(uuid4()), str(uuid4()), request.format, profile,
                               self._engine(request.format, seed, profile, request.position), request.position, self.owner_id, [])
            self.active = live
            self._starts[request.request_id] = live
            return live

    @staticmethod
    def _valid_request_id(value: str) -> bool:
        return isinstance(value, str) and 0 < len(value) <= 128 and "\x00" not in value

    def checkpoint(self, display_name: str, *, save_kind: str = "MANUAL", expected_generation: int | None = None) -> dict[str, Any]:
        if self.active is None:
            raise NavigatorError("NO_ACTIVE_SESSION")
        return self.store.save(self.active, display_name=display_name, save_kind=save_kind, expected_generation=expected_generation)

    def leave_live(self) -> dict[str, Any]:
        if self.active is not None and self.active.engine.complete:
            return self.save_completed_record()
        return self.checkpoint("Navigatorへ戻る", save_kind="AUTO")

    def save_completed_record(self) -> dict[str, Any]:
        """Persist the completed Live as a canonical record and select its Replay."""
        live = self.active
        if live is None or not live.engine.complete:
            raise NavigatorError("COMPLETED_RECORD_UNAVAILABLE")
        producer = self.store.producer
        if isinstance(live.engine, InteractiveMatchSession):
            run = live.engine.result
            if run is None:
                raise NavigatorError("COMPLETED_RECORD_UNAVAILABLE")
            document = match_record_v03_document(
                run,
                app_version=producer["app_version"],
                build_revision=producer["build_revision"],
                engine_version=producer["engine_version"],
                source_session_id=live.session_id,
            )
            filename = f"match-{run.seed}-{document['record_id'][:8]}.json"
        else:
            if live.engine.log is None:
                raise NavigatorError("COMPLETED_RECORD_UNAVAILABLE")
            document = hand_record_document(
                live.engine.log,
                app_version=producer["app_version"],
                build_revision=producer["build_revision"],
                engine_version=producer["engine_version"],
                source_session_id=live.session_id,
            )
            filename = f"hand-{live.engine.seed}-{document['record_id'][:8]}.json"
        path = self.store.records / filename
        write_record(document, path, allow_evaluation=True)
        replay = ReplaySession(filename, document)
        replay.current_hand()
        self.replay = replay
        self.active = None
        return {"saved": True, "filename": filename, "path": str(path), **replay.metadata()}

    def rematch(self) -> LiveSession:
        """Start a fresh Live with the completed session's format and rules."""
        previous = self.active
        if previous is None or not previous.engine.complete:
            raise NavigatorError("COMPLETED_SESSION_REQUIRED")
        seed = resolve_seed(
            None, **({"clock_ns": self.seed_clock} if self.seed_clock else {})
        )
        fresh_position = StartPosition()
        live = LiveSession(
            str(uuid4()), str(uuid4()), str(uuid4()), previous.format,
            previous.profile,
            self._engine(
                previous.format, seed, previous.profile, fresh_position
            ),
            fresh_position, self.owner_id, [],
        )
        self.active = live
        self.replay = None
        return live

    def open_record(self, raw: bytes, filename: str) -> ReplaySession:
        """Validate fully before replacing the currently selected Replay."""
        if not isinstance(filename, str) or not filename or len(filename) > 255 or "\x00" in filename:
            raise NavigatorError("INVALID_RECORD_FILENAME")
        try:
            record = read_record_bytes(raw, allow_evaluation=self.allow_evaluation_records)
        except CanonicalRecordError as error:
            raise NavigatorError(error.code, error.details) from error
        replay = ReplaySession(Path(filename).name, record)
        replay.current_hand()
        with self.lock:
            self.replay = replay
        return replay

    def grant_replay_analysis(self, *, scope: str, seat: str | None = None) -> str:
        """Create a server-side grant for the currently opened completed Replay."""
        if self.replay is None or scope not in {"SELF_SEAT", "ALL_HANDS"}:
            raise NavigatorError("INVALID_REPLAY_PERMISSION")
        if scope == "SELF_SEAT" and seat not in {item.value for item in Seat}:
            raise NavigatorError("INVALID_REPLAY_PERMISSION")
        grant_id = str(uuid4())
        self._replay_grants[grant_id] = (scope, seat, self.replay.record["integrity"]["document_hash"])
        return grant_id

    def revoke_replay_analysis(self, grant_id: str) -> None:
        self._replay_grants.pop(grant_id, None)
        self.conversations.revoke(grant_id)

    def issue_position_question(self, *, source_kind: str, requested_subject: str,
                                subject_seat: str | None = None,
                                permission_grant_id: str | None = None):
        """Freeze the selected Live/Replay position before any question analysis."""
        if source_kind in {"LIVE_SESSION", "ACTIVE_SESSION_REPLAY"}:
            live = self.active
            if live is None:
                raise NavigatorError("POSITION_UNAVAILABLE")
            projection = live.snapshot()
            events = projection.get("sound_events", [])
            last = events[-1] if events else {}
            return self.codex.bind_reference(self.conversations.issue(
                source_kind=source_kind,
                mode="PLAYING" if source_kind == "LIVE_SESSION" else "ACTIVE_REPLAY",
                observer_kind="HUMAN_SEAT", observer_seat=live.human_seat.value,
                requested_subject=requested_subject, subject_seat=subject_seat,
                projection=projection, session_id=live.session_id, lineage_id=live.lineage_id,
                checkpoint_id=live.parent_checkpoint_id, record_id=live.record_id,
                hand_id=str(projection["hand_id"]), event_id=last.get("event_id"),
                event_sequence=max(0, int(projection["event_count"]) - 1),
                rule_profile_id=live.profile.profile_id,
                rule_snapshot_id=live.profile.snapshot_id,
            ))
        replay = self.replay
        if source_kind != "COMPLETED_REPLAY" or replay is None:
            raise NavigatorError("POSITION_UNAVAILABLE")
        observer = PUBLIC_OBSERVER
        observer_kind = "PUBLIC"
        observer_seat = None
        if permission_grant_id is not None:
            grant = self._replay_grants.get(permission_grant_id)
            if grant is None or grant[2] != replay.record["integrity"]["document_hash"]:
                raise NavigatorError("REPLAY_PERMISSION_REQUIRED")
            observer_kind = grant[0]
            observer_seat = grant[1]
            if grant[0] == "ALL_HANDS":
                observer = AUTHORIZED_REPLAY_OBSERVER
            else:
                payload = replay.record["payload"]
                hands = (
                    [payload["hand"]]
                    if replay.record["document_kind"] == "HAND_RECORD"
                    else payload["hands"]
                )
                current_assignments = hands[replay.hand_index]["seat_assignments"]
                participant_id = current_assignments[str(grant[1])]
                initial_assignments = hands[0]["seat_assignments"]
                initial_seat = next(
                    seat for seat, assigned in initial_assignments.items()
                    if assigned == participant_id
                )
                observer = human_observer(Seat(initial_seat))
        hand = replay.current_hand(observer)
        frame = hand["frames"][replay.event_index]
        ref = replay.record["rules"]["selected_ref"]
        payload = replay.record["payload"]
        record_id = payload.get("match_id") or payload["hand"]["hand_id"]
        return self.codex.bind_reference(self.conversations.issue(
            source_kind=source_kind, mode="COMPLETED_REPLAY",
            observer_kind=observer_kind, observer_seat=observer_seat,
            requested_subject=requested_subject, subject_seat=subject_seat,
            projection={"state": frame["state"]}, session_id=None, lineage_id=None,
            checkpoint_id=None, record_id=record_id, hand_id=str(hand["hand_id"]),
            event_id=frame.get("event_id"), event_sequence=replay.event_index,
            rule_profile_id=ref["profile_id"], rule_snapshot_id=ref["snapshot_id"],
            permission_grant_id=permission_grant_id,
        ))

    def restore(self, checkpoint_id: str, request_id: str, *, expected_active_session_id: str | None, expected_generation: int) -> LiveSession:
        if not self._valid_request_id(request_id):
            raise NavigatorError("INVALID_REQUEST_ID")
        request_path = self.store.restore_requests / f"{hashlib.sha256(request_id.encode()).hexdigest()}.json"
        with self.lock:
            committed_session_id = None
            if request_path.exists():
                prior = json.loads(request_path.read_text(encoding="utf-8"))
                if prior["checkpoint_id"] != checkpoint_id:
                    raise NavigatorError("REQUEST_ID_CONFLICT")
                if self.active is not None and self.active.session_id == prior.get("new_session_id"):
                    return self.active
                committed_session_id = prior.get("new_session_id")
            if (self.active.session_id if self.active else None) != expected_active_session_id:
                raise NavigatorError("ACTIVE_SESSION_CONFLICT")
            body = self.store.load(checkpoint_id, self.owner_id)
            if self.store.list(self.owner_id)["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT")
            # The current Live remains authoritative until this save succeeds.
            # A failed save therefore cannot destroy or replace it.
            if self.active is not None and committed_session_id is None:
                self.store.save(self.active, display_name="復帰前の自動保存", save_kind="AUTO", expected_generation=expected_generation)
            profile = ResolvedRuleProfile(body["rules"]["profile_id"], body["rules"]["snapshot_id"], MappingProxyType({key: MappingProxyType(value) for key, value in body["rules"]["dimensions"].items()}))
            position_body = body["session"].get("start_position", {})
            score_body = position_body.get("seat_scores")
            position = StartPosition(Seat(position_body.get("round_wind", "east")), int(position_body.get("hand_number", 1)),
                                     int(position_body.get("honba", 0)), int(position_body.get("riichi_deposits", 0)),
                                     Seat(position_body["human_seat"]) if position_body.get("human_seat") else None,
                                     {Seat(seat): int(score) for seat, score in score_body.items()} if score_body is not None else None)
            engine = self._engine(body["session"]["format"], body["session"]["seed"], profile, position)
            live = LiveSession(committed_session_id or str(uuid4()), body["lineage_id"], str(uuid4()), body["session"]["format"], profile, engine,
                               position, self.owner_id, [], checkpoint_id, 0)
            for item in body["session"]["input_ledger"]:
                live.submit(item["request_id"], item["action_id"])
            if _state_digest(live) != body["session"]["state_digest"]:
                raise NavigatorError("CHECKPOINT_STATE_MISMATCH")
            live.boundary_revision = body["position"]["boundary_revision"]
            prepared = {"request_id": request_id, "checkpoint_id": checkpoint_id, "status": "COMMITTED", "new_session_id": live.session_id, "created_at": _utc_now()}
            self.store._write(request_path, prepared)
            self.active = live
            return live

    def branch_from_safe_boundary(self, operation_id: str, request_id: str, *,
                                  expected_active_session_id: str | None,
                                  expected_generation: int) -> LiveSession:
        """Create a new lineage from a quarantined operation's verified base."""
        if not self._valid_request_id(request_id):
            raise NavigatorError("INVALID_REQUEST_ID")
        diagnostic = self.store.quarantine_invalid_execution(operation_id, self.owner_id)
        checkpoint_id = diagnostic.get("last_safe_checkpoint_id")
        if checkpoint_id is None:
            raise NavigatorError("RECOVERY_SAFE_BOUNDARY_UNAVAILABLE", {"operation_id": operation_id})
        request_path = self.store.recovery_requests / f"{hashlib.sha256(request_id.encode()).hexdigest()}.json"
        with self.lock:
            if request_path.exists():
                prior = _strict_json_file(request_path)
                if (
                    prior.get("integrity", {}).get("document_hash") != _hash_document(prior)
                    or prior.get("operation_id") != operation_id
                    or prior.get("status") != "COMMITTED"
                    or not self.store._valid_id(prior.get("new_session_id", ""))
                    or not self.store._valid_id(prior.get("new_lineage_id", ""))
                ):
                    raise NavigatorError("REQUEST_ID_CONFLICT")
                if self.active is not None and self.active.session_id == prior.get("new_session_id"):
                    return self.active
                new_session_id = prior["new_session_id"]
                new_lineage_id = prior["new_lineage_id"]
            else:
                new_session_id, new_lineage_id = str(uuid4()), str(uuid4())
            if (self.active.session_id if self.active else None) != expected_active_session_id:
                raise NavigatorError("ACTIVE_SESSION_CONFLICT")
            body = self.store.load(checkpoint_id, self.owner_id)
            if self.store.list(self.owner_id)["generation"] != expected_generation:
                raise NavigatorError("REVISION_CONFLICT")
            profile = ResolvedRuleProfile(
                body["rules"]["profile_id"], body["rules"]["snapshot_id"],
                MappingProxyType({key: MappingProxyType(value) for key, value in body["rules"]["dimensions"].items()}),
            )
            position_body = body["session"].get("start_position", {})
            score_body = position_body.get("seat_scores")
            position = StartPosition(
                Seat(position_body.get("round_wind", "east")), int(position_body.get("hand_number", 1)),
                int(position_body.get("honba", 0)), int(position_body.get("riichi_deposits", 0)),
                Seat(position_body["human_seat"]) if position_body.get("human_seat") else None,
                {Seat(seat): int(score) for seat, score in score_body.items()} if score_body is not None else None,
            )
            live = LiveSession(
                new_session_id, new_lineage_id, str(uuid4()), body["session"]["format"], profile,
                self._engine(body["session"]["format"], body["session"]["seed"], profile, position),
                position, self.owner_id, [], checkpoint_id, 0,
            )
            for item in body["session"]["input_ledger"]:
                live.submit(item["request_id"], item["action_id"])
            if _state_digest(live) != body["session"]["state_digest"]:
                raise NavigatorError("CHECKPOINT_STATE_MISMATCH")
            live.boundary_revision = body["position"]["boundary_revision"]
            record = {
                "schema_version": 1, "request_id": request_id, "operation_id": operation_id,
                "source_checkpoint_id": checkpoint_id, "source_lineage_id": body["lineage_id"],
                "new_session_id": new_session_id, "new_lineage_id": new_lineage_id,
                "status": "COMMITTED", "created_at": _utc_now(),
                "mahjong_penalty_applied": False,
                "integrity": {"algorithm": "canonical-json-sha256.v1"},
            }
            record["integrity"]["document_hash"] = _hash_document(record)
            if not request_path.exists():
                self.store._write(request_path, record)
            self.active = live
            return live


def registered_navigator_profiles() -> list[dict[str, Any]]:
    registry = json.loads((Path(__file__).parent / "profiles/registry.v1.json").read_text(encoding="utf-8"))
    return [
        {"alias": item["aliases"][0], "name": item["aliases"][-1], "version": item["ruleset_version"],
         "points_per_player": load_selected_profile(RuleSelection(alias=item["aliases"][0]), route="match").value("match.start.points")["points_per_player"]}
        for item in registry["profiles"]
    ]


class NavigatorServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], manager: NavigatorSessionManager):
        super().__init__(address, NavigatorHandler)
        self.manager = manager
        self.profiles = registered_navigator_profiles()
        self.rule_store = DraftStore(manager.store.root / "rules", DraftValidator())
        self.state_lock = threading.RLock()
        self.busy = False
        self.replay_error: str | None = None
        self.published_state: dict[str, Any] | None = None
        if manager.active is not None:
            self.activate(manager.active)

    def activate(self, live: LiveSession) -> None:
        with self.state_lock:
            self.published_state = live.snapshot()
            live.engine.on_event = self.publish
        if self.manager.defer_initial_advance:
            with self.state_lock:
                self.busy = True
            threading.Thread(target=self.advance_live, daemon=True).start()

    def advance_live(self) -> None:
        live = self.manager.active
        if live is None:
            return
        try:
            live.engine.advance()
            self.publish()
        finally:
            with self.state_lock:
                self.busy = False

    def publish(self) -> None:
        live = self.manager.active
        if live is not None:
            with self.state_lock:
                self.published_state = live.snapshot()

    def apply_action(self, request_id: str, action_id: str) -> None:
        try:
            live = self.manager.active
            if live is None:
                return
            self.manager.submit_action(request_id, action_id)
            live = self.manager.active
            live.engine.on_event = self.publish
            snapshots = self.manager.take_presentation_snapshots(request_id)
            for index, snapshot in enumerate(snapshots):
                if index and self.manager.agent_delay_seconds:
                    time.sleep(self.manager.agent_delay_seconds)
                with self.state_lock:
                    self.published_state = snapshot
            if not snapshots:
                self.publish()
        finally:
            with self.state_lock:
                self.busy = False


class NavigatorHandler(BaseHTTPRequestHandler):
    server: NavigatorServer

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _form(self, *, strict_json: bool = False) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise NavigatorError("INVALID_REQUEST") from error
        if size < 0 or size > 64 * 1024:
            raise NavigatorError("RESOURCE_LIMIT")
        media_type = self.headers.get("Content-Type", "")
        raw = self.rfile.read(size)
        if strict_json:
            if media_type.split(";", 1)[0].strip() != "application/json":
                raise NavigatorError("INVALID_REQUEST")
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise NavigatorError("INVALID_REQUEST")
                    value[key] = item
                return value
            return json.loads(raw, object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(NavigatorError("INVALID_REQUEST")))
        if "application/json" in media_type:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise NavigatorError("INVALID_REQUEST")
            return {str(key): item for key, item in value.items() if item is not None}
        return {key: values[-1] for key, values in parse_qs(raw.decode()).items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/conversation/openapi.json":
            self._json(200, codex_openapi_document()); return
        if self.path == "/assets/chombo-icon.png":
            self._send(200, LOGO_PATH.read_bytes(), "image/png")
            return
        from engine.ui_compat import serve_ui
        if serve_ui(self):
            return
        if self.path == "/api/replay" and self.server.manager.replay is not None:
            self._json(200, self.server.manager.replay.metadata())
            return
        if self.path == "/api/state" and self.server.manager.active is not None:
            with self.server.state_lock:
                state = deepcopy(self.server.published_state or self.server.manager.active.snapshot())
                processing = self.server.busy
            self._json(200, state | {"processing": processing})
            return
        if self.path == "/api/checkpoints":
            self._json(200, self.server.manager.store.list(self.server.manager.owner_id))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/replay/open":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise NavigatorError("INVALID_REQUEST") from error
                if size > MAX_DOCUMENT_BYTES:
                    raise NavigatorError("RESOURCE_LIMIT", {"resource": "file_size"})
                filename = unquote(self.headers.get("X-Chombo-Filename", ""))
                replay = self.server.manager.open_record(self.rfile.read(size), filename)
                self.server.replay_error = None
                self._json(201, replay.metadata())
                return
            body = self._form(strict_json=True) if self.path == "/api/conversation/confirm" else self._form()
            if self.path == "/api/conversation/confirm":
                from engine.application_schema import validate
                from engine.codex_schema import CONFIRM_INPUT
                validate(body, CONFIRM_INPUT)
                try:
                    ref = self.server.manager.codex.confirm_subject(body["conversation_ref_id"], body["choice_id"])
                except ValueError as error:
                    raise NavigatorError(str(error)) from error
                self._json(201, ref.public_metadata()); return
            if self.path == "/api/rules/drafts":
                profile_id = str(body.get("base_profile_id", ""))
                base_ref = next(
                    (entry["ref"] for entry, _ in self.server.rule_store.validator.catalogs()[4]
                     if entry["ref"]["profile_id"] == profile_id),
                    None,
                )
                if base_ref is None:
                    raise RuleProfileError("REVIEW_REQUIRED", {"reason": "PROFILE_NOT_APPROVED"})
                editor = RuleEditorSession.create(
                    self.server.rule_store, base_ref, str(body.get("display_name", "")),
                    actor_id=self.server.manager.owner_id,
                )
                result = editor.save()
                location = f'/rules/edit/{result["draft"]["draft_id"]}'
                if "application/json" not in self.headers.get("Content-Type", ""):
                    self.send_response(303); self.send_header("Location", location); self.end_headers(); return
                self._json(201, {"draft_id": result["draft"]["draft_id"], "location": location}); return
            discard_match = re.fullmatch(
                r"/api/rules/custom/(custom\.[0-9a-f-]{36})/discard", self.path
            )
            if discard_match is not None:
                result = self.server.rule_store.discard_custom_ruleset(discard_match.group(1))
                if "application/json" not in self.headers.get("Content-Type", ""):
                    self.send_response(303); self.send_header("Location", "/rules"); self.end_headers(); return
                self._json(200, result); return
            save_match = re.fullmatch(r"/api/rules/drafts/([0-9a-f-]{36})/save", self.path)
            if save_match is not None:
                editor = RuleEditorSession.open(
                    self.server.rule_store, save_match.group(1), actor_id=self.server.manager.owner_id,
                )
                editor.rename(str(body.get("display_name", "")))
                overrides = body.get("overrides")
                if not isinstance(overrides, dict):
                    raise RuleProfileError("MALFORMED_DRAFT", {"reason": "EDITOR_INPUT"})
                for dimension in list(editor.draft["overrides"]):
                    if dimension not in overrides:
                        editor.unset_override(dimension)
                for dimension, value in overrides.items():
                    editor.set_override(str(dimension), value, request_summary=str(body.get("request_summary", "Navigator rule editor update")))
                editor.synchronize_fixed_dimensions(
                    request_summary="Navigator approved-profile fixed-axis synchronization"
                )
                result = editor.save()
                self._json(200, {"status": result["status"], "validation": result.get("validation")}); return
            confirm_match = re.fullmatch(r"/api/rules/drafts/([0-9a-f-]{36})/confirm", self.path)
            if confirm_match is not None:
                editor = RuleEditorSession.open(
                    self.server.rule_store, confirm_match.group(1), actor_id=self.server.manager.owner_id,
                )
                confirmed = editor.confirm()
                self._json(200, {"status": "CONFIRMED", "profile_id": confirmed["profile_id"]}); return
            if self.path == "/api/replay/analysis-grant":
                grant_id = self.server.manager.grant_replay_analysis(
                    scope=body.get("scope", ""), seat=body.get("seat") or None)
                self._json(201, {"permission_grant_id": grant_id}); return
            if self.path == "/api/live/record":
                result = self.server.manager.save_completed_record()
                with self.server.state_lock:
                    self.server.published_state = None
                self._json(201, result); return
            if self.path == "/api/live/rematch":
                live = self.server.manager.rematch()
                self.server.activate(live)
                self._json(201, {
                    "session_id": live.session_id,
                    "human_seat": live.human_seat.value,
                }); return
            if self.path == "/api/conversation/ref":
                ref = self.server.manager.issue_position_question(
                    source_kind=body.get("source_kind", ""),
                    requested_subject=body.get("requested_subject", "AMBIGUOUS"),
                    subject_seat=body.get("subject_seat") or None,
                    permission_grant_id=body.get("permission_grant_id") or None,
                )
                self._json(201, ref.public_metadata()); return
            if self.path == "/api/conversation/answer":
                result = self.server.manager.codex.legacy_position(body.get("conversation_ref_id", ""))
                self._json(200, result); return
            if self.path == "/api/conversation/snapshot":
                result = self.server.manager.codex.legacy_position(body.get("conversation_ref_id", ""), snapshot=True)
                self._json(200 if result.get("status") == "ALLOWED" else 400, result); return
            if self.path == "/api/replay/position" and self.server.manager.replay is not None:
                replay = self.server.manager.replay
                if "hand_index" in body:
                    replay.select_hand(int(body["hand_index"]))
                if "event_index" in body:
                    replay.seek(int(body["event_index"]))
                self._json(200, replay.metadata())
                return
            if self.path == "/api/start":
                seed_text = body.get("seed", "").strip()
                seed = int(seed_text) if seed_text else None
                score_values = {seat: body.get(f"score_{seat.value}", "").strip() for seat in Seat}
                supplied_scores = [value != "" for value in score_values.values()]
                if any(supplied_scores) and not all(supplied_scores):
                    raise NavigatorError("INCOMPLETE_START_SCORES")
                position = StartPosition(
                    Seat(body.get("round_wind", "east")),
                    int(body.get("hand_number", "1")),
                    int(body.get("honba", "0")),
                    int(body.get("riichi_deposit_count", "0")) * 1000,
                    Seat(body["human_seat"]) if body.get("human_seat") else None,
                    {seat: int(value) for seat, value in score_values.items()} if all(supplied_scores) else None,
                )
                live = self.server.manager.start(StartRequest(body.get("request_id", ""), body.get("format", ""), RuleSelection(alias=body.get("rule_alias")), seed, position))
                self.server.activate(live)
                if "application/json" not in self.headers.get("Content-Type", ""):
                    self.send_response(303); self.send_header("Location", "/live"); self.end_headers(); return
                self._json(201, {"session_id": live.session_id, "human_seat": live.human_seat.value})
                return
            if self.path == "/api/action" and self.server.manager.active is not None:
                request_id = body.get("request_id") or str(uuid4())
                action_id = body.get("action_id", "")
                with self.server.state_lock:
                    if self.server.busy:
                        raise NavigatorError("ACTION_IN_PROGRESS")
                    legal = {item["action_id"] for item in (self.server.published_state or {}).get("legal_actions", ())}
                    if action_id not in legal:
                        raise NavigatorError("ACTION_NOT_IN_LEGAL_SET", {"action_id": action_id})
                    self.server.busy = True
                threading.Thread(target=self.server.apply_action, args=(request_id, action_id), daemon=True).start()
                self._json(202, {"accepted": True}); return
            if self.path == "/api/checkpoints":
                result = self.server.manager.checkpoint(body.get("display_name", "復帰点"), expected_generation=int(body["expected_generation"]) if "expected_generation" in body else None)
                self._json(201, result); return
            if self.path == "/api/leave-live":
                result = self.server.manager.leave_live()
                if self.server.manager.active is None:
                    with self.server.state_lock:
                        self.server.published_state = None
                self._json(200, result); return
            if self.path == "/api/restore":
                current = self.server.manager.active.session_id if self.server.manager.active else None
                live = self.server.manager.restore(body.get("checkpoint_id", ""), body.get("request_id", ""), expected_active_session_id=body.get("expected_active_session_id", current), expected_generation=int(body.get("expected_generation", "-1")))
                self.server.activate(live)
                self._json(200, {"session_id": live.session_id, "lineage_id": live.lineage_id}); return
            if self.path == "/api/recovery/retry":
                expected_session = body.get("expected_active_session_id") or None
                result = self.server.manager.retry_recovery_operation(
                    body.get("operation_id", ""), expected_active_session_id=expected_session,
                    expected_generation=int(body.get("expected_generation", "-1")),
                )
                live = self.server.manager.active
                if live is None:
                    raise NavigatorError("NO_ACTIVE_SESSION")
                self.server.activate(live)
                self._json(200, {"session_id": live.session_id, "lineage_id": live.lineage_id,
                                 "result": result}); return
            if self.path == "/api/recovery/branch":
                expected_session = body.get("expected_active_session_id") or None
                live = self.server.manager.branch_from_safe_boundary(
                    body.get("operation_id", ""), body.get("request_id", ""),
                    expected_active_session_id=expected_session,
                    expected_generation=int(body.get("expected_generation", "-1")),
                )
                self.server.activate(live)
                self._json(200, {"session_id": live.session_id, "lineage_id": live.lineage_id}); return
            if self.path == "/api/recovery/discard":
                result = self.server.manager.store.discard_prepared_operation(
                    body.get("operation_id", ""), self.server.manager.owner_id,
                    body.get("request_id", ""),
                    expected_generation=int(body.get("expected_generation", "-1")),
                )
                self._json(200, result); return
            self.send_error(404)
        except (NavigatorError, RuleProfileError, ValueError, json.JSONDecodeError) as error:
            if self.path == "/api/replay/open":
                self.server.replay_error = getattr(error, "code", "INVALID_REQUEST")
            self._json(400, {"error": getattr(error, "code", "INVALID_REQUEST"), "details": getattr(error, "details", {})})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Open the Chombo Navigator")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--store", type=Path, default=Path("outputs/navigator"))
    parser.add_argument("--allow-evaluation-records", action="store_true",
                        help="allow development EVALUATION records in Replay")
    args = parser.parse_args()
    from engine.runtime import Runtime
    import signal
    runtime = Runtime(args.store / '.runtime', port=args.port, checkpoint_root=args.store).start()
    runtime.services.manager.allow_evaluation_records = args.allow_evaluation_records
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, runtime.handle_signal)
    print(f"Chombo Navigator: {runtime.url}", flush=True)
    runtime.serve()



if __name__ == "__main__":
    main()
