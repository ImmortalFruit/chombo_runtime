"""Bounded, deterministic knowledge envelope; reversible obfuscation, not encryption.

No disk extraction, persistent cache, network dependency, or source fallback in bundles.
"""
from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType
import hashlib
import json
from pathlib import Path, PurePosixPath
import zlib

PACK_PATH = "knowledge/chombo.ckp"
MAGIC = b"CHOMBO-KNOWLEDGE\x01\n"
MAX_PACK = 16 * 1024 * 1024
MAX_RAW = 32 * 1024 * 1024
MAX_ENTRY = 4 * 1024 * 1024
MAX_ENTRIES = 1024
KEY = hashlib.sha256(b"Chombo reversible knowledge packaging v1").digest()


class KnowledgeStoreError(ValueError):
    def __init__(self, code="KNOWLEDGE_PACK_INVALID"):
        self.code = code
        super().__init__(code)


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value or ":" in value or "\x00" in value
            or PurePosixPath(value).is_absolute()
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise KnowledgeStoreError()
    if not (value.startswith("concepts/") or value.startswith("learning/")
            or value.startswith("docs/learning/") or value == "engine/dialogue-knowledge-manifest.json"):
        raise KnowledgeStoreError()
    return value


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise KnowledgeStoreError()
        result[k] = v
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise KnowledgeStoreError() from None


def _mask(raw):
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(raw))


def encode_pack(payloads):
    if not 0 < len(payloads) <= MAX_ENTRIES:
        raise KnowledgeStoreError()
    entries = []
    for path, raw in sorted(payloads.items()):
        _path(path)
        if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY:
            raise KnowledgeStoreError()
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise KnowledgeStoreError() from None
        entries.append({"path": path, "size": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(), "text": text})
    raw = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_RAW:
        raise KnowledgeStoreError()
    packed = MAGIC + hashlib.sha256(raw).digest() + _mask(zlib.compress(raw, 9))
    if len(packed) > MAX_PACK:
        raise KnowledgeStoreError()
    return packed


def decode_pack(packed):
    try:
        if len(packed) > MAX_PACK or not packed.startswith(MAGIC):
            raise KnowledgeStoreError()
        offset = len(MAGIC)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(_mask(packed[offset + 32:]), MAX_RAW + 1)
        if (len(raw) > MAX_RAW or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail or hashlib.sha256(raw).digest() != packed[offset:offset + 32]):
            raise KnowledgeStoreError()
        doc = _json(raw)
        if (type(doc) is not dict or set(doc) != {"version", "entries"}
                or type(doc["version"]) is not int or doc["version"] != 1
                or type(doc["entries"]) is not list or not 0 < len(doc["entries"]) <= MAX_ENTRIES):
            raise KnowledgeStoreError()
        result = {}
        for entry in doc["entries"]:
            if type(entry) is not dict or set(entry) != {"path", "size", "sha256", "text"}:
                raise KnowledgeStoreError()
            path = _path(entry["path"])
            raw = entry["text"].encode("utf-8")
            if (path in result or type(entry["size"]) is not int or len(raw) > MAX_ENTRY
                    or len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]):
                raise KnowledgeStoreError()
            result[path] = raw
        return result
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, zlib.error, RecursionError):
        raise KnowledgeStoreError() from None


@lru_cache(maxsize=1)
def _validated_pack(packed):
    # One immutable memory-only entry; every read reopens the file, so replacing
    # or revoking the envelope cannot reuse stale decoded qualification evidence.
    return MappingProxyType(decode_pack(packed))


class KnowledgeStore:
    def __init__(self, root):
        self.root = Path(root)
        path = self.root / PACK_PATH
        # A missing or broken bundle must never start reading a developer manuscript.
        bundled = path.exists() or path.is_symlink() or (self.root / "release-manifest.json").exists()
        self.entries = None
        if bundled:
            try:
                if path.is_symlink() or path.stat().st_size > MAX_PACK:
                    raise KnowledgeStoreError()
                with path.open("rb") as stream:
                    packed = stream.read(MAX_PACK + 1)
                manifest_path = self.root / "release-manifest.json"
                if manifest_path.exists():
                    manifest = _json(manifest_path.read_bytes())
                    if manifest.get("knowledge_pack") != {"path": PACK_PATH, "format_version": 1}:
                        raise KnowledgeStoreError()
                    items = [item for item in manifest["files"] if item["path"] == PACK_PATH]
                    if (len(items) != 1 or items[0]["size"] != len(packed)
                            or items[0]["sha256"] != hashlib.sha256(packed).hexdigest()):
                        raise KnowledgeStoreError()
                self.entries = _validated_pack(packed)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise KnowledgeStoreError() from None

    def paths(self, prefix=""):
        if self.entries is not None:
            return tuple(sorted(p for p in self.entries if p.startswith(prefix)))
        if prefix == "concepts/":
            return tuple("concepts/" + p.name for p in sorted((self.root / "concepts").glob("*.json")))
        raise KnowledgeStoreError("KNOWLEDGE_LIST_UNAVAILABLE")

    def read_bytes(self, path):
        path = _path(path)
        if self.entries is not None:
            try:
                return self.entries[path]
            except KeyError:
                raise KnowledgeStoreError("KNOWLEDGE_ENTRY_MISSING") from None
        return (self.root / path).read_bytes()

    def read_json(self, path):
        return _json(self.read_bytes(path))

    def concept_paths(self):
        return tuple(p for p in self.paths("concepts/")
                     if "/" not in p[len("concepts/"):] and p.endswith(".json"))

    def concept_documents(self):
        return tuple((p, self.read_json(p)) for p in self.concept_paths())


def concept_document(concepts_dir, filename):
    store = KnowledgeStore(Path(concepts_dir).parent)
    if store.entries is None:
        _path("concepts/" + filename)
        return _json((Path(concepts_dir) / filename).read_bytes())
    return store.read_json("concepts/" + filename)


def concept_documents(concepts_dir):
    store = KnowledgeStore(Path(concepts_dir).parent)
    if store.entries is None:
        return tuple(("concepts/" + p.name, _json(p.read_bytes()))
                     for p in sorted(Path(concepts_dir).glob("*.json")))
    return store.concept_documents()
