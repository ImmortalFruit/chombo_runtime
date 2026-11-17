"""Read-only, bounded lesson port. Markdown remains the teaching source of truth."""
from engine.knowledge_store import KnowledgeStore
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

LESSONS = ("understand-shanten", "understand-ukeire", "identify-tenpai")
CURRICULUM = "hand-completion-tenpai-and-waits"
INDEX_PATHS = (f"learning/curriculum/{CURRICULUM}.json", f"learning/lessons/{CURRICULUM}.json")
KNOWLEDGE_PATHS = (*INDEX_PATHS, *(f"docs/learning/ja-JP/lessons/{name}.md" for name in LESSONS))


class KnowledgeUnavailable(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class LessonKnowledge:
    def __init__(self, root):
        self.root = Path(root)

    def resolve(self, lesson_id):
        if lesson_id not in LESSONS:
            raise KnowledgeUnavailable("LESSON_UNAVAILABLE")
        try:
            store = KnowledgeStore(self.root)
            manifest = store.read_json("engine/dialogue-knowledge-manifest.json")
            payloads = {}
            for path in KNOWLEDGE_PATHS:
                # Validate only this lesson and the two non-recursive indexes.
                if path not in INDEX_PATHS and path != f"docs/learning/ja-JP/lessons/{lesson_id}.md":
                    continue
                raw = store.read_bytes(path)
                if hashlib.sha256(raw).hexdigest() != manifest["files"][path]["sha256"]:
                    raise KnowledgeUnavailable("KNOWLEDGE_DIGEST_MISMATCH")
                payloads[path] = raw.decode("utf-8")
            curriculum = json.loads(payloads[INDEX_PATHS[0]])
            index = json.loads(payloads[INDEX_PATHS[1]])
            if curriculum["status"] != "AVAILABLE" or curriculum["curriculum_id"] != CURRICULUM or index["curriculum_id"] != CURRICULUM:
                raise KnowledgeUnavailable("LESSON_UNAVAILABLE")
            entries = [item for item in index["lessons"] if item["lesson_id"] == lesson_id]
            if len(entries) != 1 or lesson_id not in curriculum["lesson_ids"]:
                raise KnowledgeUnavailable("LESSON_UNAVAILABLE")
            entry = entries[0]
            path = f"docs/learning/ja-JP/lessons/{lesson_id}.md"
            if entry["status"] != "AVAILABLE" or entry["document_path"] != path:
                raise KnowledgeUnavailable("LESSON_UNAVAILABLE")
            text = payloads[path]
            front, body = text.removeprefix("---\n").split("\n---\n", 1)
            if not text.startswith("---\n"):
                raise ValueError()
            metadata = {}
            for line in front.splitlines():
                key, value = line.split(":", 1)
                if key in metadata or key != key.strip():
                    raise ValueError()
                metadata[key] = value.strip()
            ids = [value.strip() for value in metadata["concept_ids"].strip("[]").split(",")]
            if (metadata["lesson_id"] != lesson_id or metadata["availability"] != "AVAILABLE"
                    or metadata["locale"] != "ja-JP" or ids != entry["concept_ids"]
                    or metadata["version"] != manifest["files"][path]["version"]):
                raise KnowledgeUnavailable("LESSON_UNAVAILABLE")
            qualified = set()
            for filename in ("hand-completion-tenpai-and-waits.json", "hand-ukeire.json"):
                document = store.read_json("concepts/" + filename)
                if document["status"] == "QUALIFIED":
                    qualified.update(item["concept_id"] for item in document["concepts"])
            if not set(ids) <= qualified:
                raise KnowledgeUnavailable("REVIEW_REQUIRED")
            sections = re.split(r"(?m)^## ", body)
            content = next(section.split("\n", 1)[1].strip() for section in sections[1:] if section.startswith("学習内容\n"))
            reference = {"lesson_id": lesson_id, "document_path": path, "version": metadata["version"],
                         "sha256": manifest["files"][path]["sha256"], "concept_ids": ids}
            return deepcopy(reference), content
        except KnowledgeUnavailable:
            raise
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            raise KnowledgeUnavailable("LESSON_UNAVAILABLE") from None
