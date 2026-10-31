"""Independent recovery polling and atomic local processing of push/pull receipts."""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .connectors import NoRedirect
from .webhooks import EVENT, UUID

log = logging.getLogger(__name__)
Number = Annotated[int, Field(ge=0, le=2**53)]


class Queue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pending: Number
    inflight: Number
    delivered: Number
    dead_letter: Number


class Head(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    company_id: Literal['helio']
    source: Literal['helio-crm']
    source_instance_id: UUID
    high_watermark: Number
    earliest_available: Annotated[int, Field(ge=1, le=2**53)]
    capture_enabled: bool
    queue: Queue
    retained_events: Number
    capacity: Annotated[int, Field(ge=1, le=1000000)]


class Page(Head):
    through: Number
    items: Annotated[list[dict], Field(max_length=200)]
    next_after: Number
    has_more: bool


class RecoveryError(Exception):
    pass


class RecoveryClient:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {'','/'} or len(token)<32
                or (parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','crm'})):
            raise ValueError('Invalid activity connector configuration')
        self.url, self.token = url.rstrip('/'), token

    def get(self, path, **params):
        query = '?'+urllib.parse.urlencode(params) if params else ''
        req = urllib.request.Request(self.url+path+query, headers={'Authorization':'Bearer '+self.token})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req,timeout=5) as response:
                body = response.read(2*1024*1024+1)
                if len(body)>2*1024*1024:
                    raise RecoveryError('oversized')
                return json.loads(body)
        except urllib.error.HTTPError as error:
            error.close()
            raise RecoveryError('history_expired' if error.code==410 else f'http_{error.code}') from None
        except (OSError,urllib.error.URLError):
            raise RecoveryError('unavailable') from None
        except (ValueError,TypeError):
            raise RecoveryError('invalid_payload') from None


class ActivityWorker:
    def __init__(self, store, client, *, clock=time.time):
        self.store, self.client, self.clock = store, client, clock
        self.stop_event = threading.Event()
        self.thread = None

    def recover(self):
        try:
            head = Head.model_validate(self.client.get('/activity/status')).model_dump()
            if head['earliest_available'] > head['high_watermark']+1:
                raise RecoveryError('invalid_payload')
            self.store.observe(head,self.clock())
            after = self.store.received_cursor(head)
            through = head['high_watermark']
            for _ in range(10):
                if after >= through or self.stop_event.is_set():
                    break
                page = Page.model_validate(self.client.get('/activity/events',after=after,through=through,
                    source_instance_id=head['source_instance_id'],limit=100)).model_dump()
                if (page['source_instance_id'] != head['source_instance_id'] or page['through'] != through
                        or not page['items'] or len(page['items'])>100):
                    raise RecoveryError('invalid_page')
                expected = after
                validated = []
                for raw in page['items']:
                    event = EVENT.validate_python(raw).model_dump(exclude_unset=True)
                    expected += 1
                    if any(event[k] != head[k] for k in ('company_id','source','source_instance_id')) or event['sequence'] != expected or expected>through:
                        raise RecoveryError('history_gap')
                    validated.append(event)
                if page['next_after'] != expected or page['has_more'] != (expected<through):
                    raise RecoveryError('invalid_page')
                for event in validated:
                    if self.store.accept(event,self.clock()) == 409:
                        raise RecoveryError('identity_conflict')
                after = expected
        except RecoveryError as error:
            self.store.failure('helio','helio-crm',str(error))
        except (ValueError,TypeError,KeyError):
            self.store.failure('helio','helio-crm','invalid_payload')

    def process(self):
        try:
            for _ in range(100):
                if self.stop_event.is_set() or not self.store.process_one():
                    break
        except Exception:
            self.store.failure('helio','helio-crm','processing_failed',processing=True)

    def run_once(self):
        self.recover()
        self.process()

    def start(self):
        self.thread = threading.Thread(target=self._loop,name='activity-worker',daemon=True)
        self.thread.start()

    def _loop(self):
        next_poll = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_poll:
                    self.recover()
                    next_poll = time.monotonic()+5
                self.process()
            except Exception:
                log.error('Activity worker storage unavailable')
            self.stop_event.wait(0.25)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(15)
            if self.thread.is_alive():
                raise RuntimeError('Activity worker did not stop')

    @classmethod
    def from_environment(cls, store):
        enabled = os.getenv('AWASI_ACTIVITY_WORKER_ENABLED','0')
        if enabled not in {'0','1'}:
            raise ValueError('Invalid activity worker enablement')
        if enabled == '0':
            return None
        return cls(store,RecoveryClient(os.getenv('CRM_URL','http://127.0.0.1:8001'),os.getenv('CRM_RECOVERY_TOKEN','')))
