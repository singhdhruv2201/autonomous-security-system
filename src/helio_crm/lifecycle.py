"""Bounded ASGI response capture. Persist evidence before releasing response bytes."""
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import activity

context = ContextVar('crm_request', default=None)
log = logging.getLogger(__name__)
PUBLIC = {'/', '/health', '/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json'}
METADATA = {'/accounts', '/grants', '/inventory', '/audit', '/activity/status', '/activity/events', '/identity-projection'}


def classify(path):
    if path in PUBLIC | METADATA | {'/customers'}:
        return path
    if re.fullmatch(r'/customers/[^/]+', path):
        return '/customers/{id}'
    if re.fullmatch(r'/grants/[^/]+/revoke', path):
        return '/grants/{id}/revoke'
    if re.fullmatch(r'/principals/[^/]+/access', path):
        return '/principals/{id}/access'
    return 'unmatched'


class ActivityMiddleware:
    def __init__(self, app, store, max_response_bytes=2 * 1024 * 1024, response_check=None):
        self.app, self.store = app, store
        self.max_response_bytes = max_response_bytes
        self.response_check = response_check

    def record(self, ctx, kind, **fields):
        payload = {'schema_version': 1, 'event_type': kind, 'company_id': 'helio',
                   'source': 'helio-crm', 'stream': ctx['stream'], 'method': ctx['method'],
                   'route': ctx['route'], 'actor_id': ctx.get('actor_id'),
                   'actor_kind': ctx.get('actor_kind'), 'owner_employee_id': ctx.get('owner_employee_id'),
                   **fields}
        with self.store._operation_connection() as db:
            payload['source_instance_id'] = db.execute("SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()[0]
            if kind == 'response.completed' and fields.get('completion') == 'completed' and fields.get('status_code',500) < 400:
                db.execute('UPDATE budget_events SET released=reserved WHERE request_id=?',(ctx['request_id'],))
            activity.persist(db, payload, ctx['request_id'], datetime.now(timezone.utc).isoformat(), self.store.activity_limit)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not self.store.activity_enabled:
            return await self.app(scope, receive, send)
        route = classify(scope['path'])
        ctx = {'request_id': str(uuid.uuid4()), 'route': route,
               'method': scope['method'] if scope['method'] in {'GET','HEAD','POST','PATCH','PUT','DELETE','OPTIONS'} else 'OTHER',
               'stream': 'operational' if route in PUBLIC | METADATA else 'security'}
        token = context.set(ctx)
        started, emitted, headers_sent = time.monotonic(), 0, False
        start, parts, size, body_complete = None, [], 0, False

        async def capture(message):
            nonlocal start, size, body_complete
            if message['type'] == 'http.response.start':
                start = dict(message)
            elif message['type'] == 'http.response.body':
                if body_complete or start is None:
                    raise RuntimeError()
                size += len(message.get('body', b''))
                if size > self.max_response_bytes:
                    raise OverflowError()
                parts.append(message.get('body', b''))
                body_complete = not message.get('more_body', False)

        try:
            try:
                await run_in_threadpool(self.record, ctx, 'request.received')
            except Exception:
                await JSONResponse({'detail': 'CRM storage is temporarily unavailable'}, 503)(scope, receive, send)
                return
            try:
                await self.app(scope, receive, capture)
                if start is None or not body_complete:
                    raise RuntimeError()
            except Exception as error:
                status = 503 if isinstance(error, OverflowError) else 500
                fallback = JSONResponse({'detail': 'Response unavailable' if status == 503 else 'Operation failed'}, status)
                parts = [fallback.body]
                start = {'type':'http.response.start','status':status,'headers':fallback.raw_headers}
            body = b''.join(parts)
            status = start['status']
            if status >= 400 and route not in PUBLIC:
                ctx['stream'] = 'security'
            check = {'scan_status': ctx.get('scan_status','not_run'), 'release_decision': 'allow'}
            try:
                if self.response_check:
                    check = await run_in_threadpool(self.response_check, body, status)
                    if check not in ({'scan_status':'passed','release_decision':'allow'},
                                     {'scan_status':'matched','release_decision':'deny'}):
                        raise ValueError()
                if check['release_decision'] == 'deny':
                    response = JSONResponse({'detail':'Response blocked'}, 403)
                    body, start, status = response.body, {'type':'http.response.start','status':403,'headers':response.raw_headers}, 403
                await run_in_threadpool(self.record, ctx, 'response.prepared', status_code=status,
                                        serialized_bytes=len(body), result_record_count=ctx.get('result_record_count') if status < 400 else None,
                                        classification='confidential' if route.startswith('/customers') else 'internal', **check)
            except Exception:
                # No bytes have escaped. Preserve any previously committed operation.
                await JSONResponse({'detail':'CRM storage or response check is unavailable'}, 503)(scope, receive, send)
                return
            completion = 'completed'
            try:
                await send(start)
                headers_sent = True
                await send({'type':'http.response.body','body':body,'more_body':False})
                emitted = len(body)
            except BaseException:
                completion = 'interrupted'
                raise
            finally:
                try:
                    await run_in_threadpool(self.record, ctx, 'response.completed', status_code=status if headers_sent else None,
                                            emitted_bytes=emitted, duration_ms=max(0, int((time.monotonic()-started)*1000)),
                                            completion=completion)
                except Exception:
                    log.error('CRM response completion audit unavailable')
        finally:
            context.reset(token)
