"""OpenAPI for the local legacy Codex confirmation bridge, not v1 interaction."""
from copy import deepcopy

from engine.application_schema import result_schemas


CONFIRM_INPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["conversation_ref_id", "choice_id"],
    "properties": {
        "conversation_ref_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "choice_id": {"type": "string", "enum": ["SELF", "PUBLIC_TABLE"]},
    },
}


def openapi_document():
    # Legacy public_metadata predates the common PositionRef digest/TTL fields.
    position = deepcopy(result_schemas()["position.snapshot.result"]["anyOf"][0])
    for field in ("status", "position_input_digest", "visible_seat", "state"):
        position["properties"].pop(field)
        position["required"].remove(field)
    error = {"type": "object", "additionalProperties": False, "required": ["error", "details"],
             "properties": {"error": {"type": "string"}, "details": {"type": "object"}}}
    return {
        "openapi": "3.1.0", "info": {"title": "Chombo local Codex confirmation bridge", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:8765"}],
        "paths": {
            "/api/conversation/confirm": {"post": {
                "description": "Local Navigator owner context; no API key or BYOK. Resolve an owned ambiguous ref on its original frozen event. Same choice reuses the derived ref; a conflicting choice is rejected. Does not implement /api/v1 interaction state.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": CONFIRM_INPUT}}},
                "responses": {
                    "201": {"description": "Derived fixed reference (including identical retry)", "content": {"application/json": {"schema": position}}},
                    "400": {"description": "Invalid input, unknown ref, conflict, expiry or revoked grant", "content": {"application/json": {"schema": error}}},
                },
            }},
            "/api/conversation/openapi.json": {"get": {"responses": {"200": {
                "description": "This OpenAPI document", "content": {"application/json": {"schema": {"type": "object"}}},
            }}}},
        },
    }
