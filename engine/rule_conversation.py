"""Deterministic boundary between conversational intent and rule Draft storage.

The conversational model supplies a small structured interpretation.  This
module resolves only registered, fixed profile references and explicit complete
dimension values; it never guesses missing rule knowledge from prose.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from engine.rule_drafts import (
    DraftStore,
    RuleProfileError,
    confirmed_ref,
    new_draft,
    strict_json,
    validate_ref,
)


def _question(code: str, *, path: str, candidates=(), dimension_ids=()) -> dict:
    return {
        "code": code,
        "path": path,
        "candidate_refs": [copy.deepcopy(item) for item in candidates],
        "dimension_ids": list(dimension_ids),
        "message_key": "rule_conversation." + code.lower(),
    }


class RuleDraftConversation:
    """Create and revise the same managed Draft used by the future editor UI."""

    def __init__(self, store: DraftStore):
        if not isinstance(store, DraftStore):
            raise TypeError("store must be DraftStore")
        self.store = store

    def _official_candidates(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            return []
        needle = query.strip().casefold()
        approval = self.store.validator.catalogs()[0]
        registry = self.store.validator.root / "engine/profiles/registry.v1.json"
        entries = strict_json(registry.read_bytes()).get("profiles", [])
        approved = {entry["ref"]["profile_id"]: entry["ref"] for entry in approval["profiles"]}
        exact, partial = [], []
        for item in entries:
            ref = approved.get(item.get("profile_id"))
            if ref is None:
                continue
            labels = [item.get("profile_id"), item.get("ruleset_id"), *item.get("aliases", [])]
            folded = [label.casefold() for label in labels if isinstance(label, str)]
            if needle in folded:
                exact.append(ref)
            elif any(needle in label for label in folded):
                partial.append(ref)
        # catalogs() above also verifies that registry-selected refs cannot smuggle
        # an unapproved profile into this conversational boundary.
        return copy.deepcopy(exact or partial)

    def _resolve_base(self, request: Mapping[str, Any]) -> tuple[dict | None, list[dict]]:
        has_query = "base_query" in request
        has_ref = "base_ref" in request
        if has_query == has_ref:
            return None, [_question("BASE_REQUIRED", path="/base_query")]
        if has_ref:
            ref = request["base_ref"]
            try:
                validate_ref(ref)
            except RuleProfileError:
                return None, [_question("BASE_INVALID", path="/base_ref")]
            if ref.get("kind") == "CUSTOM_CONFIRMED":
                document = self.store.lookup(ref)
                if document is None:
                    return None, [_question("BASE_NOT_FOUND", path="/base_ref")]
                if confirmed_ref(document) != ref:
                    return None, [_question("BASE_INVALID", path="/base_ref")]
            if ref.get("kind") == "OFFICIAL":
                approved = [entry["ref"] for entry in self.store.validator.catalogs()[0]["profiles"]]
                if ref not in approved:
                    return None, [_question("BASE_NOT_FOUND", path="/base_ref")]
            return copy.deepcopy(ref), []
        candidates = self._official_candidates(request["base_query"])
        if len(candidates) != 1:
            code = "BASE_NOT_FOUND" if not candidates else "BASE_AMBIGUOUS"
            return None, [_question(code, path="/base_query", candidates=candidates)]
        return candidates[0], []

    @staticmethod
    def _changes(request: Mapping[str, Any], required_dimensions: set[str]) -> tuple[list[dict], list[dict]]:
        changes = request.get("changes")
        if not isinstance(changes, list):
            return [], [_question("CHANGES_REQUIRED", path="/changes")]
        questions, normalized, seen = [], [], set()
        for index, change in enumerate(changes):
            path = f"/changes/{index}"
            if not isinstance(change, Mapping):
                questions.append(_question("CHANGE_INCOMPLETE", path=path))
                continue
            dimension = change.get("dimension_id")
            operation = change.get("operation", "SET")
            if not isinstance(dimension, str) or dimension not in required_dimensions:
                questions.append(_question("DIMENSION_UNKNOWN", path=path + "/dimension_id"))
                continue
            if dimension in seen:
                questions.append(
                    _question("DIMENSION_AMBIGUOUS", path=path, dimension_ids=[dimension])
                )
                continue
            seen.add(dimension)
            if operation not in ("SET", "UNSET") or (operation == "SET" and "value" not in change) or (operation == "UNSET" and "value" in change):
                questions.append(
                    _question("CHANGE_INCOMPLETE", path=path, dimension_ids=[dimension])
                )
                continue
            normalized.append({"dimension_id": dimension, "operation": operation,
                               **({"value": copy.deepcopy(change["value"])} if operation == "SET" else {})})
        return normalized, questions

    def apply(self, request: Mapping[str, Any], *, actor_id: str) -> dict:
        """Return NEEDS_CLARIFICATION without writes, or save and revalidate a Draft."""
        if not isinstance(request, Mapping) or not isinstance(actor_id, str) or not actor_id.strip():
            raise RuleProfileError("MALFORMED_DRAFT", {"reason": "CONVERSATION_REQUEST"})
        allowed = {"base_query", "base_ref", "display_name", "request_summary", "changes",
                   "draft_id", "expected_revision"}
        if set(request) - allowed:
            raise RuleProfileError("MALFORMED_DRAFT", {"reason": "UNKNOWN_CONVERSATION_FIELD"})
        is_edit = "draft_id" in request or "expected_revision" in request
        questions: list[dict] = []
        if is_edit:
            if not isinstance(request.get("draft_id"), str) or type(request.get("expected_revision")) is not int:
                questions.append(_question("DRAFT_REFERENCE_REQUIRED", path="/draft_id"))
                draft = None
            else:
                try:
                    current, _ = self.store.load(request["draft_id"])
                except RuleProfileError:
                    current = None
                    questions.append(_question("DRAFT_NOT_FOUND", path="/draft_id"))
                draft = copy.deepcopy(current) if current else None
                if draft and draft["revision"] != request["expected_revision"]:
                    questions.append(_question("DRAFT_REVISION_STALE", path="/expected_revision"))
        else:
            draft = None

        if draft is None and not is_edit:
            base, base_questions = self._resolve_base(request)
            questions.extend(base_questions)
            name = request.get("display_name")
            if not isinstance(name, str) or not name.strip():
                questions.append(_question("DISPLAY_NAME_REQUIRED", path="/display_name"))
            if base is not None and isinstance(name, str) and name.strip():
                draft = new_draft(base, name.strip())
        elif draft is not None and ("base_query" in request or "base_ref" in request):
            base, base_questions = self._resolve_base(request)
            questions.extend(base_questions)
            if base is not None:
                draft["base_ref"] = base

        required = set(self.store.validator.catalogs()[2])
        changes, change_questions = self._changes(request, required)
        questions.extend(change_questions)
        summary = request.get("request_summary")
        if not isinstance(summary, str) or not summary.strip():
            questions.append(_question("REQUEST_SUMMARY_REQUIRED", path="/request_summary"))
        if questions:
            return {"status": "NEEDS_CLARIFICATION", "questions": questions, "draft": None,
                    "validation": None}

        now = datetime.now(timezone.utc).isoformat()
        if is_edit:
            draft["revision"] += 1
            draft["updated_at"] = now
            if "display_name" in request:
                name = request["display_name"]
                if not isinstance(name, str) or not name.strip():
                    return {"status": "NEEDS_CLARIFICATION",
                            "questions": [_question("DISPLAY_NAME_REQUIRED", path="/display_name")],
                            "draft": None, "validation": None}
                draft["display_name"] = name.strip()
        provenance_id = f"conversation-r{draft['revision']}"
        draft["provenance"][provenance_id] = {
            "kind": "USER_REQUEST", "actor_id": actor_id.strip(), "recorded_at": now,
            "request_summary": summary.strip(),
        }
        for change in changes:
            dimension = change["dimension_id"]
            if change["operation"] == "UNSET":
                draft["overrides"].pop(dimension, None)
            else:
                draft["overrides"][dimension] = {
                    "value": change["value"], "provenance_id": provenance_id,
                }
        saved, validation = self.store.save(draft, expected_revision=request["expected_revision"] if is_edit else 0)
        return {"status": "DRAFT_SAVED", "questions": [], "draft": saved,
                "validation": validation}
