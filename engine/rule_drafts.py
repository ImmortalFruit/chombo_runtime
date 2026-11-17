"""Closed custom-rule validation and transactional, immutable revision storage.

The approval catalog authorizes complete registered combinations, never the
Cartesian product of their individual values. No domain knowledge is created here.
"""
from __future__ import annotations

from engine.knowledge_store import KnowledgeStore

import copy
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from engine.rule_profiles import (
    ResolvedRuleProfile, RuleProfileError, _canonical_snapshot_id, _freeze,
    load_complete_profile_scope,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = 'four_player_east_south_complete.v1'
VALIDATOR_VERSION = '1.0.0'
MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_ANCESTORS = 32
DRAFT_FIELDS = set('schema_version draft_id revision custom_ruleset_id display_name scope_id base_ref overrides provenance created_at updated_at'.split())
CONFIRMED_FIELDS = set('schema_version custom_ruleset_id version profile_id scope_id source_draft base_ref dimensions lineage validation_ref snapshot_id confirmed_at'.split())
REF_FIELDS = set('kind ruleset_id ruleset_version profile_id snapshot_id'.split())


def fail(code, **details):
    raise RuleProfileError(code, details)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def snapshot(document):
    return digest({k: v for k, v in document.items() if k != 'snapshot_id'})


def _json_tree(value, depth=0):
    if depth > MAX_DEPTH:
        fail('RESOURCE_LIMIT', resource='json_depth')
    if type(value) is dict:
        if not all(type(k) is str for k in value):
            fail('MALFORMED_DRAFT', reason='NON_STRING_KEY')
        for child in value.values():
            _json_tree(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _json_tree(child, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        fail('MALFORMED_DRAFT', reason='NON_JSON_VALUE')
    elif type(value) is float and not math.isfinite(value):
        fail('MALFORMED_DRAFT', reason='NON_FINITE_NUMBER')


def strict_json(raw):
    """Parse untrusted JSON before it enters managed storage."""
    if not isinstance(raw, (str, bytes)):
        fail('MALFORMED_DRAFT', reason='EXPECTED_JSON_TEXT')
    if len(raw.encode('utf-8') if isinstance(raw, str) else raw) > MAX_BYTES:
        fail('RESOURCE_LIMIT', resource='file_size')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('MALFORMED_DRAFT', reason='DUPLICATE_KEY', key=key)
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: fail('MALFORMED_DRAFT', reason='NON_FINITE_NUMBER'))
        _json_tree(result)
        return result
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        if isinstance(error, RuleProfileError):
            raise
        if isinstance(error, RecursionError):
            fail('RESOURCE_LIMIT', resource='json_depth')
        fail('MALFORMED_DRAFT', reason=type(error).__name__)


def _bounded(value):
    _json_tree(value)
    try:
        if len(canonical(value).encode('utf-8')) > MAX_BYTES:
            fail('RESOURCE_LIMIT', resource='file_size')
    except (UnicodeError, ValueError) as error:
        if isinstance(error, RuleProfileError):
            raise
        fail('MALFORMED_DRAFT', reason='INVALID_JSON_ENCODING')


def _fields(value, expected):
    if type(value) is not dict or set(value) != expected:
        fail('MALFORMED_DRAFT', expected_fields=sorted(expected))


def _uuid(value, custom=False):
    try:
        text = value.removeprefix('custom.') if custom else value
        if custom and not value.startswith('custom.'):
            raise ValueError()
        if str(UUID(text)) != text:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        fail('MALFORMED_DRAFT', reason='INVALID_ID')


def _positive(value):
    if type(value) is not int or value < 1:
        fail('MALFORMED_DRAFT', reason='INVALID_VERSION')


def _text(value):
    if type(value) is not str or not value.strip():
        fail('MALFORMED_DRAFT', reason='EMPTY_TEXT')


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError()
        return parsed
    except (TypeError, AttributeError, ValueError):
        fail('MALFORMED_DRAFT', reason='INVALID_UTC_TIME')


def _hash(value):
    if type(value) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
        fail('MALFORMED_DRAFT', reason='INVALID_DIGEST')


def validate_ref(ref):
    _fields(ref, REF_FIELDS)
    if ref['kind'] not in ('OFFICIAL', 'CUSTOM_CONFIRMED'):
        fail('MALFORMED_DRAFT', reason='INVALID_BASE_KIND')
    _hash(ref['snapshot_id'])
    for field in ('ruleset_id', 'profile_id'):
        _text(ref[field])
    if ref['kind'] == 'CUSTOM_CONFIRMED':
        _uuid(ref['ruleset_id'], custom=True)
        _positive(ref['ruleset_version'])
        if ref['profile_id'] != f"{ref['ruleset_id']}.v{ref['ruleset_version']}":
            fail('MALFORMED_DRAFT', reason='PROFILE_ID_MISMATCH')
    else:
        _text(ref['ruleset_version'])


def validate_draft_structure(draft):
    _bounded(draft)
    _fields(draft, DRAFT_FIELDS)
    if type(draft['schema_version']) is not int or draft['schema_version'] != 1:
        fail('UNKNOWN_SCHEMA_VERSION')
    _uuid(draft['draft_id'])
    _uuid(draft['custom_ruleset_id'], custom=True)
    _positive(draft['revision'])
    _text(draft['display_name'])
    if draft['scope_id'] != SCOPE:
        fail('RULE_PROFILE_INCOMPLETE', reason='SCOPE_MISMATCH')
    if _time(draft['updated_at']) < _time(draft['created_at']):
        fail('MALFORMED_DRAFT', reason='TIME_ORDER')
    validate_ref(draft['base_ref'])
    if type(draft['overrides']) is not dict or type(draft['provenance']) is not dict:
        fail('MALFORMED_DRAFT')
    for key, record in draft['provenance'].items():
        _text(key)
        _fields(record, set('kind actor_id recorded_at request_summary'.split()))
        if record['kind'] not in ('USER_REQUEST', 'IMPORTED_DRAFT'):
            fail('MALFORMED_DRAFT', reason='PROVENANCE_KIND')
        _text(record['actor_id'])
        _text(record['request_summary'])
        _time(record['recorded_at'])
    for dimension, override in draft['overrides'].items():
        _text(dimension)
        _fields(override, {'value', 'provenance_id'})
        if type(override['provenance_id']) is not str or override['provenance_id'] not in draft['provenance']:
            fail('MALFORMED_DRAFT', reason='PROVENANCE_MISSING')


def new_draft(base_ref, display_name, *, custom_ruleset_id=None):
    now = datetime.now(timezone.utc).isoformat()
    draft = dict(schema_version=1, draft_id=str(uuid4()), revision=1,
                 custom_ruleset_id=custom_ruleset_id or 'custom.' + str(uuid4()),
                 display_name=display_name, scope_id=SCOPE, base_ref=copy.deepcopy(base_ref),
                 overrides={}, provenance={}, created_at=now, updated_at=now)
    validate_draft_structure(draft)
    return draft


def confirmed_ref(document):
    return dict(kind='CUSTOM_CONFIRMED', ruleset_id=document['custom_ruleset_id'],
                ruleset_version=document['version'], profile_id=document['profile_id'],
                snapshot_id=document['snapshot_id'])


def _shape(value, exemplar):
    """Full objects and exact scalar types; novel semantic values need approval."""
    if type(value) is not type(exemplar):
        return False
    if type(value) is dict:
        return set(value) == set(exemplar) and all(_shape(value[k], exemplar[k]) for k in value)
    if type(value) is list:
        # Lists may contain a novel number of entries, but never a novel JSON type.
        return all(any(_shape(item, example) for example in exemplar) for item in value)
    return True


class DraftValidator:
    def __init__(self, root=ROOT):
        self.root = Path(root)

    def catalogs(self):
        """Reload authoritative evidence on every operation; saved labels grant nothing."""
        directory = self.root / 'engine/profiles'
        approval = strict_json((directory / 'custom_rule_approval.v1.json').read_bytes())
        capability = strict_json((directory / 'custom_rule_capability.v1.json').read_bytes())
        if (approval.get('schema_version') != 1 or approval.get('scope_id') != SCOPE
                or approval.get('policy') != 'EXACT_COMPLETE_COMBINATION_ONLY'
                or capability.get('schema_version') != 1
                or capability.get('policy') != 'EXACT_TESTED_VALUES_ONLY'):
            fail('UNKNOWN_SCHEMA_VERSION', reason='CATALOG_CONTRACT')
        required = load_complete_profile_scope(directory / 'four_player_east_south_complete_scope.v1.json')
        concepts = {}
        store = KnowledgeStore(self.root)
        for path in store.concept_paths():
            doc = strict_json(store.read_bytes(path))
            for item in doc.get('concepts', []):
                concepts[item['concept_id']] = {'status': doc.get('status'), 'digest': digest(doc)}
        profiles = []
        for entry in approval['profiles']:
            path = (directory / entry['path']).resolve()
            if path.parent != directory.resolve():
                fail('MALFORMED_DRAFT', reason='CATALOG_PATH')
            doc = strict_json(path.read_bytes())
            actual = dict(kind='OFFICIAL', ruleset_id=doc['ruleset_id'],
                          ruleset_version=doc['ruleset_version'], profile_id=doc['profile_id'],
                          snapshot_id=_canonical_snapshot_id(doc))
            if actual != entry['ref']:
                fail('SNAPSHOT_MISMATCH', ref=entry['ref'])
            if doc['scope_id'] != SCOPE or set(doc['dimensions']) != required:
                fail('RULE_PROFILE_INCOMPLETE')
            profiles.append((entry, doc))
        code_hashes = {path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
                       for path in capability['implementation_files']}
        return approval, capability, required, concepts, profiles, code_hashes

    def resolve(self, ref, lookup, profiles, seen=(), *, confirmed_root=False):
        validate_ref(ref)
        identity = canonical(ref)
        if identity in seen:
            fail('REFERENCE_CYCLE', ref=ref)
        # A confirmed document being executed is the subject, not its own ancestor.
        if len(seen) >= MAX_ANCESTORS + int(confirmed_root):
            fail('RESOURCE_LIMIT', resource='ancestors')
        if ref['kind'] == 'OFFICIAL':
            for entry, doc in profiles:
                if entry['ref'] == ref:
                    dimensions = {key: dict(value=copy.deepcopy(item['value']),
                        concept_ids=copy.deepcopy(item['concept_ids']), origin_ref=copy.deepcopy(ref),
                        override_provenance_id=None, approval_refs=[entry['approval_record']])
                        for key, item in doc['dimensions'].items()}
                    return dimensions, [copy.deepcopy(ref)]
                if entry['ref']['profile_id'] == ref['profile_id']:
                    fail('SNAPSHOT_MISMATCH', ref=ref)
            fail('REFERENCE_MISSING', ref=ref)
        doc = lookup(ref) if lookup else None
        if doc is None:
            fail('REFERENCE_MISSING', ref=ref)
        _bounded(doc)
        _fields(doc, CONFIRMED_FIELDS)
        if type(doc['schema_version']) is not int or doc['schema_version'] != 1:
            fail('UNKNOWN_SCHEMA_VERSION')
        validate_ref(confirmed_ref(doc))
        if confirmed_ref(doc) != ref:
            fail('SNAPSHOT_MISMATCH', ref=ref)
        if doc['scope_id'] != SCOPE:
            fail('RULE_PROFILE_INCOMPLETE')
        _, lineage = self.resolve(doc['base_ref'], lookup, profiles, (*seen, identity),
                                  confirmed_root=confirmed_root)
        if snapshot(doc) != ref['snapshot_id']:
            fail('SNAPSHOT_MISMATCH', ref=ref)
        if doc['lineage'] != lineage:
            fail('SNAPSHOT_MISMATCH', reason='LINEAGE_MISMATCH')
        _time(doc['confirmed_at'])
        _fields(doc['source_draft'], {'draft_id', 'revision', 'input_digest'})
        _uuid(doc['source_draft']['draft_id'])
        _positive(doc['source_draft']['revision'])
        _hash(doc['source_draft']['input_digest'])
        _fields(doc['validation_ref'], set('validation_ref validator_version approval_catalog_digest capability_catalog_digest'.split()))
        _text(doc['validation_ref']['validator_version'])
        for key in ('validation_ref', 'approval_catalog_digest', 'capability_catalog_digest'):
            _hash(doc['validation_ref'][key])
        if type(doc['dimensions']) is not dict:
            fail('RULE_PROFILE_INCOMPLETE')
        for item in doc['dimensions'].values():
            _fields(item, set('value concept_ids origin_ref override_provenance_id approval_refs'.split()))
            for key in ('concept_ids', 'approval_refs'):
                if type(item[key]) is not list or not item[key]:
                    fail('MALFORMED_DRAFT', reason='MISSING_EVIDENCE')
                for text in item[key]:
                    _text(text)
            if item['override_provenance_id'] is not None:
                _text(item['override_provenance_id'])
            origin = item['origin_ref']
            if type(origin) is dict and set(origin) == {'draft_id', 'revision'}:
                _uuid(origin['draft_id'])
                _positive(origin['revision'])
            else:
                validate_ref(origin)
        return copy.deepcopy(doc['dimensions']), lineage + [copy.deepcopy(ref)]

    def validate(self, draft, lookup=None, *, _confirmed_root=False):
        result = dict(draft_id=draft.get('draft_id') if type(draft) is dict else None,
                      revision=draft.get('revision') if type(draft) is dict else None,
                      input_digest=None, validator_version=VALIDATOR_VERSION,
                      approval_catalog_digest=None, capability_catalog_digest=None,
                      status='INVALID', issues=[], resolved_preview=None,
                      execution_support={route:dict(status='NOT_EVALUATED', reasons=[], implementation_version=None)
                                         for route in ('hand', 'match', 'scoring', 'display')},
                      stages={stage:'NOT_EVALUATED' for stage in ('structure','references','types','approval','constraints','capability')})
        def issue(code, dimensions=(), path='', related_refs=()):
            result['issues'].append(dict(code=code, dimension_ids=list(dimensions), path=path,
                                        related_refs=list(related_refs), message_key='rule_draft.' + code.lower()))
        try:
            validate_draft_structure(draft)
            result['input_digest'] = digest(draft)
            result['stages']['structure'] = 'PASSED'
            approval, capability, required, concepts, profiles, code_hashes = self.catalogs()
            result['approval_catalog_digest'] = digest([approval, concepts])
            result['capability_catalog_digest'] = digest([capability, code_hashes])
            dimensions, lineage = self.resolve(draft['base_ref'], lookup, profiles,
                                                confirmed_root=_confirmed_root)
            result['stages']['references'] = 'PASSED'
            if set(dimensions) != required or set(draft['overrides']) - required:
                fail('RULE_PROFILE_INCOMPLETE')
            for item in dimensions.values():
                item['override_provenance_id'] = None
            for key, override in draft['overrides'].items():
                value = override['value']
                dimensions[key] = dict(value=copy.deepcopy(value), concept_ids=[],
                    origin_ref={'draft_id':draft['draft_id'], 'revision':draft['revision']},
                    override_provenance_id=override['provenance_id'], approval_refs=[])
            for key, item in dimensions.items():
                examples = [p['dimensions'][key]['value'] for _, p in profiles]
                if not any(_shape(item['value'], example) for example in examples):
                    prefix = '/overrides/' if key in draft['overrides'] else '/dimensions/'
                    issue('MALFORMED_DRAFT', [key], prefix + key + '/value')
            if result['issues']:
                return result
            result['stages']['types'] = 'PASSED'
            values = {key:item['value'] for key,item in dimensions.items()}
            matches = [(entry,p) for entry,p in profiles
                       if canonical(values) == canonical({k:v['value'] for k,v in p['dimensions'].items()})]
            for key, item in dimensions.items():
                evidence = [(entry,p['dimensions'][key]) for entry,p in matches]
                if not evidence:
                    known = any(canonical(item['value']) == canonical(p['dimensions'][key]['value']) for _,p in profiles)
                    if not known:
                        issue('UNAPPROVED_VALUE', [key])
                    # Do not retain imported claims as trusted approval metadata.
                    item['concept_ids'] = []
                    item['approval_refs'] = []
                    continue
                entry, source = evidence[0]
                item['concept_ids'] = copy.deepcopy(source['concept_ids'])
                item['approval_refs'] = [entry['approval_record']]
                if any(concepts.get(cid, {}).get('status') != 'QUALIFIED' for cid in source['concept_ids']):
                    issue('UNQUALIFIED_CONCEPT', [key])
                elif any(concepts[cid]['digest'] != entry['concept_digests'].get(cid)
                         for cid in source['concept_ids']):
                    issue('APPROVAL_EVIDENCE_CHANGED', [key])
            if not matches:
                issue('UNAPPROVED_COMBINATION', sorted(draft['overrides']))
            result['stages']['approval'] = 'PASSED' if not result['issues'] else 'REVIEW_REQUIRED'
            # The scoring selector binds to the complete limit value, not a display name.
            for _, profile in profiles:
                pv = profile['dimensions']
                if canonical(values['scoring.engine_profile']) == canonical(pv['scoring.engine_profile']['value']):
                    if canonical(values['scoring.limit_and_rounding']) != canonical(pv['scoring.limit_and_rounding']['value']):
                        issue('CONSTRAINT_VIOLATION', ['scoring.engine_profile','scoring.limit_and_rounding'])
                    break
            result['stages']['constraints'] = 'FAILED' if any(i['code']=='CONSTRAINT_VIOLATION' for i in result['issues']) else 'PASSED'
            result['resolved_preview'] = dict(dimensions=dimensions, lineage=lineage)
            result['status'] = ('INVALID' if result['stages']['constraints']=='FAILED'
                                else 'REVIEW_REQUIRED' if result['issues'] else 'VALIDATED')
            if result['status'] == 'VALIDATED':
                supported_ids = capability.get('supported_profile_ids')
                approved_ids = {entry['ref']['profile_id'] for entry, _ in profiles}
                if (type(supported_ids) is not list or len(set(supported_ids)) != len(supported_ids)
                        or not set(supported_ids) <= approved_ids):
                    fail('MALFORMED_DRAFT', reason='CAPABILITY_PROFILE_IDS')
                matched_id = matches[0][0]['ref']['profile_id']
                for route, config in capability['routes'].items():
                    unsupported = [] if matched_id in supported_ids else list(config['required_dimensions'])
                    result['execution_support'][route] = dict(
                        status='UNSUPPORTED' if unsupported else 'SUPPORTED',
                        reasons=[dict(code='UNSUPPORTED_RULE_VALUE', dimension_ids=unsupported)] if unsupported else [],
                        implementation_version=digest(code_hashes))
                result['stages']['capability'] = 'PASSED'
        except RuleProfileError as error:
            issue(error.code, related_refs=[error.details] if error.details else [])
        return result

    def validation_ref(self, result):
        return digest(result)


class DraftStore:
    """One SQLite transaction publishes version reservation, payload and index.

    The application supplies a managed directory, never a Draft-supplied path.
    FULL synchronous rollback journals protect the last committed revision.
    """
    def __init__(self, directory, validator=None):
        self.validator = validator or DraftValidator()
        self.directory = Path(directory).resolve()
        official = (self.validator.root / 'engine/profiles').resolve()
        if self.directory == official or official in self.directory.parents:
            fail('MALFORMED_DRAFT', reason='OFFICIAL_STORAGE_FORBIDDEN')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'custom-rules.sqlite3'
        if self.database.is_symlink():
            fail('MALFORMED_DRAFT', reason='STORAGE_SYMLINK')
        with self._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(id, revision));
                CREATE TABLE IF NOT EXISTS confirmed (
                    id TEXT NOT NULL, version INTEGER NOT NULL, request TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, PRIMARY KEY(id, version));
            ''')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()

    def lookup(self, ref):
        validate_ref(ref)
        with self._connect() as connection:
            row = connection.execute('SELECT payload FROM confirmed WHERE id=? AND version=?',
                                     (ref['ruleset_id'],ref['ruleset_version'])).fetchone()
        return strict_json(row[0]) if row else None

    def load(self, draft_id, revision=None):
        _uuid(draft_id)
        if revision is not None:
            _positive(revision)
        with self._connect() as connection:
            row = connection.execute('SELECT payload FROM drafts WHERE id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1',
                                     (draft_id, revision, revision)).fetchone()
        if row is None:
            fail('REFERENCE_MISSING')
        draft = strict_json(row[0])
        return draft, self.validator.validate(draft, self.lookup)

    def list_drafts(self):
        """Return only the latest immutable revision of every managed Draft."""
        with self._connect() as connection:
            rows = connection.execute('''
                SELECT d.payload FROM drafts AS d
                JOIN (SELECT id, MAX(revision) AS revision FROM drafts GROUP BY id) AS latest
                  ON latest.id=d.id AND latest.revision=d.revision
                ORDER BY d.id
            ''').fetchall()
        return [strict_json(row[0]) for row in rows]

    def list_confirmed(self):
        """Return immutable confirmed versions without resolving them for execution."""
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM confirmed ORDER BY id, version DESC'
            ).fetchall()
        return [strict_json(row[0]) for row in rows]

    def discard_custom_ruleset(self, custom_ruleset_id):
        """Permanently remove every Draft revision and confirmed version of one custom ruleset."""
        _uuid(custom_ruleset_id, custom=True)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            draft_rows = connection.execute('SELECT id,payload FROM drafts').fetchall()
            draft_ids = {
                draft_id for draft_id, payload in draft_rows
                if strict_json(payload)['custom_ruleset_id'] == custom_ruleset_id
            }
            confirmed_count = connection.execute(
                'SELECT COUNT(*) FROM confirmed WHERE id=?', (custom_ruleset_id,)
            ).fetchone()[0]
            if not draft_ids and not confirmed_count:
                fail('REFERENCE_MISSING')
            for draft_id in draft_ids:
                connection.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
            connection.execute('DELETE FROM confirmed WHERE id=?', (custom_ruleset_id,))
        return {'discarded': True, 'custom_ruleset_id': custom_ruleset_id}

    def save(self, draft, *, expected_revision):
        validate_draft_structure(draft)
        if type(expected_revision) is not int or expected_revision < 0:
            fail('REVISION_CONFLICT')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT revision,payload FROM drafts WHERE id=? ORDER BY revision DESC LIMIT 1', (draft['draft_id'],)).fetchone()
            current = row[0] if row else 0
            if current != expected_revision or draft['revision'] != current + 1:
                fail('REVISION_CONFLICT', current_revision=current)
            if row:
                old = strict_json(row[1])
                if any(old[k] != draft[k] for k in ('custom_ruleset_id','created_at')) or _time(draft['updated_at']) < _time(old['updated_at']):
                    fail('MALFORMED_DRAFT', reason='IMMUTABLE_DRAFT_IDENTITY')
            connection.execute('INSERT INTO drafts VALUES (?,?,?)', (draft['draft_id'], draft['revision'], canonical(draft)))
        return self.load(draft['draft_id'], draft['revision'])

    def import_draft(self, raw, *, expected_revision):
        return self.save(strict_json(raw), expected_revision=expected_revision)

    def confirm(self, *, draft_id, revision, input_digest, validation_ref):
        _uuid(draft_id)
        _positive(revision)
        _hash(input_digest)
        _hash(validation_ref)
        request = digest([draft_id, revision, input_digest, validation_ref])
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT payload FROM confirmed WHERE request=?', (request,)).fetchone()
            if prior:
                return strict_json(prior[0])
            row = connection.execute('SELECT payload FROM drafts WHERE id=? ORDER BY revision DESC LIMIT 1', (draft_id,)).fetchone()
            if row is None:
                fail('REFERENCE_MISSING')
            draft = strict_json(row[0])
            result = self.validator.validate(draft, self.lookup)
            if draft['revision'] != revision or result['input_digest'] != input_digest or self.validator.validation_ref(result) != validation_ref:
                fail('VALIDATION_STALE')
            if result['status'] != 'VALIDATED':
                fail(result['status'])
            row = connection.execute('SELECT MAX(version) FROM confirmed WHERE id=?', (draft['custom_ruleset_id'],)).fetchone()
            version = (row[0] or 0) + 1
            if version > 1 and (draft['base_ref']['kind'] != 'CUSTOM_CONFIRMED' or
                                draft['base_ref']['ruleset_id'] != draft['custom_ruleset_id'] or
                                draft['base_ref']['ruleset_version'] != version - 1):
                fail('VERSION_CONFLICT')
            document = dict(schema_version=1, custom_ruleset_id=draft['custom_ruleset_id'],
                version=version, profile_id=f"{draft['custom_ruleset_id']}.v{version}", scope_id=SCOPE,
                source_draft=dict(draft_id=draft_id, revision=revision, input_digest=input_digest),
                base_ref=copy.deepcopy(draft['base_ref']), dimensions=result['resolved_preview']['dimensions'],
                lineage=result['resolved_preview']['lineage'],
                validation_ref=dict(validation_ref=validation_ref, validator_version=VALIDATOR_VERSION,
                                    approval_catalog_digest=result['approval_catalog_digest'],
                                    capability_catalog_digest=result['capability_catalog_digest']),
                confirmed_at=datetime.now(timezone.utc).isoformat())
            document['snapshot_id'] = snapshot(document)
            _bounded(document)
            # Check evidence again immediately before publishing under the writer lock.
            if self.validator.validation_ref(self.validator.validate(draft, self.lookup)) != validation_ref:
                fail('VALIDATION_STALE')
            connection.execute('INSERT INTO confirmed VALUES (?,?,?,?)',
                (draft['custom_ruleset_id'], version, request, canonical(document)))
        return copy.deepcopy(document)

    def execution_profile(self, ref, *, route):
        """Only a revalidated confirmed reference can cross this execution boundary."""
        validate_ref(ref)
        if ref['kind'] != 'CUSTOM_CONFIRMED':
            fail('MALFORMED_DRAFT', reason='CONFIRMED_REFERENCE_REQUIRED')
        draft = new_draft(ref, 'execution validation')
        result = self.validator.validate(draft, self.lookup, _confirmed_root=True)
        if result['status'] != 'VALIDATED':
            fail(result['status'], issues=result['issues'])
        support = result['execution_support'].get(route)
        if not support or support['status'] != 'SUPPORTED':
            fail('UNSUPPORTED_RULE_VALUE', route=route)
        return ResolvedRuleProfile(ref['profile_id'], ref['snapshot_id'],
                                   _freeze(result['resolved_preview']['dimensions']))
