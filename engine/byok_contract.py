"""Provider-independent send-attempt state, failures and closed fact selection."""
from copy import deepcopy
import json
import socket
import threading

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class ProviderFailure(Exception):
    def __init__(self, code, delivery='NOT_SENT', retryable=False, retry_after=None):
        super().__init__(code)
        self.code, self.delivery, self.retryable = code, delivery, retryable
        self.retry_after = retry_after


class Attempt:
    def __init__(self):
        self.cancelled = threading.Event()
        self.connection = None
        self.started = False
        self.lock = threading.RLock()
        self.claim = lambda: None

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            if self.connection:
                try:
                    if self.connection.sock:
                        self.connection.sock.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                except OSError:
                    pass

def validate_selection(value, facts):
    """Only local source values reach the answer; never render provider prose."""
    if type(value) is not dict or set(value) != {'decision', 'blocks'} or type(value['blocks']) is not list:
        raise ProviderFailure('OUTPUT_INVALID', 'STARTED')
    blocks = value['blocks']
    if (value['decision'] not in ('ANSWER', 'OUT_OF_SCOPE') or len(blocks) > 32 or
            (value['decision'] == 'ANSWER') != bool(blocks)):
        raise ProviderFailure('OUTPUT_INVALID', 'STARTED')
    allowed = {f['fact_id']: f for f in facts}
    selected = []
    seen = set()
    for block in blocks:
        if type(block) is not dict or set(block) != {'fact_id', 'template_id'} or type(block['fact_id']) is not str:
            raise ProviderFailure('OUTPUT_INVALID', 'STARTED')
        fact = allowed.get(block['fact_id'])
        if not fact or block['template_id'] != fact['template_id'] or block['fact_id'] in seen:
            raise ProviderFailure('OUTPUT_INVALID', 'STARTED')
        seen.add(block['fact_id'])
        selected.append(deepcopy(fact))
    return {'decision': value['decision'], 'blocks': selected}

