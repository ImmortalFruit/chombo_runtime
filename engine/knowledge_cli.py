"""Local, qualified knowledge reference. Use the selected workspace Python."""
import argparse
import hashlib
import json
from pathlib import Path

from engine.knowledge_store import KnowledgeStore, KnowledgeStoreError
from engine.dialogue_knowledge import LessonKnowledge, KnowledgeUnavailable


def reference(root, *, concept_id=None, document=None):
    store = KnowledgeStore(root)
    documents = store.concept_documents()
    matches = []
    for path, doc in documents:
        if document and document != path:
            continue
        for concept in (*doc.get("concepts", []), *doc.get("provisional_concepts", [])):
            if concept_id is not None and concept.get("concept_id") != concept_id:
                continue
            status = concept.get("status", doc.get("status"))
            matches.append({"concept_id": concept["concept_id"], "status": status,
                            "document_path": path, "document_status": doc.get("status"),
                            "sha256": hashlib.sha256(store.read_bytes(path)).hexdigest(),
                            "document_metadata": {k: v for k, v in doc.items()
                                                  if k not in ("concepts", "provisional_concepts")},
                            "concept": concept})
    if concept_id is None:
        return [{k: v for k, v in m.items() if k not in ("concept", "document_metadata")} for m in matches]
    if len(matches) != 1:
        raise KnowledgeStoreError("CONCEPT_UNAVAILABLE")
    if matches[0]["status"] != "QUALIFIED" or matches[0]["document_metadata"].get("status") != "QUALIFIED":
        raise KnowledgeStoreError("REVIEW_REQUIRED")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--concept")
    group.add_argument("--lesson")
    parser.add_argument("--document", help="Optional logical concepts/*.json path for --list/--concept")
    args = parser.parse_args()
    try:
        if args.lesson:
            ref, content = LessonKnowledge(args.root).resolve(args.lesson)
            result = {"reference": ref, "section": "学習内容", "text": content}
        else:
            result = reference(args.root, concept_id=args.concept, document=args.document)
        print(json.dumps({"status": "AVAILABLE", "data": result}, ensure_ascii=False, sort_keys=True))
    except (KnowledgeStoreError, KnowledgeUnavailable, OSError, ValueError, KeyError, TypeError, AttributeError):
        # Never emit source content or parser exceptions on a failed boundary.
        print(json.dumps({"status": "UNAVAILABLE", "reason_code": "KNOWLEDGE_UNAVAILABLE"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
