"""Independent, bounded webhook admission. No graph or model dependency."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Union

from fastapi import Query, Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from starlette.concurrency import run_in_threadpool

UUID = Annotated[str, Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
ID = Annotated[str, Field(min_length=1, max_length=200)]
Count = Annotated[int, Field(ge=0, le=2**53)]


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    event_id: UUID
    request_id: UUID
    source_instance_id: UUID
    company_id: ID
    source: ID
    stream: Literal['security']
    sequence: Annotated[int, Field(ge=1, le=2**53)]
    occurred_at: Annotated[str, Field(max_length=40)]
    actor_id: ID | None
    actor_kind: Literal['agent','employee','service'] | None
    owner_employee_id: ID | None

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError('Integer version required')
        return value

    @field_validator('occurred_at')
    @classmethod
    def timestamp(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('Timezone required')
        return value


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    rule: Literal['resource_permissions','owner_status','credential_lifecycle','extraction_limits','authentication_failures','sensitive_content']
    version: Literal[1]
    incident_key: UUID | None = None
    mode: Literal['enforce','observe','off']
    boundary: Literal['request','pre_commit','pre_release']
    result: Literal['allow','deny','would_deny','unknown']
    reason: Annotated[str, Field(pattern=r'^[a-z_]{1,80}$')]


class Operation(Envelope):
    event_type: Literal['operation.completed']
    policy_decisions: Annotated[list[PolicyDecision], Field(max_length=20)] = Field(default_factory=list)
    action: Literal['access:change','identity:publish','customers:read','customers:write','grants:revoke','accounts:read','grants:read','inventory:read','audit:read','activity:replay','events:read']
    resource_id: Literal['customer-database','crm-metadata','crm-audit','crm-activity','crm-identity']
    record_id: ID | None
    outcome: Literal['allowed','denied','failed']
    status_code: Annotated[int, Field(ge=100, le=599)]
    reason_code: Literal['operation_completed','authentication_failed','access_denied','resource_not_found','version_conflict','invalid_operation','operation_failed']
    result_record_count: Count | None
    prepared_record_count: Count = 0
    record_ids: Annotated[list[ID], Field(max_length=50)] = Field(default_factory=list)
    changed_fields: Annotated[list[Literal['notes','subscription_status','status','principal_status','credential_status','expires_at']], Field(max_length=3)]
    before_version: Count | None
    after_version: Count | None


class HTTPEvent(Envelope):
    method: Literal['GET','HEAD','POST','PATCH','PUT','DELETE','OPTIONS','OTHER']
    route: Literal['/customers','/customers/{id}','/grants/{id}/revoke','/accounts','/grants','/inventory','/audit','/activity/status','/activity/events','/identity-projection','/principals/{id}/access','unmatched']


class Received(HTTPEvent):
    event_type: Literal['request.received']


class Prepared(HTTPEvent):
    event_type: Literal['response.prepared']
    status_code: Annotated[int, Field(ge=100, le=599)]
    serialized_bytes: Count
    result_record_count: Count | None
    classification: Literal['internal','confidential']
    scan_status: Literal['not_run','passed','matched','unknown']
    release_decision: Literal['allow','deny']


class Completed(HTTPEvent):
    event_type: Literal['response.completed']
    status_code: Annotated[int, Field(ge=100, le=599)] | None
    emitted_bytes: Count
    duration_ms: Count
    completion: Literal['completed','interrupted']


EVENT = TypeAdapter(Annotated[Union[Operation, Received, Prepared, Completed], Field(discriminator='event_type')])


def sign(body, key_id, secret, timestamp, delivery_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key_id) or str(uuid.UUID(delivery_id)) != delivery_id:
        raise ValueError('Invalid delivery identity')
    message = f'awasi-v1\n{key_id}\n{delivery_id}\n{timestamp}\n'.encode() + body
    return {'X-Awasi-Key-Id': key_id, 'X-Awasi-Delivery-Id': delivery_id,
            'X-Awasi-Timestamp': str(timestamp), 'X-Awasi-Signature': 'v1='+hmac.new(secret.encode(),message,hashlib.sha256).hexdigest(),
            'Content-Type':'application/json'}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


class Inbox:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            with db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('''CREATE TABLE IF NOT EXISTS inbox (
                    company_id TEXT NOT NULL, source TEXT NOT NULL, source_instance_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    received_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status='pending'),
                    PRIMARY KEY(company_id,source,source_instance_id,event_id),
                    UNIQUE(company_id,source,source_instance_id,sequence))''')
        finally:
            db.close()

    def accept(self, event, now):
        payload = json.dumps(event,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        db = self.connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('''SELECT payload FROM inbox WHERE company_id=? AND source=? AND
                    source_instance_id=? AND (event_id=? OR sequence=?)''',
                    tuple(event[k] for k in ('company_id','source','source_instance_id','event_id','sequence'))).fetchone()
                if row:
                    if row['payload'] == payload:
                        self.accepted(db, event, now)
                    else:
                        self.conflicted(db, event, now)
                    return 200 if row['payload'] == payload else 409
                db.execute('''INSERT INTO inbox(company_id,source,source_instance_id,event_id,sequence,payload,received_at)
                    VALUES (?,?,?,?,?,?,?)''', (*[event[k] for k in ('company_id','source','source_instance_id','event_id','sequence')],payload,now))
                self.accepted(db, event, now)
            return 202
        finally:
            db.close()

    def accepted(self, db, event, now):
        """Transactional extension point for durable receipt checkpoints."""

    def conflicted(self, db, event, now):
        """Transactional extension point for evidence integrity failures."""


def create_app(path=None, keys=None, *, clock=time.time, read_tokens=None):
    from .activity_client import activity_query
    inbox = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal inbox
        from .activity_store import ActivityStore
        inbox = ActivityStore(path or os.getenv('AWASI_INBOX_DB_PATH','data/inbox.sqlite3'))
        app.state.inbox = inbox
        configured = keys
        if configured is None:
            key_id = os.getenv('CRM_WEBHOOK_KEY_ID', 'crm-1')
            configured = {key_id: {'secret': os.getenv('CRM_WEBHOOK_SECRET',''), 'company_id':'helio', 'source':'helio-crm'}}
            previous = os.getenv('CRM_WEBHOOK_PREVIOUS_SECRET')
            if previous:
                previous_id = os.getenv('CRM_WEBHOOK_PREVIOUS_KEY_ID', 'crm-previous')
                if previous_id == key_id:
                    raise ValueError('Invalid webhook connector configuration')
                configured[previous_id] = {'secret':previous,'company_id':'helio','source':'helio-crm'}
        if not configured or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or
                                 not isinstance(value.get('secret'),str) or len(value['secret'])<32 or
                                 not value.get('company_id') or value.get('source') != 'helio-crm'
                                 for key,value in configured.items()):
            raise ValueError('Invalid webhook connector configuration')
        app.state.keys = configured
        readers = read_tokens if read_tokens is not None else {
            role:os.getenv('SECURITY_'+role.upper()+'_TOKEN','') for role in ('viewer','operator')}
        if any(readers.values()) and (set(readers) != {'viewer','operator'} or
                any(not isinstance(v,str) or len(v)<32 for v in readers.values()) or len(set(readers.values()))!=2
                or any(v==key['secret'] for v in readers.values() for key in configured.values())):
            raise ValueError('Invalid activity read credentials')
        app.state.read_tokens = readers
        inbox.initialize()
        from .activity_worker import ActivityWorker
        worker = ActivityWorker.from_environment(inbox)
        app.state.worker = worker
        if worker:
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.inbox = inbox

    bearer = HTTPBearer(auto_error=False)

    def reader(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        supplied = credentials.credentials.encode() if credentials else b''
        if any(value and hmac.compare_digest(supplied,value.encode()) for value in app.state.read_tokens.values()):
            return 'helio'
        raise HTTPException(401, 'Valid activity read credential required', headers={'WWW-Authenticate':'Bearer'})

    @app.get('/activity')
    def list_activity(company=Depends(reader), query=Depends(activity_query)):
        return inbox.activity(company,**query)

    @app.get('/activity/policy-events')
    def policy_events(after: Annotated[int,Query(ge=0,le=2**53)]=0,
                      limit: Annotated[int,Query(ge=1,le=200)]=100,company=Depends(reader)):
        try:
            return inbox.policy_events(company,after=after,limit=limit)
        except ValueError:
            raise HTTPException(409,'Policy cursor regressed') from None

    @app.get('/activity/health')
    def activity_health(company=Depends(reader)):
        result = inbox.health(company,now=clock())
        worker = app.state.worker
        running = bool(worker and worker.thread and worker.thread.is_alive())
        return {**result,'worker_running':running,'coverage_complete':result['coverage_complete'] and running}

    @app.get('/health')
    def health():
        db = inbox.connect()
        try:
            db.execute('SELECT 1 FROM inbox LIMIT 1')
            return {'status':'ok'}
        finally:
            db.close()

    @app.exception_handler(sqlite3.Error)
    async def unavailable(request,error):
        return JSONResponse({'detail':'Activity storage unavailable'},503)

    @app.post('/webhooks/crm/activity')
    async def activity(request: Request):
        # Reject duplicate auth headers, ambiguous encodings and oversized chunked bodies.
        for name in ('x-awasi-key-id','x-awasi-delivery-id','x-awasi-timestamp','x-awasi-signature'):
            if len(request.headers.getlist(name)) != 1:
                return JSONResponse({'detail':'Invalid delivery authentication'},401)
        if request.headers.get('content-encoding','identity') != 'identity':
            return JSONResponse({'detail':'Unsupported encoding'},415)
        body = bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>65536:
                return JSONResponse({'detail':'Event too large'},413)
            body.extend(chunk)
        try:
            key_id = request.headers['x-awasi-key-id']
            connector = request.app.state.keys[key_id]
            timestamp = request.headers['x-awasi-timestamp']
            if not re.fullmatch(r'[0-9]{1,12}',timestamp) or abs(clock()-int(timestamp))>300:
                raise ValueError()
            expected = sign(bytes(body),key_id,connector['secret'],int(timestamp),request.headers['x-awasi-delivery-id'])
            supplied = request.headers['x-awasi-signature']
            if not re.fullmatch(r'v1=[0-9a-f]{64}',supplied) or not hmac.compare_digest(expected['X-Awasi-Signature'],supplied):
                raise ValueError()
        except (KeyError, ValueError):
            return JSONResponse({'detail':'Invalid delivery authentication'},401)
        try:
            data = json.loads(bytes(body), object_pairs_hook=unique_object)
            event = EVENT.validate_python(data).model_dump(exclude_unset=True)
        except (ValueError, TypeError, RecursionError):
            return JSONResponse({'detail':'Invalid activity event'},422)
        if event['company_id'] != connector['company_id'] or event['source'] != connector['source']:
            return JSONResponse({'detail':'Connector scope mismatch'},403)
        status = await run_in_threadpool(inbox.accept,event,clock())
        return JSONResponse({'status':{202:'accepted',200:'duplicate',409:'identity_conflict'}[status]}, status)

    return app


app = create_app()
