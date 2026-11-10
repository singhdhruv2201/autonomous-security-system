"""Durable normalization and explicit coverage; no policy or graph side effects."""

import json
import time
import uuid
from contextlib import contextmanager

from .webhooks import EVENT, Inbox

SCOPE = ('company_id', 'source', 'source_instance_id')
MATCH = 'company_id=? AND source=? AND source_instance_id=?'


class ActivityStore(Inbox):
    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    def initialize(self):
        super().initialize()
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO activity_metadata VALUES ('instance_id',?)",(str(uuid.uuid4()),))
            db.execute('''CREATE TABLE IF NOT EXISTS activity_worker_state (
                company_id TEXT NOT NULL, source TEXT NOT NULL, error TEXT, integrity_error TEXT,
                last_attempt REAL, PRIMARY KEY(company_id,source))''')
            db.execute('''CREATE TABLE IF NOT EXISTS activity_sources (
                company_id TEXT NOT NULL, source TEXT NOT NULL, source_instance_id TEXT NOT NULL,
                received_cursor INTEGER NOT NULL DEFAULT 0, processed_cursor INTEGER NOT NULL DEFAULT 0,
                high_watermark INTEGER, earliest_available INTEGER, capture_enabled INTEGER,
                checked_at REAL, queue TEXT, retained_events INTEGER, capacity INTEGER,
                error TEXT, processing_error TEXT,
                PRIMARY KEY(company_id,source,source_instance_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS activity_connectors (
                company_id TEXT NOT NULL, source TEXT NOT NULL, current_instance TEXT NOT NULL,
                resets INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(company_id,source))''')
            db.execute('''CREATE TABLE IF NOT EXISTS normalized_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, source TEXT NOT NULL,
                source_instance_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                request_id TEXT NOT NULL, payload TEXT NOT NULL, processed_at REAL NOT NULL,
                UNIQUE(company_id,source,source_instance_id,event_id),
                UNIQUE(company_id,source,source_instance_id,sequence))''')
            db.execute('CREATE INDEX IF NOT EXISTS activity_request ON normalized_events(company_id,source,source_instance_id,request_id,id)')
            # Upgrade old pending inboxes without reassigning identities or losing receipts.
            for row in db.execute('SELECT DISTINCT company_id,source,source_instance_id FROM inbox').fetchall():
                self.ensure_source(db, tuple(row))
                self.advance(db, tuple(row), 'inbox', 'received_cursor')

    @staticmethod
    def ensure_source(db, scope):
        db.execute('INSERT OR IGNORE INTO activity_sources(company_id,source,source_instance_id) VALUES (?,?,?)', scope)

    @staticmethod
    def advance(db, scope, table, column):
        cursor = db.execute(f'SELECT {column} FROM activity_sources WHERE {MATCH}', scope).fetchone()[0]
        for row in db.execute(f'SELECT sequence FROM {table} WHERE {MATCH} AND sequence>? ORDER BY sequence', (*scope, cursor)):
            if row[0] != cursor + 1:
                break
            cursor = row[0]
        db.execute(f'UPDATE activity_sources SET {column}=? WHERE {MATCH}', (cursor, *scope))

    def accepted(self, db, event, now):
        scope = tuple(event[k] for k in SCOPE)
        self.ensure_source(db, scope)
        self.advance(db, scope, 'inbox', 'received_cursor')

    def conflicted(self, db, event, now):
        db.execute('''INSERT INTO activity_worker_state(company_id,source,integrity_error,last_attempt)
            VALUES (?,?,'identity_conflict',?) ON CONFLICT(company_id,source)
            DO UPDATE SET integrity_error='identity_conflict',last_attempt=excluded.last_attempt''',
            (event['company_id'],event['source'],now))

    def observe(self, head, now):
        scope = tuple(head[k] for k in SCOPE)
        with self.transaction() as db:
            db.execute('''INSERT INTO activity_worker_state(company_id,source,last_attempt) VALUES (?,?,?)
                ON CONFLICT(company_id,source) DO UPDATE SET error=NULL,last_attempt=excluded.last_attempt''', (*scope[:2],now))
            self.ensure_source(db, scope)
            previous = db.execute('SELECT current_instance FROM activity_connectors WHERE company_id=? AND source=?', scope[:2]).fetchone()
            if previous is None:
                db.execute('INSERT INTO activity_connectors(company_id,source,current_instance) VALUES (?,?,?)', scope)
            elif previous[0] != scope[2]:
                db.execute('UPDATE activity_connectors SET current_instance=?,resets=resets+1 WHERE company_id=? AND source=?', (scope[2], *scope[:2]))
            old_high = db.execute(f'SELECT high_watermark FROM activity_sources WHERE {MATCH}', scope).fetchone()[0]
            error = 'source_regressed' if old_high is not None and head['high_watermark'] < old_high else None
            db.execute(f'''UPDATE activity_sources SET high_watermark=?,earliest_available=?,capture_enabled=?,
                checked_at=?,queue=?,retained_events=?,capacity=?,error=? WHERE {MATCH}''',
                (max(old_high or 0, head['high_watermark']), head['earliest_available'], int(head['capture_enabled']),
                 now, json.dumps(head['queue']), head['retained_events'], head['capacity'], error, *scope))

    def failure(self, company, source, error, *, processing=False):
        column = 'processing_error' if processing else 'error'
        with self.transaction() as db:
            db.execute(f'UPDATE activity_sources SET {column}=? WHERE company_id=? AND source=?', (error, company, source))
            if not processing:
                db.execute('''INSERT INTO activity_worker_state(company_id,source,error,last_attempt) VALUES (?,?,?,?)
                    ON CONFLICT(company_id,source) DO UPDATE SET error=excluded.error,last_attempt=excluded.last_attempt''',
                    (company,source,error,time.time()))

    def received_cursor(self, head):
        with self.transaction() as db:
            return db.execute(f'SELECT received_cursor FROM activity_sources WHERE {MATCH}', tuple(head[k] for k in SCOPE)).fetchone()[0]

    def process_one(self):
        with self.transaction() as db:
            row = db.execute('''SELECT i.* FROM inbox i WHERE NOT EXISTS (
                SELECT 1 FROM normalized_events n WHERE n.company_id=i.company_id AND n.source=i.source
                AND n.source_instance_id=i.source_instance_id AND n.event_id=i.event_id)
                ORDER BY i.received_at,i.sequence LIMIT 1''').fetchone()
            if row is None:
                return False
            event = EVENT.validate_python(json.loads(row['payload'])).model_dump()
            scope = tuple(event[k] for k in SCOPE)
            db.execute('''INSERT INTO normalized_events(company_id,source,source_instance_id,event_id,sequence,
                       request_id,payload,processed_at) VALUES (?,?,?,?,?,?,?,?)''',
                       (*scope, event['event_id'], event['sequence'], event['request_id'], json.dumps(event), time.time()))
            self.advance(db, scope, 'normalized_events', 'processed_cursor')
            db.execute(f'UPDATE activity_sources SET processing_error=NULL WHERE {MATCH}', scope)
        return True

    def health(self, company, *, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            sources = []
            failures = db.execute('SELECT error,integrity_error FROM activity_worker_state WHERE company_id=?', (company,)).fetchall()
            recovery_error = next((r['integrity_error'] or r['error'] for r in failures if r['integrity_error'] or r['error']), None)
            connectors = db.execute('SELECT * FROM activity_connectors WHERE company_id=?', (company,)).fetchall()
            current = {r['source']:r['current_instance'] for r in connectors}
            resets = sum(r['resets'] for r in connectors)
            for row in db.execute('SELECT * FROM activity_sources WHERE company_id=? ORDER BY source,source_instance_id', (company,)).fetchall():
                item = dict(row)
                scope = tuple(item[k] for k in SCOPE)
                receipts = db.execute(f'SELECT sequence,received_at,payload FROM inbox WHERE {MATCH} ORDER BY sequence', scope).fetchall()
                horizon = max(item['high_watermark'] or 0, receipts[-1]['sequence'] if receipts else 0)
                cursor, gaps, missing = 0, [], 0
                for sequence in [r['sequence'] for r in receipts] + [horizon + 1]:
                    if sequence > cursor + 1:
                        missing += sequence - cursor - 1
                        if len(gaps) < 20:
                            gaps.append([cursor + 1, sequence - 1])
                    cursor = sequence
                pending = db.execute(f'''SELECT count(*),min(received_at) FROM inbox i WHERE {MATCH} AND NOT EXISTS
                    (SELECT 1 FROM normalized_events n WHERE n.company_id=i.company_id AND n.source=i.source
                     AND n.source_instance_id=i.source_instance_id AND n.event_id=i.event_id)''', scope).fetchone()
                item.update(active=current.get(item['source']) == item['source_instance_id'],
                            stale=item['checked_at'] is None or now-item['checked_at'] > 30,
                            queue=json.loads(item['queue']) if item['queue'] else None,
                            missing_ranges=gaps, missing_events=missing, received_events=len(receipts),
                            pending_processing=pending[0], oldest_pending_seconds=max(0,now-pending[1]) if pending[1] else None,
                            receipt_lag_events=max(0,horizon-item['received_cursor']),
                            processing_lag_events=max(0,horizon-item['processed_cursor']),
                            history_expired=item['earliest_available'] is not None and item['earliest_available'] > item['received_cursor']+1)
                item['coverage_complete'] = bool(item['active'] and not item['stale'] and item['capture_enabled']
                    and not item['error'] and not item['processing_error'] and not missing
                    and item['processed_cursor'] >= horizon and not item['history_expired'])
                sources.append(item)
            return {'company_id':company, 'enabled':True, 'sources':sources, 'source_resets':resets,
                    'coverage_complete':bool(sources) and not resets and not recovery_error and all(s['coverage_complete'] for s in sources),
                    'recovery_error':recovery_error,
                    'scanners':'per_request', 'policies_enabled':None}

    def activity(self, company, *, limit=50, before=None, snapshot=None, actor_id=None, outcome=None):
        with self.transaction() as db:
            high = db.execute('SELECT coalesce(max(id),0) FROM normalized_events WHERE company_id=?', (company,)).fetchone()[0]
            snapshot = high if snapshot is None else min(snapshot, high)
            params = [company, snapshot]
            having = []
            if actor_id:
                having.append("max(json_extract(payload,'$.actor_id')=?)")
                params.append(actor_id)
            if outcome:
                having.append("max(json_extract(payload,'$.outcome')=?)")
                params.append(outcome)
            grouped = '''SELECT source,source_instance_id,request_id,min(id) AS first_id FROM normalized_events
                         WHERE company_id=? AND id<=? GROUP BY source,source_instance_id,request_id'''
            if having:
                grouped += ' HAVING ' + ' AND '.join(having)
            total = db.execute('SELECT count(*) FROM ('+grouped+')', params).fetchone()[0]
            query = 'SELECT * FROM ('+grouped+')'
            if before is not None:
                query += ' WHERE first_id<?'
                params.append(before)
            groups = db.execute(query+' ORDER BY first_id DESC LIMIT ?', (*params, limit+1)).fetchall()
            items = []
            for group in groups[:limit]:
                stages = [json.loads(r[0]) for r in db.execute('''SELECT payload FROM normalized_events
                    WHERE company_id=? AND source=? AND source_instance_id=? AND request_id=? AND id<=? ORDER BY sequence''',
                    (company, group['source'], group['source_instance_id'], group['request_id'], snapshot))]
                operation = next((s for s in stages if s['event_type']=='operation.completed'), {})
                prepared = next((s for s in stages if s['event_type']=='response.prepared'), {})
                completed = next((s for s in stages if s['event_type']=='response.completed'), {})
                items.append({**dict(group), 'occurred_at':stages[0]['occurred_at'],
                    'actor_id':next((s['actor_id'] for s in stages if s['actor_id']), None),
                    'action':operation.get('action', stages[0].get('route','unknown')),
                    'outcome':operation.get('outcome','unknown'), 'status_code':completed.get('status_code', prepared.get('status_code',operation.get('status_code'))),
                    'scan_status':prepared.get('scan_status','not_run'), 'completion':completed.get('completion','unknown'),
                    'stages':stages})
            return {'company_id':company, 'enabled':True, 'items':items, 'total':total, 'snapshot':snapshot,
                    'next_before':groups[limit-1]['first_id'] if len(groups)>limit else None}
