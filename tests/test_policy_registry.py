"""Rule isolation and historical incident semantics on the real platform database."""
import json
import uuid

import pytest

from helio_security.platform_store import PlatformStore
from helio_security.rules import RULE_ID
from test_platform_store import evaluation, finish


def test_registry_is_versioned_and_unimplemented_rules_are_not_enabled():
    from helio_security.policies import POLICIES
    assert POLICIES[RULE_ID].version == 1
    assert POLICIES[RULE_ID].lifecycle == 'posture'
    assert all(not p.enabled for p in POLICIES.values() if not p.implemented)


def test_rules_have_independent_watermarks_and_reconciliation(tmp_path):
    store = PlatformStore(tmp_path / 'platform.db')
    store.initialize()
    finish(store, evaluation(200), 200)
    original = store.findings('helio', now=200)['items'][0]
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(['helio', RULE_ID, 'maya', 'sales', 'grant-sales', 'customers'])))
    assert original['id'] == expected
    incident = {**evaluation(100), 'rule': 'resource_permissions'}
    finish(store, incident, 201)
    assert store.findings('helio', now=201)['total'] == 2
    finish(store, {**incident, 'observed_at': 101, 'candidates': []}, 202)
    items = {f['rule']: f for f in store.findings('helio', now=10000)['items']}
    assert items[RULE_ID]['assessment'] == 'unknown'  # stale posture
    assert items['resource_permissions']['assessment'] == 'current'  # durable incident
    assert items['resource_permissions']['title'] != items[RULE_ID]['title']
    store.initialize()
    assert store.finding('helio', expected, now=201)['event_count'] == 1


def test_unknown_policy_rejected_atomically(tmp_path):
    store = PlatformStore(tmp_path / 'platform.db')
    store.initialize()
    with pytest.raises(ValueError, match='policy'):
        finish(store, {**evaluation(), 'rule': 'invented'})
    assert store.findings('helio')['total'] == 0


def test_legacy_single_rule_migration_retains_identity_evidence_and_watermark(tmp_path):
    import sqlite3
    path=tmp_path/'legacy.db'
    legacy_id=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(['helio',RULE_ID,'maya','sales','grant-sales','customers'])))
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version','1');
            CREATE TABLE runs (id TEXT PRIMARY KEY,company_id TEXT NOT NULL,actor TEXT NOT NULL,trigger TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,started_at REAL,finished_at REAL,source_results TEXT NOT NULL DEFAULT '{}',error TEXT);
            INSERT INTO runs VALUES ('old-run','helio','operator','manual','succeeded',100,100,100,'{}',NULL);
            CREATE TABLE findings (id TEXT PRIMARY KEY,company_id TEXT NOT NULL,rule TEXT NOT NULL,severity TEXT NOT NULL,status TEXT NOT NULL,assessment TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,last_evaluated REAL NOT NULL,last_verified REAL NOT NULL,resolved_at REAL,evidence TEXT NOT NULL,reason TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,company_id TEXT NOT NULL,finding_id TEXT NOT NULL REFERENCES findings(id),type TEXT NOT NULL,occurred_at REAL NOT NULL,run_id TEXT NOT NULL REFERENCES runs(id),reason TEXT NOT NULL,evidence TEXT NOT NULL);
            CREATE TABLE evaluations (company_id TEXT PRIMARY KEY,observed_at REAL NOT NULL);
            INSERT INTO evaluations VALUES ('helio',100);
        ''')
        db.execute("INSERT INTO findings VALUES (?,'helio',?,'high','open','current',100,100,100,100,NULL,'{}','legacy')",(legacy_id,RULE_ID))
        db.execute("INSERT INTO events VALUES (1,'helio',?,'opened',100,'old-run','legacy','{}')",(legacy_id,))
    store=PlatformStore(path);store.initialize();store.initialize()
    item=store.finding('helio',legacy_id,now=101)
    assert item['event_count']==1 and item['events'][0]['run_id']=='old-run'
    finish(store,evaluation(99,rows=[]),102)
    assert store.finding('helio',legacy_id,now=102)['assessment']=='current'
