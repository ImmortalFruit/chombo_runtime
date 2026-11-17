"""Browser session-cookie credentials. No disk, OS store, environment or network access."""
import secrets
import threading
import time


class CredentialError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SessionCredentials:
    PROVIDERS = ("openai", "anthropic", "google")
    TTL = 3600
    LIMIT = 128

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.closed = False

    def expire(self):
        with self.lock:
            self._prune()

    def _prune(self):
        for token, entry in list(self.sessions.items()):
            if entry['expires'] <= self.clock():
                self._drop(token)

    def _drop(self, token):
        entry = self.sessions.pop(token, None)
        if entry and entry['secret'] is not None:
            entry['secret'][:] = b'\0' * len(entry['secret'])

    def open(self, provider="openai"):
        if provider not in self.PROVIDERS:
            raise CredentialError("INVALID_REQUEST")
        with self.lock:
            self._prune()
            if self.closed:
                raise CredentialError("CAPABILITY_UNAVAILABLE")
            if len(self.sessions) >= self.LIMIT:
                raise CredentialError('RESOURCE_LIMIT')
            token = secrets.token_urlsafe(32)
            self.sessions[token] = dict(secret=None, revision=0, provider=provider, expires=self.clock() + self.TTL)
            return dict(session_token=token, expires_in_seconds=self.TTL)

    def execute(self, token, action, body, provider="openai"):
        with self.lock:
            self._prune()
            entry = self.sessions.get(token)
            if entry is None or entry['provider'] != provider:
                raise CredentialError('BROWSER_SESSION_EXPIRED')
            fields = {'status': set(), 'end': set(), 'activity': set(), 'set': {'secret', 'expected_config_revision'},
                      'delete': {'expected_config_revision'}, 'test-preview': {'expected_config_revision'}}
            if action not in fields or type(body) is not dict or set(body) != fields[action]:
                raise CredentialError('INVALID_REQUEST')
            if 'expected_config_revision' in body:
                revision = body['expected_config_revision']
                if type(revision) is not int or revision != entry['revision']:
                    raise CredentialError('CONFIG_REVISION_CONFLICT')
            if action == 'set':
                secret = body['secret']
                if (not isinstance(secret, str) or not secret or
                        any(ord(c) < 33 or ord(c) == 127 for c in secret)):
                    raise CredentialError('INVALID_REQUEST')
                try:
                    raw = bytearray(secret, 'utf-8')
                except UnicodeError:
                    raise CredentialError('INVALID_REQUEST') from None
                if len(raw) > 2048:
                    raise CredentialError('INVALID_REQUEST')
                if entry['secret'] is not None:
                    entry['secret'][:] = b'\0' * len(entry['secret'])
                entry['secret'] = raw
                entry['revision'] += 1
                entry['expires'] = self.clock() + self.TTL
            elif action == 'activity':
                entry['expires'] = self.clock() + self.TTL
            elif action == 'delete':
                if entry['secret'] is not None:
                    entry['secret'][:] = b'\0' * len(entry['secret'])
                revision = 0
                self._drop(token)
                return dict(configured=False, available=True, reason_code=None, config_revision=revision)
            elif action == 'end':
                self._drop(token)
                return {'ended': True}
            elif action == 'test-preview':
                # Provider preview and one-use permission belong to I51-08.
                raise CredentialError('CAPABILITY_UNAVAILABLE')
            return dict(configured=entry['secret'] is not None, available=True,
                        reason_code=None, config_revision=entry['revision'])

    def clear(self):
        with self.lock:
            for token in list(self.sessions):
                self._drop(token)

    def check(self, token, provider, revision=None):
        """Non-secret reference check. Reads never renew the idle deadline."""
        status = self.execute(token, 'status', {}, provider)
        if not status['configured']:
            raise CredentialError('LLM_UNCONFIGURED')
        if revision is not None and status['config_revision'] != revision:
            raise CredentialError('PROVIDER_CONFIGURATION_CHANGED')
        return status['config_revision']

    def use(self, token, provider, revision):
        """Only the outbound adapter calls this immediately before transport."""
        with self.lock:
            self.check(token, provider, revision)
            return self.sessions[token]['secret'].decode('utf-8')

    def close(self):
        with self.lock:
            self.closed = True
            self.clear()
