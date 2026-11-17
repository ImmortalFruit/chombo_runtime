"""OpenAPI for the lifecycle-only HTTP adapter (no business routes yet)."""
from engine.application_schema import BOOLEAN, STRING, array, enum, nullable, obj


def openapi_document():
    health = obj({"state": enum("STARTING", "READY", "STOPPING", "STOPPED", "FAILED"),
                  "ready": BOOLEAN, "instance_id": STRING, "app_version": STRING,
                  "contract_versions": array(STRING), "recovery_required": BOOLEAN,
                  "reason_code": nullable(STRING)})
    def response(description, schema):
        return {"description": description, "content": {"application/json": {"schema": schema}}}
    error = obj({"code": STRING})
    document = {"openapi": "3.1.0", "info": {"title": "Chombo standalone runtime", "version": "1.0.0",
            "description": "Loopback Runtime: local templates, Navigator operations and explicitly authorized browser-session BYOK."},
            "servers": [{"url": "http://127.0.0.1:8765", "description": "port 0指定時は起動stdoutの実portを使用"}],
            "paths": {
                "/api/v1/health/ready": {"get": {"operationId": "runtimeReady", "security": [],
                    "responses": {"200": response("READY", health), "503": response("非READY", health),
                                  "403": response("期待Host不一致", error)}}},
                "/api/v1/control/stop": {"post": {"operationId": "runtimeStop",
                    "description": "同一Origin・期待Hostを検証。bodyは正確に{}。202は受付のみ、CLIはOS lock解放まで待つ。",
                    "security": [{"RuntimeControl": []}],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": obj({}), "example": {}}}},
                    "responses": {"202": response("終了受付", obj({"state": enum("STOPPING"), "instance_id": STRING})),
                                  "400": response("入力不正", error), "403": response("制御資格・Host・Origin不正", error)}}},
                "/api/v1/openapi.json": {"get": {"operationId": "runtimeOpenapi", "security": [],
                    "responses": {"200": response("本仕様", {"type": "object"}), "403": response("期待Host不一致", error)}}}},
            "components": {"securitySchemes": {"RuntimeControl": {"type": "http", "scheme": "bearer",
                "description": "run/instance.jsonのinstance専用token。URL・ログ・通常UIへ出さない。"}}}}
    from copy import deepcopy
    from engine.application_schema import openapi_document as application_document, ref, SESSION_REF, REPLAY_REF
    from engine.runtime_application import RESULTS, RESULTS_V11, DURABLE, INTERACTION_V11
    application = application_document()
    schemas = application["components"]["schemas"]
    schemas.update({name + ".result": deepcopy(schema) for name, schema in RESULTS.items()})
    response_variants = schemas["Response"]["oneOf"]
    base = deepcopy(response_variants[0])
    for name in RESULTS:
        variant = deepcopy(base)
        variant["properties"]["operation"] = {"const": name}
        variant["properties"]["data"] = ref(name + ".result")
        if name in DURABLE:
            variant["properties"]["meta"]["properties"]["operation_id"] = STRING
            variant["properties"]["meta"]["required"].append("operation_id")
        response_variants.append(variant)
    # Version-specific response branches keep the closed 1.0 contract intact.
    for name, schema in RESULTS_V11.items():
        schemas["v1_1." + name + ".result"] = ({"anyOf": [deepcopy(RESULTS[name]), deepcopy(schema)]}
            if name in RESULTS else deepcopy(schema))
    for name in sorted(set(RESULTS) | set(RESULTS_V11)):
        variant = deepcopy(base)
        variant["properties"]["contract_version"] = {"const": "1.1.0"}
        variant["properties"]["operation"] = {"const": name}
        variant["properties"]["data"] = ref(("v1_1." if name in RESULTS_V11 else "") + name + ".result")
        if name in DURABLE:
            variant["properties"]["meta"]["properties"]["operation_id"] = STRING
            variant["properties"]["meta"]["required"].append("operation_id")
        response_variants.append(variant)
    document["components"]["schemas"] = schemas
    document["components"]["examples"] = application["components"]["examples"]
    examples = document["components"]["examples"]
    for state, reason in (("WAITING_CONFIRMATION", "SUBJECT_AMBIGUOUS"), ("FAILED", "LLM_UNCONFIGURED"),
                          ("DENIED", "OTHER_SEAT_DURING_ACTIVE_PLAY"), ("EXPIRED", "REFERENCE_EXPIRED")):
        data = {"interaction_id": "interaction-example", "state": state, "revision": 0, "reason_code": reason,
                "position_ref": None, "confirmation": None, "send_preview": None, "result": None,
                "failure": None, "target": {"kind": "KNOWLEDGE", "lesson_id": "understand-shanten"}, "knowledge_ref": None}
        if state == "WAITING_CONFIRMATION":
            data["target"] = {"kind": "POSITION", "conversation_ref_id": "server-issued-ref", "intent": "SHAPE_ANALYSIS"}
            data["confirmation"] = {"confirmation_id": "confirmation-example", "reason_code": reason,
                "choices": [{"choice_id": "SELF", "label": "自家"}, {"choice_id": "PUBLIC_TABLE", "label": "公開表"}]}
        if state == "FAILED":
            data["failure"] = {"failure_id": "failure-example", "code": reason, "retryable": False, "delivery_status": "NOT_SENT"}
        examples["Dialogue11" + state] = {"summary": "1.1.0 " + state, "value": {
            "contract_id": "chombo.application", "contract_version": "1.1.0", "request_id": "example",
            "operation": "interaction.read", "status": "SUCCEEDED", "data": data, "error": None,
            "meta": {"engine_version": None, "rule_ref": None, "applied_concept_ids": [], "position_ref": None}}}
    for state in ('WAITING_SEND_PERMISSION', 'RUNNING'):
        example = deepcopy(examples['Dialogue11FAILED'])
        example['summary'] = '1.1.0 ' + state
        data = example['value']['data']
        data.update(state=state, reason_code=None, failure=None)
        if state == 'WAITING_SEND_PERMISSION':
            data['send_preview'] = {'send_preview_id': 'preview-example', 'attempt_id': 'attempt-example',
                'provider': 'openai', 'model': 'gpt-4.1-mini-2025-04-14', 'endpoint': 'https://api.openai.com/v1/responses',
                'config_revision': 1, 'payload': {'description': 'See actual preview for the entire request body'},
                'payload_digest': '0' * 64, 'expires_in_seconds': 899, 'position_ref': None}
        examples['Dialogue11' + state] = example
    document["paths"].update({
        "/api/v1/invoke": {"post": {"operationId": "invoke", "security": [],
            "description": "One OS-user owner, resolved by the loopback host. Exact Host and same Origin required; JSON cannot supply an owner or grant. Body limit 90 MiB. No API key.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": ref("Request")}}},
            "responses": {str(code): response("Application envelope; read inner business status", ref("Response")) for code in (200, 202, 400, 403, 404, 409, 413, 422, 500, 503)}}},
        "/api/v1/capabilities": {"get": {"operationId": "capabilities", "security": [],
            "responses": {"200": response("Composition-specific operations and availability", {"type": "object"})}}},
        "/api/v1/context": {"get": {"operationId": "runtimeContext", "security": [],
            "responses": {"200": response("Server-issued current selectors; no authority from caller JSON", obj({"runtime_instance_id": STRING,
                "replay_request_scope": STRING, "session_ref": nullable(SESSION_REF), "replay_ref": nullable(REPLAY_REF)}))}}},
        "/api/v1/artifacts/{artifact_id}": {"get": {"operationId": "readArtifact", "security": [],
            "parameters": [{"name": "artifact_id", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}],
            "responses": {"200": {"description": "Owner-checked bytes, digest checked before disclosure", "content": {
                media: {"schema": {"type": "string", "format": "binary"}} for media in ("application/json", "image/png", "text/plain")}},
                "404": response("Unavailable to this owner", error)}}},
    })
    document["components"]["securitySchemes"]["BrowserSession"] = {
        "type": "apiKey", "in": "cookie", "name": "chombo_byok_<runtime_instance_id_without_hyphens>",
        "description": "HttpOnly SameSite=Strict session cookie. Name suffix is the runtime instance ID without hyphens; no API key in cookie."}
    status_schema = obj({"configured": BOOLEAN, "available": BOOLEAN,
                         "reason_code": nullable(STRING), "config_revision": {"type": "integer"}})
    for suffix, method, action, inputs, output in (
        ("/session", "post", "open", obj({}), status_schema),
        ("", "get", "status", None, status_schema),
        ("", "post", "set", obj({"secret": STRING, "expected_config_revision": {"type": "integer"}}), status_schema),
        ("/delete", "post", "delete", obj({"expected_config_revision": {"type": "integer"}}), status_schema),
        ("/activity", "post", "activity", obj({}), status_schema),
        ("/end", "post", "end", obj({}), obj({"ended": BOOLEAN})),
        ("/test-preview", "post", "preview", obj({"expected_config_revision": {"type": "integer"}}), INTERACTION_V11),
        ('/invoke', 'post', 'dialogue_invoke', ref('Request'), ref('Response')),
    ):
        route = {"operationId": "credential_" + action,
                 "description": "Exact Host required. Mutations require same Origin; GET also accepts Sec-Fetch-Site:same-origin when Origin is absent. Body limit 8 KiB, or 64 KiB for invoke. no-store. API key in Runtime memory only. One-hour idle session cookie. Status does not renew. test-preview creates a fixed connection-check interaction with zero sends. invoke accepts only 1.1.0 interaction operations and binds them to this provider's Cookie; authorize_send ALLOW consumes one preview, retry requires fresh permission. Normal /invoke has no browser credentials.",
                 "security": [] if action == "open" else [{"BrowserSession": []}],
                 "parameters": [{"name": "Origin", "in": "header", "required": method == "post", "schema": STRING}],
                 "responses": {"200": response("Session credential result", output),
                               **{str(code): response("Safe error code", error) for code in (400, 403, 404, 409, 413, 503)}}}
        if inputs is not None:
            route["requestBody"] = {"required": True, "content": {"application/json": {"schema": inputs}}}
        document["paths"].setdefault("/api/v1/credentials/openai" + suffix, {})[method] = route
    # Each provider uses a separate cookie and a provider-bound volatile record.
    from engine.session_credentials import SessionCredentials
    for provider in SessionCredentials.PROVIDERS:
        if provider == "openai":
            continue
        scheme = "BrowserSession_" + provider
        document["components"]["securitySchemes"][scheme] = {
            **document["components"]["securitySchemes"]["BrowserSession"],
            "name": "chombo_byok_<runtime_instance_id_without_hyphens>_" + provider}
        for path, routes in list(document["paths"].items()):
            if path.startswith("/api/v1/credentials/openai"):
                copied = deepcopy(routes)
                for route in copied.values():
                    route["operationId"] += "_" + provider
                    if route["security"]:
                        route["security"] = [{scheme: []}]
                document["paths"][path.replace("/openai", "/" + provider)] = copied
    return document
