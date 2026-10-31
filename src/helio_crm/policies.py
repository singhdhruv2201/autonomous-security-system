"""Local deterministic policy support; no remote calls inside CRM transactions."""
import json
import os
import uuid
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class PolicyConfig:
    owner_mode: str = 'off'
    volume_mode: str = 'off'
    failure_mode: str = 'off'
    content_mode: str = 'off'
    scan_max_bytes: int = 65536
    scan_timeout_ms: int = 10
    failure_limit: int = 5
    cooldown_seconds: int = 60
    request_limit: int = 100
    record_limit: int = 100
    bulk_limit: int = 50
    window_seconds: int = 60

    def __post_init__(self):
        if any(mode not in {'off','observe','enforce'} for mode in (self.owner_mode,self.volume_mode,self.failure_mode,self.content_mode)):
            raise ValueError('Invalid policy mode')
        if any(type(value) is not int or not 1 <= value <= 100000 for value in
               (self.request_limit,self.record_limit,self.bulk_limit,self.window_seconds,self.failure_limit,self.cooldown_seconds,self.scan_max_bytes,self.scan_timeout_ms)):
            raise ValueError('Invalid policy limit')

    def view(self):
        return {'version':1,'modes':{'resource_permissions':'enforce','credential_lifecycle':'enforce',
                'owner_status':self.owner_mode,'extraction_limits':self.volume_mode,
                'authentication_failures':self.failure_mode,'sensitive_content':self.content_mode},
                'limits':{k:v for k,v in asdict(self).items() if not k.endswith('_mode')}}

    @classmethod
    def from_environment(cls):
        return cls(owner_mode=os.getenv('CRM_OWNER_MODE','off'),volume_mode=os.getenv('CRM_VOLUME_MODE','off'),
                   request_limit=int(os.getenv('CRM_REQUEST_LIMIT','100')),record_limit=int(os.getenv('CRM_RECORD_LIMIT','100')),
                   bulk_limit=int(os.getenv('CRM_BULK_LIMIT','50')),failure_mode=os.getenv('CRM_FAILURE_MODE','off'),
                   failure_limit=int(os.getenv('CRM_FAILURE_LIMIT','5')),cooldown_seconds=int(os.getenv('CRM_COOLDOWN_SECONDS','60')),content_mode=os.getenv('CRM_CONTENT_MODE','off'),
                   scan_max_bytes=int(os.getenv('CRM_SCAN_MAX_BYTES','65536')),scan_timeout_ms=int(os.getenv('CRM_SCAN_TIMEOUT_MS','10')))


def initialize(db):
    if 'app_id' not in {r[1] for r in db.execute('PRAGMA table_info(grants)')}:
        db.execute("ALTER TABLE grants ADD COLUMN app_id TEXT NOT NULL DEFAULT 'helio-crm'")
    db.execute('''CREATE TABLE IF NOT EXISTS policy_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, actor_id TEXT,
        resource_id TEXT NOT NULL, occurred_at REAL NOT NULL, rule TEXT NOT NULL,
        version INTEGER NOT NULL, mode TEXT NOT NULL, boundary TEXT NOT NULL,
        result TEXT NOT NULL, reason TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS budget_events (request_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
        occurred_at REAL NOT NULL, attempted INTEGER NOT NULL, prepared INTEGER NOT NULL, reserved INTEGER NOT NULL,
        released INTEGER NOT NULL DEFAULT 0)''')
    db.execute('CREATE TABLE IF NOT EXISTS cooldowns (actor_key TEXT PRIMARY KEY, until_at REAL NOT NULL)')
    if 'incident_key' not in {r[1] for r in db.execute('PRAGMA table_info(cooldowns)')}:
        db.execute('ALTER TABLE cooldowns ADD COLUMN incident_key TEXT')
    for row in db.execute('SELECT actor_key FROM cooldowns WHERE incident_key IS NULL').fetchall():
        db.execute('UPDATE cooldowns SET incident_key=? WHERE actor_key=?',(str(uuid.uuid4()),row[0]))
    db.execute('CREATE INDEX IF NOT EXISTS budget_actor_time ON budget_events(actor_id,occurred_at)')
    db.execute("CREATE TABLE IF NOT EXISTS access_state (principal_id TEXT PRIMARY KEY REFERENCES principals(id), principal_status TEXT NOT NULL CHECK(principal_status IN ('active','disabled')), credential_status TEXT NOT NULL CHECK(credential_status IN ('active','revoked')), expires_at REAL, version INTEGER NOT NULL)")
    db.execute('CREATE TABLE IF NOT EXISTS identity_projection (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, observed_at REAL NOT NULL, expires_at REAL NOT NULL, employees TEXT NOT NULL)')
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('policy_schema_version','1')")


def decision(rule, result, reason, *, mode='enforce', boundary='request', incident_key=None):
    result = dict(rule=rule, version=1, mode=mode, boundary=boundary, result=result, reason=reason)
    if incident_key is not None: result['incident_key']=incident_key
    return result


def persist(db, decisions, request_id, actor, resource, now):
    db.executemany('''INSERT INTO policy_decisions
        (request_id,actor_id,resource_id,occurred_at,rule,version,mode,boundary,result,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        [(request_id,actor,resource,now,d['rule'],d['version'],d['mode'],d['boundary'],d['result'],d['reason']) for d in decisions])


def owner_check(db, principal, config, now):
    if config.owner_mode == 'off':
        return None
    row = db.execute('SELECT * FROM identity_projection WHERE id=1').fetchone()
    if row is None or not row['observed_at'] <= now < row['expires_at']:
        return decision('owner_status','unknown','identity_unavailable',mode=config.owner_mode)
    status = json.loads(row['employees']).get(principal['owner_employee_id'])
    if status is None:
        return decision('owner_status','unknown','identity_unavailable',mode=config.owner_mode)
    return decision('owner_status','allow' if status == 'active' else 'deny' if config.owner_mode == 'enforce' else 'would_deny',
                    'owner_active' if status == 'active' else 'owner_inactive',mode=config.owner_mode)


def budget_check(db, actor, count, config, now):
    last = db.execute("SELECT value FROM metadata WHERE key='budget_clock'").fetchone()
    if last and now < float(last[0]):
        return decision('extraction_limits','unknown','clock_regressed',mode=config.volume_mode)
    requests,records = db.execute('SELECT coalesce(sum(attempted),0),coalesce(sum(reserved),0) FROM budget_events WHERE actor_id=? AND occurred_at>?',
                                 (actor,now-config.window_seconds)).fetchone()
    reason = ('request_budget' if requests+1 > config.request_limit else
              'bulk_result' if count is not None and count >= config.bulk_limit else
              'record_budget' if count is not None and records+count > config.record_limit else 'within_budget')
    return decision('extraction_limits', 'allow' if reason=='within_budget' else 'deny' if config.volume_mode=='enforce' else 'would_deny',
                    reason,mode=config.volume_mode,boundary='request' if count is None else 'pre_release')


def record_budget(db, request_id, actor, prepared, allowed, config, now):
    last = db.execute("SELECT value FROM metadata WHERE key='budget_clock'").fetchone()
    if last and now < float(last[0]):
        return
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('budget_clock',?)",(str(now),))
    db.execute('DELETE FROM budget_events WHERE occurred_at<=?',(now-config.window_seconds,))
    db.execute('INSERT INTO budget_events VALUES (?,?,?,?,?,?,0)',(request_id,actor,now,1,prepared,prepared if allowed else 0))


def failure_check(db, key, config, now):
    last = db.execute("SELECT value FROM metadata WHERE key='budget_clock'").fetchone()
    if last and now < float(last[0]):
        return decision('authentication_failures','unknown','clock_regressed',mode=config.failure_mode)
    row = db.execute('SELECT until_at,incident_key FROM cooldowns WHERE actor_key=?',(key,)).fetchone()
    if row and now < row[0]:
        return decision('authentication_failures','deny' if config.failure_mode=='enforce' else 'would_deny',
                        'access_cooldown',mode=config.failure_mode,incident_key=row['incident_key'])
    return None


def record_failure(db, key, request_id, failed, authenticated, config, now):
    last = db.execute("SELECT value FROM metadata WHERE key='budget_clock'").fetchone()
    if last and now < float(last[0]): return None
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('budget_clock',?)",(str(now),))
    db.execute('DELETE FROM budget_events WHERE occurred_at<=?',(now-config.window_seconds,))
    cooldown = db.execute('SELECT until_at FROM cooldowns WHERE actor_key=?',(key,)).fetchone()
    if cooldown and now >= cooldown[0]:
        db.execute('DELETE FROM budget_events WHERE actor_id=?',(key,))
        db.execute('DELETE FROM cooldowns WHERE actor_key=?',(key,))
    elif cooldown:
        return None  # Requests during cooldown never extend it.
    if authenticated:
        db.execute('DELETE FROM budget_events WHERE actor_id=?',(key,))
    if not failed: return None
    db.execute('INSERT INTO budget_events VALUES (?,?,?,1,0,0,0)',(request_id,key,now))
    count=db.execute('SELECT coalesce(sum(attempted),0) FROM budget_events WHERE actor_id=? AND occurred_at>?',
                     (key,now-config.window_seconds)).fetchone()[0]
    if count >= config.failure_limit:
        incident_key=str(uuid.uuid4())
        db.execute('INSERT OR REPLACE INTO cooldowns VALUES (?,?,?)',(key,now+config.cooldown_seconds,incident_key))
        return decision('authentication_failures','deny' if config.failure_mode=='enforce' else 'would_deny',
                        'failure_threshold',mode=config.failure_mode,incident_key=incident_key)
    return None


def content_checks(result, config, boundary):
    from .scanner import scan
    scanned=scan(json.dumps(result,ensure_ascii=False,separators=(',',':')),
                 max_bytes=config.scan_max_bytes,timeout_ms=config.scan_timeout_ms)
    if scanned['status']=='unknown':
        return [decision('sensitive_content','unknown','scan_incomplete',mode=config.content_mode,boundary=boundary)]
    if not scanned['detectors']:
        return [decision('sensitive_content','allow','scan_passed',mode=config.content_mode,boundary=boundary)]
    return [decision('sensitive_content','deny' if config.content_mode=='enforce' and name!='pii_ssn' else 'would_deny',
                     name,mode='observe' if name=='pii_ssn' else config.content_mode,boundary=boundary)
            for name in scanned['detectors']]
