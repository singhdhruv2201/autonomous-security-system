"""Durable Slack outbox; unknown post outcomes are never blindly retried."""
import json


class SlackStore:
    def __init__(self, cases):
        self.cases = cases
        with cases.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS slack_messages (
                id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                key TEXT NOT NULL, channel TEXT NOT NULL, ts TEXT, body TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, delivered INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', retry_at REAL NOT NULL DEFAULT 0,
                UNIQUE(case_id,key))''')

            if 'payload' not in {r['name'] for r in db.execute('PRAGMA table_info(slack_messages)')}:
                db.execute("ALTER TABLE slack_messages ADD COLUMN payload TEXT")

    def recover(self):
        with self.cases.connection() as db:
            db.execute("UPDATE slack_messages SET status=CASE WHEN ts IS NULL THEN 'unknown' ELSE 'queued' END WHERE status='sending'")

    def enqueue(self, case_id, key, channel, body, payload=None):
        encoded = json.dumps(body, sort_keys=True)
        context = json.dumps(payload, sort_keys=True) if payload is not None else None
        with self.cases.connection() as db:
            db.execute('''INSERT INTO slack_messages(case_id,key,channel,body,payload) VALUES (?,?,?,?,?)
                ON CONFLICT(case_id,key) DO UPDATE SET body=excluded.body,payload=coalesce(excluded.payload,slack_messages.payload),revision=revision+1,
                status=CASE WHEN slack_messages.status IN ('unknown','sending') THEN slack_messages.status ELSE 'queued' END,
                retry_at=0 WHERE slack_messages.body != excluded.body OR (excluded.payload IS NOT NULL AND coalesce(slack_messages.payload,'') != excluded.payload)''', (case_id, key, channel, encoded, context))

    def message(self, case_id, key):
        with self.cases.connection() as db:
            row = db.execute('SELECT * FROM slack_messages WHERE case_id=? AND key=?', (case_id,key)).fetchone()
            return dict(row) if row else None

    def root(self, case_id):
        with self.cases.connection() as db:
            row = db.execute('SELECT * FROM slack_messages WHERE case_id=? ORDER BY id LIMIT 1', (case_id,)).fetchone()
            return dict(row) if row else None

    def pending(self, now):
        with self.cases.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM slack_messages WHERE status='queued' AND retry_at<=? ORDER BY id LIMIT 50", (now,))]

    def claim(self, row):
        with self.cases.connection() as db:
            return db.execute("UPDATE slack_messages SET status='sending' WHERE id=? AND revision=? AND status='queued'",
                              (row['id'], row['revision'])).rowcount == 1

    def sent(self, row, ts):
        with self.cases.connection() as db:
            db.execute("UPDATE slack_messages SET ts=?,delivered=?,status=CASE WHEN revision=? THEN 'sent' ELSE 'queued' END WHERE id=?",
                       (ts,row['revision'],row['revision'],row['id']))

    def failed(self, row, *, uncertain, retry_at):
        with self.cases.connection() as db:
            db.execute('UPDATE slack_messages SET status=?,retry_at=? WHERE id=?',
                       ('unknown' if uncertain else 'queued',retry_at,row['id']))

    def status(self):
        with self.cases.connection() as db:
            counts = {r['status']:r['n'] for r in db.execute('SELECT status,count(*) AS n FROM slack_messages GROUP BY status')}
            return {'pending':counts.get('queued',0)+counts.get('sending',0), 'unknown':counts.get('unknown',0)}

    def questions_to_refresh(self):
        with self.cases.connection() as db:
            return [dict(r) for r in db.execute("SELECT m.* FROM slack_messages m JOIN cases c ON c.id=m.case_id "
                "WHERE m.key!='outcome' AND (c.status NOT IN ('waiting_owner','waiting_approver') OR EXISTS "
                "(SELECT 1 FROM case_decisions d WHERE d.case_id=m.case_id AND d.key=m.key))")]
