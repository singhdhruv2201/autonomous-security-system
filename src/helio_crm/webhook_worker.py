"""Durable leased delivery. Network I/O never holds a CRM transaction."""
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.utils import parsedate_to_datetime

from helio_security.webhooks import sign

log = logging.getLogger(__name__)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=5) as response:
            return response.status, response.headers.get('Retry-After')
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers.get('Retry-After')
    except (OSError, urllib.error.URLError):
        return 0, None


def validate_destination(url, allow_local):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path != '/webhooks/crm/activity' or not parsed.hostname):
            raise ValueError()
        if parsed.scheme == 'http':
            if not allow_local or parsed.hostname not in {'127.0.0.1','localhost','awasi-ingest'}:
                raise ValueError()
        elif parsed.scheme != 'https':
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid fixed webhook destination') from None


class DeliveryWorker:
    def __init__(self, store, url, key_id, secret, *, allow_local=False, clock=time.time,
                 transport=deliver, max_attempts=5, jitter=random.random):
        validate_destination(url, allow_local)
        if not isinstance(secret,str) or len(secret)<32 or type(max_attempts) is not int or not 1<=max_attempts<=20:
            raise ValueError('Invalid webhook delivery configuration')
        sign(b'',key_id,secret,0,str(uuid.uuid4()))
        self.store,self.url,self.key_id,self.secret = store,url,key_id,secret
        self.clock,self.transport,self.max_attempts,self.jitter = clock,transport,max_attempts,jitter
        self.stop_event = threading.Event()
        self.thread = None

    def claim(self):
        now = self.clock()
        with self.store._operation_connection() as db:
            db.execute("""UPDATE activity_outbox SET status='dead_letter',lease_id=NULL,lease_until=NULL,
                last_error='attempts_exhausted' WHERE status='inflight' AND lease_until<=? AND attempts>=?""",(now,self.max_attempts))
            row = db.execute("""SELECT * FROM activity_outbox WHERE
                (status='pending' AND next_attempt_at<=?) OR (status='inflight' AND lease_until<=?)
                ORDER BY event_sequence LIMIT 1""",(now,now)).fetchone()
            if row is None: return None
            lease = str(uuid.uuid4())
            db.execute("""UPDATE activity_outbox SET status='inflight',lease_id=?,lease_until=?,attempts=attempts+1
                WHERE event_sequence=?""",(lease,now+60,row['event_sequence']))
            event = dict(db.execute('SELECT * FROM activity_events WHERE sequence=?',(row['event_sequence'],)).fetchone())
            payload = json.loads(event.pop('payload'))
            return {**dict(row),'lease_id':lease,'attempts':row['attempts']+1,'event':{**payload,**event}}

    def finish(self, claim, status, retry_after):
        now = self.clock()
        success = status in {200,202}
        retry = status == 0 or status == 429 or 500<=status<=599
        state = 'delivered' if success else 'pending' if retry and claim['attempts']<self.max_attempts else 'dead_letter'
        delay = min(300, 2**claim['attempts'] + self.jitter())
        if retry_after and retry:
            try:
                seconds = int(retry_after) if str(retry_after).isdigit() else parsedate_to_datetime(retry_after).timestamp()-now
                delay = max(delay,min(300,max(0,seconds)))
            except (ValueError,TypeError,OverflowError):
                pass
        with self.store._operation_connection() as db:
            updated = db.execute("""UPDATE activity_outbox SET status=?,lease_id=NULL,lease_until=NULL,
                next_attempt_at=?,last_status=?,last_error=? WHERE event_sequence=? AND status='inflight' AND lease_id=?""",
                (state,now+delay,status,None if success else 'delivery_failed',claim['event_sequence'],claim['lease_id'])).rowcount
            if updated:
                db.execute('INSERT INTO delivery_audit(event_sequence,occurred_at,status_code,outcome,actor_id) VALUES (?,?,?,?,?)',
                           (claim['event_sequence'],now,status,state,'service:crm-webhook'))
                db.execute('DELETE FROM delivery_audit WHERE id NOT IN (SELECT id FROM delivery_audit ORDER BY id DESC LIMIT 1000)')

    def run_once(self):
        claim = self.claim()
        if claim is None: return False
        body = json.dumps(claim['event'],sort_keys=True,separators=(',',':')).encode()
        if len(body)>65536:
            self.finish(claim,413,None)
            return True
        headers = sign(body,self.key_id,self.secret,int(self.clock()),str(uuid.uuid4()))
        try:
            status,retry_after = self.transport(self.url,body,headers)
        except Exception:
            status,retry_after = 0,None
        self.finish(claim,status,retry_after)
        return True

    def start(self):
        self.thread = threading.Thread(target=self._loop,name='crm-webhook',daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception:
                log.error('CRM webhook worker storage unavailable')
            # Poll when idle; ready events already have durable retry/lease bounds.
            if not worked:
                self.stop_event.wait(0.25)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive(): raise RuntimeError('Webhook worker did not stop')

    @classmethod
    def from_environment(cls,store):
        enabled = os.getenv('CRM_WEBHOOK_ENABLED','0')
        local = os.getenv('CRM_WEBHOOK_ALLOW_LOCAL','0')
        if enabled not in {'0','1'} or local not in {'0','1'}:
            raise ValueError('Invalid webhook enablement configuration')
        if enabled == '0': return None
        if not store.activity_enabled:
            raise ValueError('Webhook delivery requires activity capture')
        return cls(store,os.getenv('CRM_WEBHOOK_URL',''),os.getenv('CRM_WEBHOOK_KEY_ID','crm-1'),
                   os.getenv('CRM_WEBHOOK_SECRET',''),allow_local=local=='1')
