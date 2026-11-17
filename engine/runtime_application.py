"""Owner-scoped standalone operations; transport and Engine stay separate."""
from copy import deepcopy
from collections import OrderedDict
import base64
import hashlib
import json
import time
from threading import RLock
from uuid import uuid4

from engine.application import ApplicationContext
from engine.application_schema import (INPUTS, STRING, INTEGER, BOOLEAN, SESSION_REF, REPLAY_REF, RULE_REFERENCE, POINT_DECIMAL, CONDITION_PLAYER,
    obj, array, nullable, enum, request_schema, validate, json_tree)
from engine.navigator import NavigatorError, NavigatorSessionManager, StartPosition, StartRequest, _plain
from engine.game_state import Seat
from engine.rule_inputs import RuleSelection, load_selected_profile
from engine.rule_editor import RuleEditorSession, rule_management_model
from engine.rule_drafts import digest as draft_digest
from engine.canonical_record import _strict_load as strict_record_json
from engine.runtime_storage import StorageError
from engine.dialogue_knowledge import LessonKnowledge, KnowledgeUnavailable, LESSONS

# Nested display models remain owned by their existing observer/store validators.
DISPLAY = {"type": "object", "description": "Existing validated owner-visible display model"}
ARTIFACT = obj({"artifact_id": STRING, "media_type": STRING, "sha256": STRING, "size_bytes": INTEGER})
SESSION = obj({"session_ref": SESSION_REF, "seed": STRING, "rule_ref": obj({"profile_id": STRING, "snapshot_id": STRING}), "snapshot": {**DISPLAY, "description": "Owner-visible snapshot. session.read may include one-shot presentation: {session_ref, frames, frame_delay_ms}. Frames are committed display data with no legal actions; never authority for input. Absence means show the current state immediately."}})
REPLAY = obj({"replay_ref": REPLAY_REF, "metadata": DISPLAY})
INTERACTION = obj({"interaction_id": STRING, "state": enum("WAITING_CONFIRMATION", "COMPLETED", "DENIED", "CANCELLED", "EXPIRED", "FAILED"),
    "revision": INTEGER, "reason_code": nullable(STRING), "position_ref": nullable(DISPLAY),
    "confirmation": nullable(obj({"confirmation_id": STRING, "reason_code": STRING, "choices": array(obj({"choice_id": STRING, "label": STRING}))})),
    "send_preview": {"type": "null"}, "result": nullable(DISPLAY), "failure": nullable(DISPLAY)})
INTERACTION_V11 = deepcopy(INTERACTION)
INTERACTION_V11["properties"].update(target=nullable({"oneOf": [
    obj({"kind": enum("POSITION"), "conversation_ref_id": STRING, "intent": enum("SHAPE_ANALYSIS", "PUBLIC_TABLE")}),
    obj({"kind": enum("KNOWLEDGE"), "lesson_id": STRING})]}), knowledge_ref=nullable(DISPLAY))
INTERACTION_V11["properties"]["state"] = enum("VALIDATING", "WAITING_CONFIRMATION", "WAITING_SEND_PERMISSION", "RUNNING", "COMPLETED", "DENIED", "FAILED", "CANCELLED", "EXPIRED")
INTERACTION_V11["required"] += ["target", "knowledge_ref"]
SEND_PREVIEW = obj({'send_preview_id': STRING, 'attempt_id': STRING, 'provider': enum('openai', 'anthropic', 'google'),
    'model': STRING, 'endpoint': STRING, 'config_revision': INTEGER, 'payload': DISPLAY, 'payload_digest': STRING,
    'expires_in_seconds': INTEGER, 'position_ref': nullable(DISPLAY)})
INTERACTION_V11['properties']['send_preview'] = nullable(SEND_PREVIEW)
INTERACTION_V11['properties']['failure'] = nullable(obj({'failure_id': STRING, 'code': STRING, 'retryable': BOOLEAN,
    'delivery_status': enum('NOT_SENT', 'STARTED', 'UNKNOWN'), 'retry_after': {'type': 'integer', 'minimum': 0, 'maximum': 3600}},
    ['failure_id', 'code', 'retryable', 'delivery_status']))
RESULTS_V11 = {key: INTERACTION_V11 for key in ("interaction.create", "interaction.read", "interaction.confirm", "interaction.cancel", "interaction.authorize_send", "interaction.retry")}
RESULTS_V11["interaction.list"] = obj({"interactions": {"type": "array", "maxItems": 128, "items": {"anyOf": [INTERACTION, INTERACTION_V11]}}})

CONDITION_FIELDS = {**{key: STRING for key in ("document_kind", "schema_id", "format_version", "created_at", "request_id", "calculation_basis", "ranking_scope", "evaluation_point", "calculator_version", "input_digest", "status")},
    **{key: DISPLAY for key in ("producer", "integrity", "match_rule_ref", "usage")},
    "aggregate_profile_ref": nullable(DISPLAY), "concept_refs": array(STRING), "evidence": array(DISPLAY)}
CONDITION = obj({**CONDITION_FIELDS, **{key: {"type": "array", "items": DISPLAY, "maxItems": 10000} for key in ("candidates", "partial_candidates")},
    "complete_search_evidence": DISPLAY, "limit_reason": STRING}, list(CONDITION_FIELDS))
RESULTS = {
    **{key: SESSION for key in ("session.start", "session.read", "session.rematch", "checkpoint.restore", "recovery.branch")},
    "session.action": obj({"accepted": BOOLEAN, "session_ref": SESSION_REF}),
    "session.leave": obj({"result": DISPLAY}),
    "session.save_record": obj({"artifact": ARTIFACT, "record_id": STRING, "replay_ref": REPLAY_REF}),
    **{key: obj({"result": DISPLAY}) for key in ("checkpoint.list", "checkpoint.save", "checkpoint.rename", "checkpoint.delete", "recovery.retry", "recovery.discard", "rules.list", "rules.draft_read", "rules.draft_discard", "rules.draft_create", "rules.draft_save", "rules.draft_confirm", "rules.custom_discard")},
    "recovery.list": obj({"candidates": array(DISPLAY)}),
    "replay.read": obj({"replay_ref": REPLAY_REF, "metadata": DISPLAY, "snapshot": DISPLAY, "hands": array(DISPLAY)}),
    "replay.open": REPLAY, "replay.seek": REPLAY,
    "replay.grant": obj({"permission_grant_id": STRING}), "replay.revoke": obj({"revoked": BOOLEAN}),
    "position.issue": obj({"position_ref": DISPLAY}),
    **{key: INTERACTION for key in ("interaction.create", "interaction.read", "interaction.confirm", "interaction.cancel")},
    "condition.calculate": CONDITION,
    "condition.query": obj({"status": STRING, "reason_code": nullable(STRING), "response": nullable(CONDITION), "display_points": DISPLAY}, ["status", "reason_code", "response"]),
    "condition.points_query": obj({
        "status": enum("ACHIEVABLE", "UNREACHABLE", "INDETERMINATE_LIMIT", "KNOWLEDGE_UNQUALIFIED"),
        "reason_code": nullable(STRING), "calculator_version": STRING, "input_digest": STRING,
        "goal": obj({"participant_id": CONDITION_PLAYER, "target_points": POINT_DECIMAL,
                     "comparison": enum("GT", "GTE"), "aggregation_policy": enum("EXACT_SUM_NO_EXTRA_ADJUSTMENTS")}),
        "match_rule_ref": DISPLAY, "concept_refs": array(STRING), "usage": DISPLAY,
        "candidates": array(DISPLAY), "partial_candidates": array(DISPLAY),
        "complete_search_evidence": nullable(DISPLAY),
    }),
    "condition.options": obj({"rules": array(obj({"name": STRING, "ref": RULE_REFERENCE,
        "points_per_player": INTEGER, "sudden_death": BOOLEAN})), "aggregate_status": enum("RULES_INCOMPLETE"), "aggregate_reason": STRING}),
    "operation.read": obj({"operation_status": enum("SUCCEEDED", "REJECTED", "FAILED"), "result": DISPLAY}),
}
DURABLE = frozenset({"session.start", "session.action", "session.leave", "session.rematch", "session.save_record",
    "checkpoint.save", "checkpoint.rename", "checkpoint.delete", "checkpoint.restore", "recovery.retry", "recovery.branch", "recovery.discard",
    "rules.draft_create", "rules.draft_save", "rules.draft_confirm", "rules.draft_discard", "rules.custom_discard"})
DYNAMIC = frozenset({"condition.points_query", "condition.query", "condition.options", "replay.read","session.read", "checkpoint.list", "recovery.list", "rules.list", "rules.draft_read", "operation.read", "interaction.read", "interaction.list"})


def http_status(result):
    if result["status"] == "SUCCEEDED":
        return 200
    code = result["error"]["code"]
    if result["status"] == "FAILED":
        return 503 if code == "STORAGE_UNAVAILABLE" else 500
    return {"PERMISSION_DENIED": 403, "RESOURCE_NOT_FOUND": 404, "REQUEST_ID_CONFLICT": 409,
        "REVISION_CONFLICT": 409, "SEND_PREVIEW_CONFLICT": 409, "FAILURE_CONFLICT": 409, "ACTIVE_SESSION_CONFLICT": 409, "RECOVERY_REQUIRED": 409,
        "RESOURCE_LIMIT": 413, "CAPABILITY_UNAVAILABLE": 503, "LLM_UNCONFIGURED": 503, "LESSON_UNAVAILABLE": 422, "KNOWLEDGE_DIGEST_MISMATCH": 422, "REVIEW_REQUIRED": 422,
        "ACTION_NOT_IN_LEGAL_SET": 422, "RULE_PROFILE_INCOMPLETE": 422,
        "UNSUPPORTED_RULE_VALUE": 422}.get(code, 400)


class RuntimeApplication:
    operations = frozenset(RESULTS) | frozenset(RESULTS_V11)

    def __init__(self, runtime, *, send_port, clock=time.monotonic):
        self.runtime = runtime
        self.owner = "local-user"  # One OS-user runtime; never accepted from JSON.
        self.manager = NavigatorSessionManager(runtime.storage.store, owner_id=self.owner)
        self.rules = runtime.storage.rules
        self.clock = clock
        self.lock = RLock()
        self.requests = {}
        self.replay_scopes = OrderedDict()
        self.identities = {}
        self.refs = set()
        self.interactions = {}
        self.knowledge = LessonKnowledge(runtime.config["assets_dir"])
        from engine.byok_dialogue import ByokDialogue
        self.byok = ByokDialogue(self, send_port)
        self.replay_id = None
        self.presentation = None

    def context(self, credential_binding=None):
        return ApplicationContext(self.owner, self.manager.conversations, frozenset(self.refs), credential_binding)

    def issue_replay_request_scope(self):
        """Bound view/navigation retries without consuming durable request quota."""
        with self.lock:
            token = uuid4().hex
            self.replay_scopes[token] = {"next": 1, "requests": OrderedDict()}
            if len(self.replay_scopes) > 16:
                self.replay_scopes.popitem(last=False)
            return token

    def replay_request_cache(self, request):
        key = request["request_id"]
        if not key.startswith("rp."):
            return None
        parts = key.split(".")
        if len(parts) != 3 or not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 16:
            raise NavigatorError("INVALID_REQUEST")
        if request["operation"] not in {"replay.read", "replay.seek"}:
            raise NavigatorError("INVALID_REQUEST")
        scope = self.replay_scopes.get(parts[1])
        if scope is None:
            raise NavigatorError("REFERENCE_EXPIRED")
        sequence = int(parts[2])
        if str(sequence) != parts[2] or sequence < 1:
            raise NavigatorError("INVALID_REQUEST")
        return scope, sequence

    def bind_request(self, request, context):
        with self.lock:
            if context.owner_id != self.owner:
                return "RESOURCE_NOT_FOUND"
            iid = request['input'].get('interaction_id')
            item = self.interactions.get(iid)
            if item and item.get('binding') and item['binding'] != context.credential_binding:
                return 'RESOURCE_NOT_FOUND'
            key = request["request_id"]
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            try:
                scoped = self.replay_request_cache(request)
            except NavigatorError as error:
                return error.code
            if scoped is not None:
                scope, sequence = scoped
                old = scope["requests"].get(sequence)
                if old:
                    return None if old[0] == digest else "REQUEST_ID_CONFLICT"
                if sequence < scope["next"]:
                    return "REFERENCE_EXPIRED"
                scope["requests"][sequence] = (digest, None)
                scope["next"] = sequence + 1
                if len(scope["requests"]) > 128:
                    scope["requests"].popitem(last=False)
                return None
            old = self.identities.get(key)
            record = next((r for r in self.runtime.ledger.records if r["owner"] == self.owner and r["request_id"] == key), None)
            if (old and old != digest) or (record and record["digest"] != digest):
                return "REQUEST_ID_CONFLICT"
            if not old and len(self.identities) >= 1024:
                return "RESOURCE_LIMIT"
            self.identities[key] = digest
            return None

    def validate_request(self, request):
        schema = deepcopy(request_schema(request["operation"], request["contract_version"]))
        # Existing authoritative document validators own these nested schemas.
        if request["operation"] == "condition.calculate":
            schema["properties"]["input"] = obj({key: DISPLAY for key in ("request", "state", "support_ref", "aggregate_profile")}, ["request"])
        elif request["operation"] == "rules.draft_save":
            schema["properties"]["input"]["properties"]["overrides"] = DISPLAY
        validate(request, schema)

    def invoke(self, application, request, context):
        with self.lock:
            if context.owner_id != self.owner:
                return application._envelope(request, code="RESOURCE_NOT_FOUND")
            key, op = request["request_id"], request["operation"]
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            try:
                scoped = self.replay_request_cache(request)
            except NavigatorError as error:
                return application._envelope(request, code=error.code)
            cache, cache_key = (scoped[0]["requests"], scoped[1]) if scoped else (self.requests, key)
            prior = cache.get(cache_key)
            if scoped and prior is None:
                return application._envelope(request, code="REFERENCE_EXPIRED")
            if prior and prior[0] != digest:
                return application._envelope(request, code="REQUEST_ID_CONFLICT")
            try:
                durable = next((r for r in self.runtime.ledger.records if r["owner"] == self.owner and r["request_id"] == key), None)
                if durable:
                    if durable["digest"] != digest:
                        raise StorageError("REQUEST_ID_CONFLICT")
                    if durable["state"] == "PREPARED":
                        raise StorageError("RECOVERY_REQUIRED")
                    if durable["state"] == "DELETED":
                        raise StorageError("RESOURCE_NOT_FOUND")
                    payload, _ = self.runtime.artifacts.read(self.owner, durable["result_ref"])
                    return json.loads(payload)
                if prior and prior[1] is not None:
                    # Revalidate interaction authority even when returning a cached terminal response.
                    result = deepcopy(prior[1])
                    if op == "condition.calculate" and result["status"] == "SUCCEEDED":
                        from engine.condition_skill import qualified_concept_ids
                        if not set(request["input"]["request"]["concept_refs"]) <= qualified_concept_ids(application.concepts_dir):
                            return application._envelope(request, code="REVIEW_REQUIRED")
                    if op.startswith("interaction.") and op != "interaction.list" and result["status"] == "SUCCEEDED":
                        bound = self.interactions[result['data']['interaction_id']].get('binding')
                        if bound and bound != context.credential_binding:
                            raise NavigatorError('RESOURCE_NOT_FOUND')
                        current = self._interaction(result["data"]["interaction_id"])
                        if request["contract_version"] == "1.0.0" and "target" in current:
                            raise NavigatorError("CAPABILITY_UNAVAILABLE")
                        return self.response(application, request, current)
                    if op not in DYNAMIC:
                        return result
                if not scoped and not prior and len(self.requests) >= 1024:
                    raise StorageError("RESOURCE_LIMIT")
                operation_id = str(uuid4())
                if op in DURABLE:
                    self.runtime.ledger.prepare(self.owner, key, digest, operation_id)
                data = self.execute(application, request, context)
                try:
                    data = _plain(data)
                    if op == "condition.calculate":
                        from engine.condition_documents import load_condition_document
                        load_condition_document(json.dumps(data, allow_nan=False))
                    else:
                        json_tree(data)
                        if op == "condition.query" and data["response"] is not None:
                            from engine.condition_documents import load_condition_document
                            load_condition_document(json.dumps(data["response"], allow_nan=False))
                    validate(data, (RESULTS_V11 if request["contract_version"] == "1.1.0" and op in RESULTS_V11 and (op == "interaction.list" or "target" in data) else RESULTS)[op])
                except (ValueError, TypeError, KeyError) as error:
                    raise RuntimeError("OUTPUT_INVALID") from error
                result = self.response(application, request, data)
                if op in DURABLE:
                    result["meta"]["operation_id"] = operation_id
            except Exception as error:
                code = getattr(error, "code", None)
                if isinstance(error, (ValueError, NavigatorError)):
                    code = code or "INVALID_REQUEST"
                if code is None:
                    code = "STORAGE_UNAVAILABLE" if isinstance(error, OSError) else "INTERNAL_ERROR"
                result = application._envelope(request, code=code, failed=code in {"STORAGE_UNAVAILABLE", "INTERNAL_ERROR"})
            if op in DURABLE and not durable:
                record = next((r for r in self.runtime.ledger.records if r["owner"] == self.owner and r["request_id"] == key), None)
                if record and record["state"] == "PREPARED" and result["status"] in {"SUCCEEDED", "REJECTED"}:
                    try:
                        artifact = self.runtime.artifacts.save(self.owner, json.dumps(result).encode(), "application/json")
                        self.runtime.ledger.finish(self.owner, key, artifact)
                    except (OSError, StorageError):
                        result = application._envelope(request, code="STORAGE_UNAVAILABLE", failed=True)
            cached = None if op in {"session.read", "replay.read", "interaction.list"} else deepcopy(result)
            if cached and op.startswith("interaction.") and result["status"] == "SUCCEEDED":
                cached["data"] = {"interaction_id": result["data"]["interaction_id"]}
            cache[cache_key] = (digest, cached)
            return result

    def response(self, application, request, data):
        op = request["operation"]
        result = application._envelope(request, data=data)
        rule = data.get("rule_ref")
        if op == "condition.calculate" or (op == "condition.query" and data["response"] is not None):
            condition = data if op == "condition.calculate" else data["response"]
            rule = condition["match_rule_ref"]
            if condition["status"] in {"ACHIEVABLE", "UNREACHABLE", "INDETERMINATE_LIMIT"}:
                result["meta"]["applied_concept_ids"] = list(condition["concept_refs"])
        if op == "condition.points_query":
            rule = data["match_rule_ref"]
            if data["status"] in {"ACHIEVABLE", "UNREACHABLE", "INDETERMINATE_LIMIT"}:
                result["meta"]["applied_concept_ids"] = list(data["concept_refs"])
        if rule:
            result["meta"]["rule_ref"] = {"kind": "MATCH", "profile_id": rule["profile_id"], "snapshot_id": rule["snapshot_id"]}
            result["meta"]["engine_version"] = self.manager.store.producer["engine_version"]
        if op.startswith("interaction.") and op != "interaction.list" and data["state"] == "COMPLETED":
            result["meta"]["position_ref"] = data["position_ref"]
            result["meta"]["applied_concept_ids"] = data["result"].get("applied_concept_ids", [])
        return result

    def session_ref(self):
        live = self.manager.active
        if live is None:
            raise NavigatorError("RESOURCE_NOT_FOUND")
        return {"session_id": live.session_id, "lineage_id": live.lineage_id, "boundary_revision": live.boundary_revision}

    def session(self):
        live = self.manager.active
        return {"session_ref": self.session_ref(), "seed": str(live.engine.seed),
            "rule_ref": {"profile_id": live.profile.profile_id, "snapshot_id": live.profile.snapshot_id}, "snapshot": live.snapshot()}

    def _advance_for_presentation(self):
        live = self.manager.active
        frames = [live.snapshot()]
        live.engine.on_event = lambda: frames.append(live.snapshot())
        try:
            live.engine.advance()
        finally:
            live.engine.on_event = None
        self._publish_presentation(frames)

    def _publish_presentation(self, frames):
        # Already committed, owner-projected display data. No Engine sleeps or
        # callable intermediate legal actions; keep only the current boundary.
        visible = []
        for frame in frames:
            if frame.get("human_seat") not in {"east", "south", "west", "north"}:
                continue
            value = deepcopy(frame)
            value["legal_actions"] = []
            if visible and visible[-1].get("event_count") == value.get("event_count"):
                visible[-1] = value
            else:
                visible.append(value)
        self.presentation = (self.session_ref(), visible)

    def check_session(self, ref):
        if ref != self.session_ref():
            raise NavigatorError("REVISION_CONFLICT")

    def replay_ref(self):
        replay = self.manager.replay
        if replay is None or self.replay_id is None:
            raise NavigatorError("RESOURCE_NOT_FOUND")
        return {"replay_id": self.replay_id, "record_id": replay.record["record_id"],
            "document_hash": replay.record["integrity"]["document_hash"], "hand_index": replay.hand_index, "event_index": replay.event_index}

    def check_replay(self, ref):
        if ref != self.replay_ref():
            raise NavigatorError("REVISION_CONFLICT")

    def artifact(self, raw, media_type="application/json"):
        aid = self.runtime.artifacts.save(self.owner, raw, media_type)
        entry = self.runtime.artifacts.index[aid]
        return {"artifact_id": aid, "media_type": media_type, "sha256": entry["sha256"], "size_bytes": entry["size"]}

    def execute(self, app, request, context=None):
        op, i, rid = request["operation"], request["input"], request["request_id"]
        m = self.manager
        if "session_ref" in i:
            self.check_session(i["session_ref"])
        if "replay_ref" in i:
            self.check_replay(i["replay_ref"])
        if op == "session.start":
            if m.active is not None:
                raise NavigatorError("ACTIVE_SESSION_CONFLICT")
            rules = i["rules"]
            selection = RuleSelection(alias=rules.get("alias"), reference=rules.get("reference"),
                store_directory=self.rules.directory if rules["kind"] == "CUSTOM_CONFIRMED" else None)
            p = i.get("position", {})
            position = StartPosition(Seat(p.get("round_wind", "east")), p.get("hand_number", 1), p.get("honba", 0), p.get("riichi_deposits", 0),
                Seat(p["human_seat"]) if p.get("human_seat") else None,
                {Seat(k): v for k, v in p["seat_scores"].items()} if "seat_scores" in p else None)
            deferred = m.defer_initial_advance
            m.defer_initial_advance = True
            try:
                m.start(StartRequest(rid, i["format"], selection, int(i["seed"]) if "seed" in i else None, position))
                self._advance_for_presentation()
                return self.session()
            except Exception:
                # Initial progress is part of starting the session. A failed
                # start must not occupy the owner or survive in the start cache.
                m.active = None
                m._starts.pop(rid, None)
                self.presentation = None
                raise
            finally:
                m.defer_initial_advance = deferred
        if op == "session.read":
            if i["session_id"] != self.session_ref()["session_id"]:
                raise NavigatorError("RESOURCE_NOT_FOUND")
            result = self.session()
            ref = result["session_ref"]
            frames = self.presentation[1] if self.presentation and self.presentation[0] == ref else []
            self.presentation = None
            if frames:
                result["snapshot"]["presentation"] = {"session_ref": ref, "frames": deepcopy(frames), "frame_delay_ms": 800}
            return result
        if op == "session.action":
            result = m.submit_action(rid, i["action_id"])
            self._publish_presentation(m.take_presentation_snapshots(rid))
            return {"accepted": result["accepted"], "session_ref": self.session_ref()}
        if op == "session.rematch":
            deferred = m.defer_initial_advance
            m.defer_initial_advance = True
            try:
                m.rematch()
            finally:
                m.defer_initial_advance = deferred
            self._advance_for_presentation()
            return self.session()
        if op in {"session.save_record", "session.leave"}:
            result = m.save_completed_record() if op.endswith("save_record") else m.leave_live()
            result.pop("path", None)
            if m.replay and m.active is None:
                self.replay_id = str(uuid4())
            if op == "session.leave":
                m.active = None
                return {"result": result}
            raw = json.dumps(m.replay.record, ensure_ascii=False).encode()
            return {"artifact": self.artifact(raw), "record_id": m.replay.record["record_id"], "replay_ref": self.replay_ref()}
        if op == "checkpoint.list":
            return {"result": m.store.list(self.owner)}
        if op == "checkpoint.save":
            return {"result": m.checkpoint(i["display_name"], expected_generation=i["expected_generation"])}
        if op in {"checkpoint.rename", "checkpoint.delete"}:
            args = (i["checkpoint_id"], self.owner, i["display_name"] if op.endswith("rename") else rid)
            return {"result": getattr(m.store, op.split(".")[1])(*args, expected_generation=i["expected_generation"])}
        if op == "checkpoint.restore":
            m.restore(i["checkpoint_id"], rid, expected_active_session_id=i["expected_active_session_id"], expected_generation=i["expected_generation"])
            return self.session()
        if op == "recovery.list":
            return {"candidates": m.store.navigator_recovery_candidates(self.owner)}
        if op == "recovery.discard":
            return {"result": m.store.discard_prepared_operation(i["operation_id"], self.owner, rid, expected_generation=i["expected_generation"])}
        if op == "recovery.retry":
            return {"result": m.retry_recovery_operation(**i)}
        if op == "recovery.branch":
            m.branch_from_safe_boundary(i["operation_id"], rid, expected_active_session_id=i["expected_active_session_id"], expected_generation=i["expected_generation"])
            return self.session()
        if op == "replay.open":
            raw = base64.b64decode(i["record_base64"], validate=True)
            if len(raw) > 64 * 1024 * 1024:
                raise NavigatorError("RESOURCE_LIMIT")
            # Local development records retain EVALUATION. Only an exact record
            # already written by this owner may use the evaluation reader path;
            # arbitrary imported evaluation records remain rejected.
            candidate = strict_record_json(raw)
            own_evaluation = candidate.get("baseline_status") == "EVALUATION" and any(
                strict_record_json(path.read_bytes()) == candidate
                for path in m.store.records.glob("*.json") if not path.is_symlink())
            previous = m.allow_evaluation_records
            try:
                m.allow_evaluation_records = own_evaluation
                m.open_record(raw, i["filename"])
            finally:
                m.allow_evaluation_records = previous
            self.replay_id = str(uuid4())
            return {"replay_ref": self.replay_ref(), "metadata": m.replay.metadata()}
        if op == "replay.read":
            return {"replay_ref": self.replay_ref(), "metadata": m.replay.metadata(),
                    **m.replay.display_snapshot(i["perspective"], i["reveal_all"])}
        if op == "replay.seek":
            # Validate on a separate selector before changing the current position.
            hand, event = m.replay.hand_index, m.replay.event_index
            try:
                m.replay.select_hand(i["hand_index"])
                m.replay.seek(i["event_index"])
            except Exception:
                m.replay.select_hand(hand)
                m.replay.seek(event)
                raise
            return {"replay_ref": self.replay_ref(), "metadata": m.replay.metadata()}
        if op == "replay.grant":
            return {"permission_grant_id": m.grant_replay_analysis(scope=i["scope"], seat=i.get("seat"))}
        if op == "replay.revoke":
            m.revoke_replay_analysis(i["permission_grant_id"])
            return {"revoked": True}
        if op == "position.issue":
            live_source = i["source_kind"] != "COMPLETED_REPLAY"
            if (live_source and ("session_ref" not in i or "replay_ref" in i)) or (not live_source and ("replay_ref" not in i or "session_ref" in i)):
                raise ValueError("INVALID_REQUEST")
            ref = m.issue_position_question(**{k: v for k, v in i.items() if k not in {"session_ref", "replay_ref"}})
            self.refs.add(ref.conversation_ref_id)
            return {"position_ref": {**ref.public_metadata(), "projection_digest": ref.projection_digest, "expires_at": ref.expires_at}}
        if op == "rules.list":
            return {"result": rule_management_model(self.rules)}
        if op == "rules.draft_read":
            from engine.rule_editor import HIDDEN_EDITOR_DIMENSIONS, VALUE_NAMES, VALUE_FIELD_LABELS, _qualified_yaku_names
            editor = RuleEditorSession.open(self.rules, i["draft_id"], actor_id=self.owner)
            return {"result": {"model": editor.view_model(), "draft": editor.draft,
                "draft_digest": draft_digest(editor.draft),
                "hidden_dimensions": sorted(HIDDEN_EDITOR_DIMENSIONS), "value_names": VALUE_NAMES,
                "field_labels": VALUE_FIELD_LABELS, "yaku_names": _qualified_yaku_names()}}
        if op == "rules.draft_create":
            editor = RuleEditorSession.create(self.rules, i["base_ref"], i["display_name"], actor_id=self.owner)
            return {"result": editor.save()}
        if op in {"rules.draft_save", "rules.draft_confirm", "rules.draft_discard"}:
            editor = RuleEditorSession.open(self.rules, i["draft_id"], actor_id=self.owner)
            if draft_digest(editor.draft) != i["expected_draft_digest"]:
                raise NavigatorError("REVISION_CONFLICT")
            if op == "rules.draft_discard":
                return {"result": self.rules.discard_custom_ruleset(editor.draft["custom_ruleset_id"])}
            if op.endswith("confirm"):
                return {"result": editor.confirm()}
            editor.rename(i["display_name"])
            for dimension in list(editor.draft["overrides"]):
                if dimension not in i["overrides"]:
                    editor.unset_override(dimension)
            for dimension, value in i["overrides"].items():
                editor.set_override(dimension, value, request_summary=i["request_summary"])
            editor.synchronize_fixed_dimensions(request_summary=i["request_summary"])
            return {"result": editor.save()}
        if op == "rules.custom_discard":
            model = rule_management_model(self.rules)
            selected = next((x for x in model["confirmed"] if x["ref"]["profile_id"] == i["profile_id"] and x["ref"]["snapshot_id"] == i["expected_snapshot_id"]), None)
            if selected is None:
                raise NavigatorError("REVISION_CONFLICT")
            return {"result": self.rules.discard_custom_ruleset(selected["custom_ruleset_id"])}
        if op == "condition.options":
            from engine.condition_form import form_options
            return form_options(self.rules)
        if op == "condition.query":
            from engine.condition_form import query
            return query(i, store=self.rules, concepts_dir=app.concepts_dir,
                         producer=self.manager.store.producer, request_id=request["request_id"])
        if op == "condition.points_query":
            from engine.condition_points import points_query
            return points_query(i, store=self.rules, concepts_dir=app.concepts_dir,
                                producer=self.manager.store.producer)
        if op == "condition.calculate":
            from engine.condition_skill import calculate_condition_documents, qualified_concept_ids
            from engine.condition_documents import load_condition_document
            load_condition_document(json.dumps(i["request"], allow_nan=False))
            if i["request"]["position_source"]["kind"] == "SUPPORT_REF" or "support_ref" in i:
                # Pasted documents cannot stand in for an owner-bound server
                # support reference. This UI explicitly imports general inputs.
                raise NavigatorError("CAPABILITY_UNAVAILABLE")
            reference = i["request"]["match_rule_ref"]
            model = rule_management_model(self.rules)
            approved = next((x["ref"] for x in model["official"] + model["confirmed"] if
                x["ref"]["profile_id"] == reference["profile_id"] and x["ref"]["snapshot_id"] == reference["snapshot_id"]), None)
            if approved is None:
                raise NavigatorError("REVIEW_REQUIRED")
            selection = RuleSelection(reference=approved, store_directory=self.rules.directory if approved["kind"] == "CUSTOM_CONFIRMED" else None)
            profile = load_selected_profile(selection, route="match")
            return dict(calculate_condition_documents(i["request"], profile,
                qualified_concepts=qualified_concept_ids(app.concepts_dir), **{k: v for k, v in i.items() if k != "request"}))
        if op.startswith("interaction."):
            return self.interaction(app, op, i, version=request["contract_version"], binding=context.credential_binding if context else None)
        if op == "operation.read":
            record = next((r for r in self.runtime.ledger.records if r["owner"] == self.owner and r["operation_id"] == i["operation_id"]), None)
            if record is None:
                raise NavigatorError("RESOURCE_NOT_FOUND")
            if record["state"] == "PREPARED":
                raise StorageError("RECOVERY_REQUIRED")
            if record["state"] == "DELETED":
                raise NavigatorError("RESOURCE_NOT_FOUND")
            raw, _ = self.runtime.artifacts.read(self.owner, record["result_ref"])
            result = json.loads(raw)
            return {"operation_status": result["status"], "result": result}
        raise NavigatorError("CAPABILITY_UNAVAILABLE")

    def dialogue_capabilities(self):
        lessons = []
        for lesson in LESSONS:
            try:
                self.knowledge.resolve(lesson)
                reason = None
            except KnowledgeUnavailable as error:
                reason = error.code
            lessons.append({"lesson_id": lesson, "available": reason is None, "reason_code": reason})
        return {"lessons": lessons, "standalone_byok": {"available": False, "reason_code": "LLM_UNCONFIGURED",
            'implemented': True, 'providers': [{'provider': provider, 'model': model,
                'invoke_path': '/api/v1/credentials/' + provider + '/invoke'} for provider, model in self.byok.adapter.models.items()]}}

    def _interaction(self, iid):
        item = self.interactions.get(iid)
        if item is None:
            raise NavigatorError("RESOURCE_NOT_FOUND")
        self.byok.check(item)
        if item['public']['state'] == 'RUNNING' and self.clock() >= item.get('attempt_deadline', item['deadline']):
            self.byok.cancel(item)
            from engine.byok_contract import ProviderFailure
            self.byok.fail(item, ProviderFailure('DELIVERY_UNKNOWN' if item['attempt'].started else 'TRANSPORT_FAILED',
                'UNKNOWN' if item['attempt'].started else 'NOT_SENT', True))
            item['public']['revision'] += 1
        if item.get('test'):
            decision = {'status': 'ALLOWED'}
        elif item.get("knowledge"):
            decision = {"status": "ALLOWED"}
            try:
                reference, _ = self.knowledge.resolve(item["knowledge"]["lesson_id"])
                if reference != item["knowledge"]:
                    raise KnowledgeUnavailable("KNOWLEDGE_DIGEST_MISMATCH")
            except KnowledgeUnavailable as error:
                decision = {"status": "REFERENCE_INVALID", "reason_code": error.code}
        else:
            decision = self.manager.conversations.authorize(item["ref"])
        if (item["public"]["state"] == "COMPLETED" or "target" in item["public"]) and not item.get("knowledge") and not item.get('test'):
            try:
                self.runtime.application._qualified()
            except (OSError, ValueError, KeyError, TypeError):
                decision = {"status": "REFERENCE_INVALID", "reason_code": "REVIEW_REQUIRED"}
        if self.clock() >= item["deadline"] or decision["status"] in {"REFERENCE_INVALID", "POSITION_UNAVAILABLE"}:
            if item["public"]["state"] != "EXPIRED":
                item["public"].update(state="EXPIRED", reason_code=decision.get("reason_code") or "REFERENCE_EXPIRED", result=None, confirmation=None,
                    revision=item["public"]["revision"] + 1)
        elif item["public"]["state"] in {"COMPLETED", "WAITING_SEND_PERMISSION", "RUNNING"} and decision["status"] != "ALLOWED":
            item["public"].update(state="EXPIRED", reason_code=decision.get("reason_code"), result=None, revision=item["public"]["revision"] + 1)
        if item["public"]["state"] in {"EXPIRED", "CANCELLED"}:
            self.byok.cancel(item)
            item['public']['send_preview'] = None
            item.pop("question", None)
            for key in ('evidence', 'facts', 'preview'):
                item.pop(key, None)
        return deepcopy(item["public"])

    def _run(self, app, item):
        result = app.invoke({"contract_id": "chombo.application", "contract_version": "1.0.0", "request_id": str(uuid4()),
            "operation": "position.analyze", "input": {"conversation_ref_id": item["ref"]}}, self.context())
        public = item["public"]
        if result["status"] != "SUCCEEDED":
            public.update(state="DENIED", reason_code=result["error"]["code"], result=None, confirmation=None)
        elif result["data"]["status"] == "CLARIFICATION_REQUIRED":
            public.update(state="WAITING_CONFIRMATION", reason_code=result["data"]["reason_code"], confirmation={"confirmation_id": str(uuid4()),
                "reason_code": result["data"]["reason_code"], "choices": [{"choice_id": "SELF", "label": "自家"}, {"choice_id": "PUBLIC_TABLE", "label": "公開表"}] if result["data"]["reason_code"] == "SUBJECT_AMBIGUOUS" else []})
        elif result["data"]["status"] == "ALLOWED":
            public.update(state="COMPLETED", reason_code=None, confirmation=None, result=result["data"])
        elif result["data"]["status"] in {"REFERENCE_INVALID", "POSITION_UNAVAILABLE"}:
            public.update(state="EXPIRED", reason_code=result["data"]["reason_code"], confirmation=None, result=None)
        else:
            public.update(state="DENIED", reason_code=result["data"]["reason_code"], confirmation=None, result=None)
        ref, _ = self.manager.conversations._refs[item["ref"]]
        public["position_ref"] = {**ref.public_metadata(), "projection_digest": ref.projection_digest, "expires_at": ref.expires_at}
        return deepcopy(public)

    def interaction(self, app, op, i, *, version="1.0.0", binding=None):
        if op == "interaction.list":
            if i["runtime_instance_id"] != self.runtime.instance_id:
                raise NavigatorError("INSTANCE_MISMATCH")
            return {"interactions": [self._interaction(iid) for iid, item in self.interactions.items()
                                     if not item.get('binding') or item['binding'] == binding]}
        if op == "interaction.create" and version == "1.1.0":
            return self._create_v11(app, i, binding=binding)

        if op == "interaction.create":
            if i["runtime_instance_id"] != self.runtime.instance_id:
                raise NavigatorError("INSTANCE_MISMATCH")
            if i["channel"] != "LOCAL_TEMPLATE" or "question" in i:
                raise NavigatorError("CAPABILITY_UNAVAILABLE")
            if i["conversation_ref_id"] not in self.refs:
                raise NavigatorError("RESOURCE_NOT_FOUND")
            root, _ = self.manager.conversations._refs[i["conversation_ref_id"]]
            if i["intent"] == "PUBLIC_TABLE" and root.requested_subject != "PUBLIC_TABLE":
                raise ValueError("INVALID_REQUEST")
            if len(self.interactions) >= 128:
                raise NavigatorError("RESOURCE_LIMIT")
            iid = str(uuid4())
            item = {"ref": i["conversation_ref_id"], "deadline": self.clock() + 900,
                "public": {"interaction_id": iid, "state": "DENIED", "revision": 0, "reason_code": None,
                    "position_ref": None, "confirmation": None, "send_preview": None, "result": None, "failure": None}}
            self.interactions[iid] = item
            return self._run(app, item)
        iid = i["interaction_id"]
        public = self._interaction(iid)
        item = self.interactions[iid]
        if version == "1.0.0" and "target" in public:
            raise NavigatorError("CAPABILITY_UNAVAILABLE")
        if op == "interaction.read":
            return public
        if public["state"] == "EXPIRED":
            return public
        if public["revision"] != i["expected_revision"]:
            raise NavigatorError("REVISION_CONFLICT")
        if op == "interaction.cancel":
            if public["state"] not in {"WAITING_CONFIRMATION", "FAILED", "VALIDATING", "RUNNING", "WAITING_SEND_PERMISSION"}:
                public["reason_code"] = "ALREADY_TERMINAL"
                return public
            item["public"].update(state="CANCELLED", reason_code="USER_CANCELLED", confirmation=None, result=None, revision=public["revision"] + 1)
            self.byok.cancel(item)
            item['public']['send_preview'] = None
            if item.get('binding'):
                item['public']['failure'] = {'failure_id': str(uuid4()), 'code': 'USER_CANCELLED', 'retryable': False,
                    'delivery_status': 'UNKNOWN' if item.get('attempt') and item['attempt'].started else 'NOT_SENT'}
            item.pop("question", None)
            for key in ('evidence', 'facts', 'preview'):
                item.pop(key, None)
            return deepcopy(item["public"])
        if op in {'interaction.authorize_send', 'interaction.retry'}:
            if version != '1.1.0' or item.get('channel') != 'STANDALONE_BYOK':
                raise NavigatorError('CAPABILITY_UNAVAILABLE')
            return self.byok.operation(item, op, i)
        confirmation = public["confirmation"]
        if public["state"] != "WAITING_CONFIRMATION" or not confirmation or confirmation["confirmation_id"] != i["confirmation_id"]:
            raise NavigatorError("CONFIRMATION_CONFLICT")
        if i["choice_id"] not in {x["choice_id"] for x in confirmation["choices"]}:
            raise ValueError("INVALID_REQUEST")
        ref = self.manager.conversations.resolve_subject(item["ref"], i["choice_id"])
        self.refs.add(ref.conversation_ref_id)
        item["ref"] = ref.conversation_ref_id
        item["public"]["revision"] += 1
        result = self._run(app, item)
        if item.get("channel") == "STANDALONE_BYOK" and result["state"] == "COMPLETED":
            self.byok.prepare(item)
        return deepcopy(item["public"])

    def _create_v11(self, app, inputs, *, binding=None):
        i = deepcopy(inputs)
        if i["runtime_instance_id"] != self.runtime.instance_id:
            raise NavigatorError("INSTANCE_MISMATCH")
        if len(i.get("question", "").encode("utf-8")) > 16384:
            raise NavigatorError("RESOURCE_LIMIT")
        if i["channel"] == "CODEX":
            raise NavigatorError("CAPABILITY_UNAVAILABLE")
        if i["channel"] == "STANDALONE_BYOK" and "question" not in i:
            raise ValueError("INVALID_REQUEST")
        target = i.get("target")
        if target is None and i["channel"] == "LOCAL_TEMPLATE":
            return self.interaction(app, "interaction.create", i)
        if target is None:
            target = {"kind": "POSITION", "conversation_ref_id": i["conversation_ref_id"], "intent": i["intent"]}
        if i["channel"] == "LOCAL_TEMPLATE" and "question" in i:
            raise NavigatorError("CAPABILITY_UNAVAILABLE")
        if len(self.interactions) >= 128:
            raise NavigatorError("RESOURCE_LIMIT")
        if target["kind"] == "POSITION":
            local = {"runtime_instance_id": i["runtime_instance_id"], "channel": "LOCAL_TEMPLATE",
                     "conversation_ref_id": target["conversation_ref_id"], "intent": target["intent"]}
            public = self.interaction(app, "interaction.create", local)
            item = self.interactions[public["interaction_id"]]
            item["public"].update(target=target, knowledge_ref=None)
        else:
            reference, content = self.knowledge.resolve(target["lesson_id"])
            iid = str(uuid4())
            item = {"knowledge": reference, "deadline": self.clock() + 900, "public": {
                "interaction_id": iid, "state": "COMPLETED", "revision": 0, "reason_code": None,
                "position_ref": None, "confirmation": None, "send_preview": None, "failure": None,
                "target": target, "knowledge_ref": reference,
                "result": {"applied_concept_ids": reference["concept_ids"], "section": "学習内容", "text": content}}}
            self.interactions[iid] = item
        item["channel"] = i["channel"]
        if i['channel'] == 'STANDALONE_BYOK':
            self.byok.bind(item, binding)
        if "question" in i:
            item["question"] = i["question"]
        if i["channel"] == "STANDALONE_BYOK" and item["public"]["state"] == "COMPLETED":
            self.byok.prepare(item)
        return deepcopy(item["public"])
