"""The v1 posture rule and preservation of existing platform history."""
import json
import uuid

import pytest

from helio_security.platform_store import PlatformStore
from helio_security.rules import RULE_ID
from test_platform_store import evaluation, finish


def test_registry_contains_only_the_versioned_inactive_owner_rule():
    from helio_security.policies import POLICIES
    assert set(POLICIES) == {RULE_ID}
    assert POLICIES[RULE_ID].enabled is True
    assert POLICIES[RULE_ID].version == 1
    assert POLICIES[RULE_ID].lifecycle == 'posture'
    assert all(not p.enabled for p in POLICIES.values() if not p.implemented)


def test_retired_policy_history_remains_readable_without_reactivating_it(tmp_path):
    store = PlatformStore(tmp_path / "platform.db")
    store.initialize()
    finish(store, evaluation(200), 200)
    original = store.findings("helio", now=200)["items"][0]
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["helio", RULE_ID, "maya", "sales", "grant-sales", "customers"])))
    assert original["id"] == expected
    # A database written by the previous release contains activity incidents.
    with store.connection() as db:
        db.execute("INSERT INTO findings SELECT 'legacy-incident', company_id, 'resource_permissions', severity, status, assessment, first_seen, last_seen, last_evaluated, last_verified, resolved_at, evidence, reason FROM findings WHERE id=?", (expected,))
        db.execute("INSERT INTO events(company_id,finding_id,type,occurred_at,run_id,reason,evidence) VALUES ('helio','legacy-incident','opened',200,NULL,'access_denied','{}')")
        db.execute("INSERT INTO activity_import_state VALUES ('helio','old-instance',42,200)")
    store.initialize()
    finish(store, evaluation(201), 201)
    items = {f["rule"]: f for f in store.findings("helio", now=10000)["items"]}
    assert items[RULE_ID]["assessment"] == "unknown"
    legacy = store.finding("helio", "legacy-incident", now=10000)
    assert legacy["title"] == "Historical CRM policy: resource_permissions"
    assert legacy["event_count"] == 1 and legacy["events"][0]["reason"] == "access_denied"
    assert legacy["status"] == "open" and legacy["lifecycle"] == "incident"
    with store.connection() as db:
        assert db.execute("SELECT cursor FROM activity_import_state WHERE company_id='helio'").fetchone()[0] == 42
    assert store.finding("other", "legacy-incident") is None
    assert store.finding("helio", expected, now=201)["event_count"] == 1


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
