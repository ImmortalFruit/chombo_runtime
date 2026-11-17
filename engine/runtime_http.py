"""Standalone API, static Nuxt UI, and existing Navigator data adapters."""
import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from engine.navigator import NavigatorHandler, NavigatorError
from engine.runtime_application import http_status
from engine.rule_drafts import digest


class RuntimeHandler(NavigatorHandler):
    def invoke(self, operation, inputs, request_id=None):
        return self.server.application.invoke({"contract_id": "chombo.application", "contract_version": "1.0.0",
            "request_id": request_id or str(uuid4()), "operation": operation, "input": inputs}, self.server.services.context())

    def do_GET(self):
        if self.path.startswith("/api/v1/credentials/"):
            return self.credentials_request(False)
        path = urlparse(self.path).path
        from engine.runtime_ui import RuntimeUI
        asset = RuntimeUI(self.server.services.runtime.config["assets_dir"]).read(path)
        if asset is not None:
            status, raw, media, cache = asset
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/v1/capabilities":
            return self._json(200, self.server.application.capabilities())
        if path == "/api/v1/context":
            s = self.server.services
            with s.lock:
                return self._json(200, {"runtime_instance_id": s.runtime.instance_id,
                    "replay_request_scope": s.issue_replay_request_scope(),
                    "session_ref": s.session_ref() if s.manager.active else None,
                    "replay_ref": s.replay_ref() if s.manager.replay else None})
        if path == "/api/state" and self.server.manager.active:
            with self.server.services.lock:
                return self._json(200, {**self.server.manager.active.snapshot(), "session_ref": self.server.services.session_ref(), "processing": False})
        if path.startswith("/api/v1/artifacts/"):
            try:
                raw, media_type = self.server.services.runtime.artifacts.read("local-user", path.rsplit("/", 1)[-1])
                return self._send(200, raw, media_type)
            except (OSError, ValueError):
                return self._json(404, {"code": "RESOURCE_NOT_FOUND"})
        # Only data/bridge endpoints may delegate to Navigator. A missing SPA
        # must never revive its Python-generated pages.
        if path in {"/api/conversation/openapi.json", "/api/replay", "/api/checkpoints", "/assets/chombo-icon.png"}:
            with self.server.services.lock:
                return super().do_GET()
        return self._json(404, {"code": "RESOURCE_NOT_FOUND"})

    def do_POST(self):
        if self.path in {"/api/conversation/ref", "/api/conversation/confirm", "/api/conversation/answer", "/api/conversation/snapshot"}:
            with self.server.services.lock:
                return super().do_POST()
        if self.path.startswith("/api/v1/credentials/"):
            return self.credentials_request(True)
        if self.path == "/api/v1/invoke":
            if self.headers.get("Content-Type") != "application/json":
                return self._json(415, {"code": "CONTENT_TYPE_REJECTED"})
            raw = self.read_body()
            if raw is None:
                return
            result = self.server.application.invoke_json(raw, self.server.services.context())
            return self._json(http_status(result), result)
        # Translate old forms into the same closed, versioned application calls.
        raw = self.read_body()
        if raw is None:
            return
        try:
            if self.path == "/api/replay/open":
                op, inputs = "replay.open", {"filename": unquote(self.headers.get("X-Chombo-Filename", "record.json")), "record_base64": base64.b64encode(raw).decode()}
                body = {}
            else:
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    from engine.rule_drafts import strict_json
                    body = strict_json(raw)
                else:
                    body = {k: values[-1] for k, values in parse_qs(raw.decode(), keep_blank_values=True).items()}
                if type(body) is not dict:
                    raise ValueError()
                op, inputs = self.translate(body)
            result = self.invoke(op, inputs, body.get("request_id"))
            if result["status"] != "SUCCEEDED":
                return self._json(http_status(result), {"error": result["error"]["code"], "details": {}})
            data = result["data"]
            if op in {"session.start", "checkpoint.restore", "session.rematch"}:
                data = {"session_id": data["session_ref"]["session_id"], "lineage_id": data["session_ref"]["lineage_id"]}
                if self.path == "/api/start" and not self.headers.get("Content-Type", "").startswith("application/json"):
                    self.send_response(303); self.send_header("Location", "/live"); self.end_headers(); return
            elif op == "rules.draft_create":
                did = data["result"]["draft"]["draft_id"]
                data = {"draft_id": did, "location": "/rules/edit/" + did}
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    self.send_response(303); self.send_header("Location", data["location"]); self.end_headers(); return
            elif "result" in data:
                data = data["result"]
            elif op in {"replay.open", "replay.seek"}:
                data = data["metadata"]
            elif op == "position.issue":
                data = data["position_ref"]
            return self._json(200, data)
        except Exception:
            return self._json(400, {"error": "INVALID_REQUEST", "details": {}})

    def credentials_request(self, mutation):
        from engine.session_credentials import CredentialError, SessionCredentials
        from engine.rule_drafts import strict_json
        # Status requires same-origin browser metadata; mutations require Origin.
        origin = "http://" + self.headers.get("Host", "")
        if (self.headers.get_all("Origin") != [origin] and
                not (not mutation and self.headers.get("Origin") is None and
                     self.headers.get_all("Sec-Fetch-Site") == ["same-origin"])):
            return self.server_credential_json(403, {"code": "ORIGIN_REJECTED"})
        parts = self.path.split("/")
        provider = parts[4] if len(parts) > 4 else ""
        if provider not in SessionCredentials.PROVIDERS:
            return self.server_credential_json(404, {"code": "RESOURCE_NOT_FOUND"})
        prefix = "/api/v1/credentials/" + provider
        actions = {prefix: "set" if mutation else "status",
                   prefix + "/session": "open", prefix + "/end": "end",
                   prefix + "/delete": "delete", prefix + "/test-preview": "test-preview"}
        actions[prefix + "/activity"] = "activity"
        actions[prefix + '/invoke'] = 'invoke'
        action = actions.get(self.path)
        if action is None or (not mutation and action != "status"):
            return self.server_credential_json(404, {"code": "RESOURCE_NOT_FOUND"})
        from http.cookies import SimpleCookie, CookieError
        cookie_name = "chombo_byok_" + self.server.services.runtime.instance_id.replace("-", "") + ("_" + provider if provider != "openai" else "")
        token = None
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            if cookie_name in cookie:
                token = cookie[cookie_name].value
        except CookieError:
            pass
        cookie_header = None
        try:
            body = {}
            if mutation:
                lengths = self.headers.get_all("Content-Length")
                if (self.headers.get("Content-Type") != "application/json" or
                        self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding") or
                        not lengths or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])):
                    raise CredentialError("INVALID_REQUEST")
                size = int(lengths[0])
                if size > (65536 if action == 'invoke' else 8192):
                    raise CredentialError("RESOURCE_LIMIT")
                self.connection.settimeout(10)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise CredentialError("INVALID_REQUEST")
                body = strict_json(raw)
            store = self.server.services.runtime.credentials
            if action == 'test-preview':
                if type(body) is not dict or set(body) != {'expected_config_revision'} or type(body['expected_config_revision']) is not int:
                    raise CredentialError('INVALID_REQUEST')
                result = self.server.services.byok.connection_preview((provider, token), body['expected_config_revision'])
                return self.server_credential_json(200, result)
            if action == 'invoke':
                store.execute(token, 'status', {}, provider)
                if (type(body) is not dict or body.get('contract_version') != '1.1.0' or
                        not str(body.get('operation', '')).startswith('interaction.')):
                    raise CredentialError('INVALID_REQUEST')
                result = self.server.application.invoke(body, self.server.services.context((provider, token)))
                return self.server_credential_json(http_status(result), result)
            if action == "open":
                if body != {}:
                    raise CredentialError("INVALID_REQUEST")
                try:
                    result = store.execute(token, "status", {}, provider)
                except CredentialError:
                    token = store.open(provider)["session_token"]
                    result = store.execute(token, "status", {}, provider)
                    # Session cookie: no Expires/Max-Age, Domain or JS access.
                    cookie_header = f"{cookie_name}={token}; Path=/api/v1/credentials; HttpOnly; SameSite=Strict"
            else:
                result = store.execute(token, action, body, provider)
                if action in {"delete", "end"}:
                    cookie_header = f"{cookie_name}=; Path=/api/v1/credentials; HttpOnly; SameSite=Strict; Max-Age=0"
            return self.server_credential_json(200, result, cookie_header)
        except (CredentialError, NavigatorError) as exc:
            status = {"BROWSER_SESSION_EXPIRED": 403, "CONFIG_REVISION_CONFLICT": 409,
                      "RESOURCE_LIMIT": 413, "CAPABILITY_UNAVAILABLE": 503, 'LLM_UNCONFIGURED': 503,
                      'PROVIDER_CONFIGURATION_CHANGED': 409}.get(exc.code, 400)
            return self.server_credential_json(status, {"code": exc.code})
        except Exception:
            return self.server_credential_json(400, {"code": "INVALID_REQUEST"})

    def server_credential_json(self, status, result, cookie_header=None):
        raw = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie_header is not None:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        values = self.headers.get_all("Content-Length")
        if (not values or len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]) or
                self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding")):
            self._json(400, {"code": "INVALID_REQUEST"}); return None
        if len(values[0]) > 12:
            self._json(413, {"code": "RESOURCE_LIMIT"}); return None
        size = int(values[0])
        if size > 90 * 1024 * 1024:
            self._json(413, {"code": "RESOURCE_LIMIT"}); return None
        self.connection.settimeout(10)
        raw = self.rfile.read(size)
        if len(raw) != size:
            self._json(400, {"code": "INVALID_REQUEST"}); return None
        return raw

    def translate(self, b):
        s = self.server.services
        path = self.path
        if path == "/api/start":
            scores = {seat: b.get("score_" + seat, "") for seat in ("east", "south", "west", "north")}
            if any(scores.values()) and not all(scores.values()):
                raise ValueError()
            p = {"round_wind": b.get("round_wind", "east"), "hand_number": int(b.get("hand_number", 1)),
                "honba": int(b.get("honba", 0)), "riichi_deposits": int(b.get("riichi_deposit_count", 0)) * 1000,
                "human_seat": b.get("human_seat") or None}
            if all(scores.values()):
                p["seat_scores"] = {k: int(v) for k, v in scores.items()}
            selector = b["rule_alias"]
            rules = {"kind": "CUSTOM_CONFIRMED", "reference": json.loads(selector)} if selector.startswith("{") else {"kind": "OFFICIAL", "alias": selector}
            i = {"format": b.get("format", "single_hand"), "rules": rules, "position": p}
            if b.get("seed"):
                i["seed"] = str(b["seed"])
            return "session.start", i
        if path == "/api/action":
            return "session.action", {"session_ref": b["session_ref"], "action_id": b["action_id"]}
        if path in {"/api/leave-live", "/api/live/record", "/api/live/rematch"}:
            return {"/api/leave-live": "session.leave", "/api/live/record": "session.save_record", "/api/live/rematch": "session.rematch"}[path], {"session_ref": b["session_ref"]}
        if path == "/api/checkpoints":
            return "checkpoint.save", {"session_ref": s.session_ref(), "display_name": b.get("display_name", "復帰点"),
                "expected_generation": int(b.get("expected_generation", s.manager.store.list(s.owner)["generation"]))}
        if path == "/api/restore":
            return "checkpoint.restore", {"checkpoint_id": b["checkpoint_id"], "expected_generation": int(b["expected_generation"]), "expected_active_session_id": b.get("expected_active_session_id") or None}
        if path.startswith("/api/recovery/"):
            return "recovery." + path.rsplit("/", 1)[-1], {k: int(v) if k == "expected_generation" else v for k, v in b.items() if k != "request_id"}
        if path == "/api/replay/position":
            ref = s.replay_ref()
            return "replay.seek", {"replay_ref": ref, "hand_index": int(b.get("hand_index", ref["hand_index"])), "event_index": int(b.get("event_index", ref["event_index"]))}
        if path == "/api/replay/analysis-grant":
            i = {"replay_ref": s.replay_ref(), "scope": b["scope"]}
            if b.get("seat"):
                i["seat"] = b["seat"]
            return "replay.grant", i
        if path == "/api/conversation/ref":
            i = {k: v for k, v in b.items() if v is not None and v != ""}
            i["session_ref" if i["source_kind"] != "COMPLETED_REPLAY" else "replay_ref"] = s.session_ref() if i["source_kind"] != "COMPLETED_REPLAY" else s.replay_ref()
            return "position.issue", i
        if path in {"/api/conversation/answer", "/api/conversation/snapshot"}:
            return "position.analyze" if path.endswith("answer") else "position.snapshot", {"conversation_ref_id": b["conversation_ref_id"]}
        if path == "/api/rules/drafts":
            ref = next(x["ref"] for x, _ in s.rules.validator.catalogs()[4] if x["ref"]["profile_id"] == b["base_profile_id"])
            return "rules.draft_create", {"base_ref": ref, "display_name": b["display_name"]}
        match = re.fullmatch(r"/api/rules/drafts/([0-9a-f-]{36})/(save|confirm)", path)
        if match:
            draft, _ = s.rules.load(match[1])
            i = {"draft_id": match[1], "expected_draft_digest": digest(draft)}
            if match[2] == "save":
                i.update(display_name=b["display_name"], overrides=b["overrides"], request_summary=b.get("request_summary", "Navigator rule editor update"))
            return "rules.draft_" + match[2], i
        discard = re.fullmatch(r"/api/rules/custom/(custom\.[0-9a-f-]{36})/discard", path)
        if discard:
            from engine.rule_editor import rule_management_model
            item = next(x for x in rule_management_model(s.rules)["confirmed"] if x["custom_ruleset_id"] == discard[1])
            return "rules.custom_discard", {"profile_id": item["ref"]["profile_id"], "expected_snapshot_id": item["ref"]["snapshot_id"]}
        raise NavigatorError("CAPABILITY_UNAVAILABLE")
