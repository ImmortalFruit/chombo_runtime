"""Short-lived one-permission/one-attempt orchestration; caller holds service lock."""
from copy import deepcopy
import hashlib
import threading
from uuid import uuid4

from engine.byok_contract import Attempt, ProviderFailure, canonical, validate_selection
from engine.navigator import NavigatorError
from engine.session_credentials import CredentialError


class ByokDialogue:
    def __init__(self, services, adapter):
        self.services = services
        self.adapter = adapter
        self.slots = threading.BoundedSemaphore(2)
        self.closed = False

    def bind(self, item, binding):
        if binding:
            item['binding'] = binding

    def connection_preview(self, binding, revision):
        services = self.services
        with services.lock:
            services.runtime.credentials.check(binding[1], binding[0], revision)
            if len(services.interactions) >= 128:
                raise NavigatorError('RESOURCE_LIMIT')
            iid = str(uuid4())
            item = {'test': True, 'binding': binding, 'channel': 'STANDALONE_BYOK',
                    'question': 'Return the supplied connection check fact.', 'deadline': services.clock() + 900,
                    'public': {'interaction_id': iid, 'state': 'COMPLETED', 'revision': 0, 'reason_code': None,
                               'position_ref': None, 'confirmation': None, 'send_preview': None, 'failure': None,
                               'target': None, 'knowledge_ref': None, 'result': {}}}
            services.interactions[iid] = item
            self.prepare(item)
            return deepcopy(item['public'])

    def prepare(self, item):
        public = item['public']
        binding = item.get('binding')
        try:
            if self.closed or not binding:
                raise CredentialError('LLM_UNCONFIGURED')
            provider, token = binding
            revision = self.services.runtime.credentials.check(token, provider)
        except CredentialError as error:
            self.fail(item, ProviderFailure(error.code))
            return
        # Retain local evidence privately even after the public result is cleared.
        if public['result'] is not None:
            item['evidence'] = deepcopy(public['result'])
        evidence = item['evidence']
        if item.get('test'):
            facts = [{'fact_id': str(uuid4()), 'template_id': 'connection.check', 'source': {'kind': 'CONNECTION_CHECK'},
                      'value': 'CHOMBO_CONNECTION_OK'}]
        elif item.get('knowledge'):
            facts = [{'fact_id': str(uuid4()), 'template_id': 'lesson.section', 'source': {
                'lesson_id': item['knowledge']['lesson_id'], 'version': item['knowledge']['version'],
                'sha256': item['knowledge']['sha256'], 'section': '学習内容'},
                'value': evidence['text']}]
        elif evidence.get('subject') == 'PUBLIC_TABLE':
            # Explicit allowlist: no full projection, hidden tiles, RNG, refs or grants.
            state = evidence['public_state']
            value = {'seats': {seat: {key: details[key] for key in ('points', 'river', 'melds') if key in details}
                               for seat, details in state.get('seats', {}).items()}}
            facts = [{'fact_id': str(uuid4()), 'template_id': 'table.public', 'source': {'pointer': '/public_state'}, 'value': value}]
        else:
            facts = [{'fact_id': str(uuid4()), 'template_id': 'shape.result', 'source': {'pointer': '/engine_results/shape'},
                      'value': evidence['engine_results']['shape']}]
        item['facts'] = facts
        preview = self.adapter.prepare(provider, item['question'], facts)
        preview.update(send_preview_id=str(uuid4()), attempt_id=str(uuid4()), config_revision=revision,
                       expires_in_seconds=int(max(0, item['deadline'] - self.services.clock())),
                       position_ref=deepcopy(public['position_ref']))
        item['preview'] = deepcopy(preview)
        public.update(state='WAITING_SEND_PERMISSION', reason_code=None, result=None, failure=None, send_preview=preview)

    @staticmethod
    def fail(item, failure):
        record = {'failure_id': str(uuid4()), 'code': failure.code, 'retryable': failure.retryable,
                  'delivery_status': failure.delivery}
        if failure.retry_after is not None:
            record['retry_after'] = failure.retry_after
        item['public'].update(state='FAILED', reason_code=failure.code, result=None, send_preview=None, failure=record)

    def check(self, item):
        """Recheck credentials for both pending attempts and terminal disclosure."""
        if not item.get('preview') or item['public']['state'] in {'CANCELLED', 'EXPIRED', 'DENIED', 'FAILED'}:
            return
        try:
            provider, token = item['binding']
            self.services.runtime.credentials.check(token, provider, item['preview']['config_revision'])
            if self.closed:
                raise CredentialError('RUNTIME_STOPPING')
        except CredentialError as error:
            self.cancel(item)
            revision = item['public']['revision'] + 1
            if error.code == 'PROVIDER_CONFIGURATION_CHANGED' and item['public']['state'] != 'COMPLETED':
                self.fail(item, ProviderFailure(error.code, 'UNKNOWN' if item.get('attempt') and item['attempt'].started else 'NOT_SENT', True))
            else:
                item['public'].update(state='EXPIRED', reason_code=error.code, result=None, send_preview=None)
            item['public']['revision'] = revision

    def cancel(self, item):
        attempt = item.get('attempt')
        if attempt:
            self.adapter.cancel(attempt)

    def operation(self, item, op, inputs):
        public = item['public']
        if op == 'interaction.retry':
            failure = public['failure']
            if public['state'] != 'FAILED' or not failure or inputs['failure_id'] != failure['failure_id']:
                raise NavigatorError('FAILURE_CONFLICT')
            if not failure['retryable']:
                raise NavigatorError('CAPABILITY_UNAVAILABLE')
            self.prepare(item)
            public['revision'] += 1
            return deepcopy(public)
        preview = item.get('preview')
        if public['state'] != 'WAITING_SEND_PERMISSION' or not preview or inputs['send_preview_id'] != preview['send_preview_id']:
            raise NavigatorError('SEND_PREVIEW_CONFLICT')
        if inputs['decision'] == 'DENY':
            public.update(state='CANCELLED', reason_code='SEND_NOT_AUTHORIZED', send_preview=None, revision=public['revision'] + 1)
            item.pop('question', None)
            for key in ('facts', 'preview', 'evidence'):
                item.pop(key, None)
            return deepcopy(public)
        # Detect even internal mutation; no caller-provided payload is accepted.
        if (canonical(preview) != canonical(public['send_preview']) or
                hashlib.sha256(canonical(preview['payload'])).hexdigest() != preview['payload_digest']):
            raise NavigatorError('SEND_PREVIEW_CONFLICT')
        self.check(item)
        if public['state'] != 'WAITING_SEND_PERMISSION':
            return deepcopy(public)
        if self.closed:
            raise NavigatorError('CAPABILITY_UNAVAILABLE')
        if not self.slots.acquire(blocking=False):
            self.fail(item, ProviderFailure('RESOURCE_LIMIT', retryable=True))
            public['revision'] += 1
            return deepcopy(public)
        attempt = Attempt()
        item['attempt'] = attempt
        item['attempt_deadline'] = min(item['deadline'], self.services.clock() + 60)
        public.update(state='RUNNING', send_preview=None, revision=public['revision'] + 1)
        # The worker first takes the lock, so permission is consumed before it can run.
        worker = threading.Thread(target=self._work, args=(item, attempt), daemon=True, name='chombo-byok')
        worker.start()
        return deepcopy(public)

    def _work(self, item, attempt):
        services = self.services
        try:
            with services.lock:
                public = services._interaction(item['public']['interaction_id'])
                if public['state'] != 'RUNNING' or item.get('attempt') is not attempt:
                    return
                preview = deepcopy(item['preview'])
                token = item['binding'][1]
                timeout = min(60, item['deadline'] - services.clock())
                if timeout <= 0:
                    return
                def claim():
                    with services.lock:
                        state = services._interaction(item['public']['interaction_id'])
                        if state['state'] != 'RUNNING' or item.get('attempt') is not attempt:
                            raise ProviderFailure('USER_CANCELLED')
                        with services.runtime.credentials.lock:
                            services.runtime.credentials.check(token, preview['provider'], preview['config_revision'])
                            with attempt.lock:
                                if attempt.cancelled.is_set():
                                    raise ProviderFailure('USER_CANCELLED')
                                attempt.started = True
                attempt.claim = claim
            try:
                value = self.adapter.send(preview, token, attempt, timeout)
                result = validate_selection(value, item['facts'])
                failure = None
            except ProviderFailure as error:
                result, failure = None, error
            except Exception:
                result, failure = None, ProviderFailure('OUTPUT_INVALID', 'UNKNOWN')
            with services.lock:
                current = services._interaction(item['public']['interaction_id'])
                if current['state'] != 'RUNNING' or item.get('attempt') is not attempt:
                    return
                if failure:
                    self.fail(item, failure)
                else:
                    item['public'].update(state='COMPLETED', reason_code=None, result=result, failure=None)
                item['public']['revision'] += 1
        finally:
            self.slots.release()

    def tick(self):
        with self.services.lock:
            for item in self.services.interactions.values():
                if item.get('binding'):
                    self.services._interaction(item['public']['interaction_id'])

    def close(self):
        with self.services.lock:
            self.closed = True
            for item in self.services.interactions.values():
                if item.get('binding'):
                    self.cancel(item)
                    if item['public']['state'] not in {'CANCELLED', 'EXPIRED', 'DENIED'}:
                        item['public'].update(state='EXPIRED', reason_code='RUNTIME_STOPPING', result=None,
                                              send_preview=None, revision=item['public']['revision'] + 1)
                    for key in ('question', 'evidence', 'facts', 'preview'):
                        item.pop(key, None)
