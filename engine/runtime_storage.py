"""Runtime-owned durable layout and allowlisted operational logging."""
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import subprocess

from engine.runtime_config import read_json


class StorageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def atomic_json(path, document):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def private_directory(path):
    path = Path(path)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise StorageError('STORAGE_UNAVAILABLE')
        return
    if not path.parent.exists():
        private_directory(path.parent)
    path.mkdir(mode=0o700)
    if os.name == 'nt':
        # Restrict inheritance on newly created managed directories only.
        import csv
        try:
            output = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], check=True, capture_output=True, text=True).stdout
            sid = next(csv.reader(output.splitlines()))[1]
            if re.fullmatch(r'S-1-[0-9-]+', sid) is None:
                raise ValueError()
            subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F'], check=True, capture_output=True)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            raise StorageError('STORAGE_UNAVAILABLE')


def managed_directory(path, root):
    path = Path(path)
    if path.resolve() != root and root not in path.resolve().parents:
        raise StorageError('STORAGE_UNAVAILABLE')
    if path.is_symlink():
        raise StorageError('STORAGE_UNAVAILABLE')
    private_directory(path)
    return path


def build_metadata(root):
    root = Path(root).resolve()
    manifest = root / 'release-manifest.json'
    if manifest.exists():
        try:
            doc = read_json(manifest)
            if type(doc['schema_version']) is not int or doc['schema_version'] != 1 or not isinstance(doc['version'], str) or not doc['version']:
                raise ValueError()
            revision = doc['source_revision']
            for item in doc['files']:
                relative = Path(item['path'])
                path = (root / relative).resolve()
                if relative.is_absolute() or root not in path.parents or not path.is_file():
                    raise ValueError()
                payload = path.read_bytes()
                if len(payload) != item['size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
                    raise ValueError()
            listed = {item['path'] for item in doc['files']}
            if not {'VERSION', 'engine/runtime.py'} <= listed or not any(p.startswith('engine/profiles/') for p in listed) or not any(p.startswith('engine/schemas/') for p in listed):
                raise ValueError()
            from engine.knowledge_store import KnowledgeStore, PACK_PATH
            if doc.get('knowledge_pack') != {'path': PACK_PATH, 'format_version': 1} or PACK_PATH not in listed:
                raise ValueError()
            KnowledgeStore(root)
            version = doc['version']
            if (root / 'VERSION').read_text(encoding='utf-8').strip() != version:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise StorageError('BUILD_METADATA_UNAVAILABLE')
    else:
        try:
            # Do not accept the revision of an unrelated parent checkout.
            top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root, check=True, capture_output=True, text=True, timeout=5).stdout.strip()
            if Path(top).resolve() != root:
                raise ValueError()
            revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True, capture_output=True, text=True, timeout=5).stdout.strip()
            version = '0.5.2'
        except (OSError, ValueError, subprocess.SubprocessError):
            raise StorageError('BUILD_METADATA_UNAVAILABLE')
    if not isinstance(revision, str) or re.fullmatch('[0-9a-f]{40}', revision) is None:
        raise StorageError('BUILD_METADATA_UNAVAILABLE')
    for directory in ('engine/profiles', 'engine/schemas'):
        paths = list((root / directory).glob('*.json'))
        if not paths:
            raise StorageError('BUILD_METADATA_UNAVAILABLE')
        try:
            for path in paths:
                if type(read_json(path)) is not dict:
                    raise ValueError()
        except (OSError, ValueError):
            raise StorageError('BUILD_METADATA_UNAVAILABLE')
    return {'source_revision': revision, 'version': version}


class RuntimeStorage:
    def __init__(self, root, metadata, *, assets_root=None, checkpoint_root=None):
        self.root = Path(root).resolve()
        path = self.root / 'runtime.json'
        if path.is_symlink():
            raise StorageError('STORAGE_UNAVAILABLE')
        if path.exists():
            self.metadata = read_json(path)
            if (type(self.metadata) is not dict or set(self.metadata) != {'data_format_version', 'store_id', 'created_build'} or
                    type(self.metadata['data_format_version']) is not int or self.metadata['data_format_version'] != 1 or
                    not isinstance(self.metadata['store_id'], str) or re.fullmatch('[0-9a-f]{32}', self.metadata['store_id']) is None or
                    type(self.metadata['created_build']) is not dict or
                    set(self.metadata['created_build']) != {'source_revision', 'version'} or
                    not isinstance(self.metadata['created_build']['source_revision'], str) or
                    re.fullmatch('[0-9a-f]{40}', self.metadata['created_build']['source_revision']) is None or
                    not isinstance(self.metadata['created_build']['version'], str) or not self.metadata['created_build']['version']):
                raise StorageError('STORE_INVALID')
        else:
            if (self.root / 'store').exists() or (self.root / 'requests').exists():
                raise StorageError('STORE_INVALID')
            self.metadata = {'data_format_version': 1, 'store_id': secrets.token_hex(16), 'created_build': metadata}
            atomic_json(path, self.metadata)
        store_root = self.root / 'store' if checkpoint_root is None else Path(checkpoint_root).absolute()
        if any(path.is_symlink() for path in (store_root, *store_root.parents)):
            raise StorageError('STORAGE_UNAVAILABLE')
        private_directory(store_root)
        for name in ('store', 'requests', 'artifacts', 'cache'):
            directory = managed_directory(self.root / name, self.root)
            if any(path.is_symlink() for path in directory.rglob('*')):
                raise StorageError('STORAGE_UNAVAILABLE')
            probe = directory / ('.probe-' + secrets.token_hex(8))
            atomic_json(probe, {})
            probe.unlink()
        if (store_root / 'rules').is_symlink():
            raise StorageError('STORAGE_UNAVAILABLE')
        private_directory(store_root / 'rules')
        # Reject symlinks before the legacy store can follow or chmod them.
        for path in store_root.rglob('*'):
            if path.is_symlink():
                raise StorageError('STORE_INVALID')
        from engine.navigator import CheckpointStore
        index_path = store_root / 'index.json'
        if index_path.exists():
            try:
                index = read_json(index_path)
                if (type(index) is not dict or index.get('schema_version') != 1 or
                        type(index.get('entries')) is not list or
                        index.get('integrity', {}).get('document_hash') != CheckpointStore.hash_document(index)):
                    raise ValueError()
            except (OSError, ValueError, TypeError, AttributeError):
                raise StorageError('STORE_INVALID')
        self.store = CheckpointStore(store_root, producer={
            'app_id': 'chombo', 'app_version': metadata['version'],
            'build_revision': metadata['source_revision'], 'engine_version': 'chombo-engine.v0.3'})
        from engine.rule_drafts import DraftStore, DraftValidator
        database = store_root / 'rules/custom-rules.sqlite3'
        if not database.exists():
            fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        self.rules = DraftStore(database.parent, validator=DraftValidator(assets_root) if assets_root else None)
        self.recovery_required = any(item['disposition'] not in ('COMPLETED', 'DISCARDED')
                                     for item in self.store.recovery.classify_all('local-user'))


class SafeLog:
    EVENTS = {'RUNTIME_READY', 'RUNTIME_STOPPED', 'RUNTIME_FAILED'}

    def __init__(self, directory, instance_id, level, on_failure):
        self.directory = Path(directory).resolve()
        self.instance_id = instance_id
        self.on_failure = on_failure
        self.failed = False
        managed_directory(self.directory, self.directory)
        for suffix in ('', '.1', '.2', '.3', '.4'):
            if (self.directory / ('runtime.jsonl' + suffix)).is_symlink():
                raise StorageError('STORAGE_UNAVAILABLE')
        probe = self.directory / ('.probe-' + secrets.token_hex(8))
        atomic_json(probe, {})
        probe.unlink()
        path = self.directory / 'runtime.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        owner = self
        class Handler(RotatingFileHandler):
            def handleError(self, record):
                owner.failure()
            def _open(self):
                fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                return os.fdopen(fd, 'a', encoding='utf-8')
        self.handler = Handler(path, maxBytes=5 * 1024 * 1024, backupCount=4, encoding='utf-8')
        self.level = getattr(logging, level)

    def failure(self):
        if not self.failed:
            self.failed = True
            self.on_failure()

    def event(self, code, level='INFO'):
        if code not in self.EVENTS or level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
            raise ValueError('unapproved log field')
        if getattr(logging, level) < self.level:
            return
        body = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': level,
                'event_code': code, 'instance_id': self.instance_id}
        self.handler.handle(logging.LogRecord('runtime', getattr(logging, level), '', 0, json.dumps(body), (), None))

    def close(self):
        try:
            self.handler.flush()
            os.fsync(self.handler.stream.fileno())
        except OSError:
            self.failure()
        finally:
            try:
                self.handler.close()
            except OSError:
                self.failure()


class RequestLedger:
    """One bounded durable record per request, including deleted-result tombstones.

    The caller holds the runtime writer lock. PREPARED is persisted before any
    side effect; restart never treats that state as permission to execute again.
    """
    def __init__(self, root, limit=1024, *, store=None):
        self.root = Path(root)
        self.limit = limit
        self.store = store
        self.path = self.root / 'ledger.json'
        if self.path.is_symlink():
            raise StorageError('STORE_INVALID')
        self.records = read_json(self.path) if self.path.exists() else []
        if type(self.records) is not list or len(self.records) > limit:
            raise StorageError('STORE_INVALID')
        seen = set()
        for record in self.records:
            if (type(record) is not dict or set(record) != {'owner', 'request_id', 'digest', 'operation_id', 'state', 'result_ref'} or
                    not all(isinstance(record[k], str) and record[k] for k in ('owner', 'request_id', 'digest', 'operation_id')) or
                    record['state'] not in ('PREPARED', 'COMMITTED', 'DELETED') or
                    (record['state'] != 'COMMITTED' and record['result_ref'] is not None) or
                    (record['state'] == 'COMMITTED' and (not isinstance(record['result_ref'], str) or not record['result_ref'])) or
                    (record['owner'], record['request_id']) in seen):
                raise StorageError('STORE_INVALID')
            seen.add((record['owner'], record['request_id']))

    def prepare(self, owner, request_id, digest, operation_id):
        if not all(isinstance(value, str) and value for value in (owner, request_id, digest, operation_id)):
            raise StorageError('REQUEST_INVALID')
        for record in self.records:
            if (record['owner'], record['request_id']) == (owner, request_id):
                if record['digest'] != digest:
                    raise StorageError('REQUEST_ID_CONFLICT')
                return dict(record)
        if len(self.records) >= self.limit:
            raise StorageError('RESOURCE_LIMIT')
        if self.store is not None:
            self.store.ensure_operation_capacity()
        record = dict(owner=owner, request_id=request_id, digest=digest, operation_id=operation_id, state='PREPARED', result_ref=None)
        updated = self.records + [record]
        atomic_json(self.path, updated)
        self.records = updated
        return dict(record)

    def finish(self, owner, request_id, result_ref, *, deleted=False):
        if not deleted and (not isinstance(result_ref, str) or not result_ref):
            raise StorageError('REQUEST_INVALID')
        updated = [dict(item) for item in self.records]
        for record in updated:
            if (record['owner'], record['request_id']) == (owner, request_id):
                if record['state'] == 'DELETED':
                    raise StorageError('RESULT_DELETED')
                if record['state'] == 'COMMITTED' and not deleted:
                    if record['result_ref'] != result_ref:
                        raise StorageError('REQUEST_ID_CONFLICT')
                    return
                record.update(state='DELETED' if deleted else 'COMMITTED', result_ref=None if deleted else result_ref)
                atomic_json(self.path, updated)
                self.records = updated
                return
        raise StorageError('REQUEST_NOT_FOUND')

    def read(self, owner, request_id):
        for record in self.records:
            if (record['owner'], record['request_id']) == (owner, request_id):
                if record['state'] == 'PREPARED':
                    raise StorageError('RECOVERY_REQUIRED')
                return dict(record)
        raise StorageError('REQUEST_NOT_FOUND')


class ArtifactStore:
    """Owner-scoped managed bytes; transport adapters must supply trusted owner."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'index.json'
        if self.path.is_symlink():
            raise StorageError('STORE_INVALID')
        self.index = read_json(self.path) if self.path.exists() else {}
        if type(self.index) is not dict:
            raise StorageError('STORE_INVALID')
        for key, item in self.index.items():
            if (re.fullmatch('[0-9a-f]{32}', key) is None or type(item) is not dict or
                    set(item) != {'owner', 'media_type', 'size', 'sha256'} or
                    not isinstance(item['owner'], str) or not item['owner'] or
                    item['media_type'] not in ('image/png', 'application/json', 'text/plain') or
                    type(item['size']) is not int or item['size'] < 0 or
                    not isinstance(item['sha256'], str) or re.fullmatch('[0-9a-f]{64}', item['sha256']) is None):
                raise StorageError('STORE_INVALID')

    def save(self, owner, payload, media_type):
        if not isinstance(owner, str) or not owner or not isinstance(payload, bytes) or media_type not in ('image/png', 'application/json', 'text/plain'):
            raise StorageError('ARTIFACT_INVALID')
        artifact_id = secrets.token_hex(16)
        path = self.root / artifact_id
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        entry = {'owner': owner, 'media_type': media_type, 'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        updated = dict(self.index, **{artifact_id: entry})
        atomic_json(self.path, updated)
        self.index = updated
        return artifact_id

    def read(self, owner, artifact_id):
        if not isinstance(artifact_id, str) or re.fullmatch('[0-9a-f]{32}', artifact_id) is None:
            raise StorageError('ARTIFACT_NOT_FOUND')
        entry = self.index.get(artifact_id)
        if entry is None or entry['owner'] != owner:
            raise StorageError('ARTIFACT_NOT_FOUND')
        path = self.root / artifact_id
        if path.is_symlink():
            raise StorageError('ARTIFACT_INVALID')
        payload = path.read_bytes()
        if len(payload) != entry['size'] or hashlib.sha256(payload).hexdigest() != entry['sha256']:
            raise StorageError('ARTIFACT_INVALID')
        return payload, entry['media_type']
