from concurrent.futures import ThreadPoolExecutor

import pytest

from helio_security.platform_store import PlatformStore
from helio_security.rules import assess


def evidence(**changes):
    return {"employee_id": "maya", "employee_name": "Maya", "employee_status": "inactive",
            "agent_id": "sales", "grant_id": "grant-sales", "grant_status": "active", "grant_version": 1,
            "resource_id": "customers", "classification": "confidential", "scopes": ["customers:read"],
            "present": True, "directory_observed_at": 100.0, "crm_observed_at": 100.0, **changes}


def evaluation(at=100.0, coverage=True, rows=None):
    return {"coverage_complete": coverage, "observed_at": at,
            "sources": {"directory": {"last_success": at}, "crm": {"last_success": at}},
            "candidates": [assess(row, coverage) for row in (rows if rows is not None else [evidence()])]}


@pytest.fixture
def store(tmp_path):
    result = PlatformStore(tmp_path / "security.sqlite3")
    result.initialize()
    return result


def finish(store, result, now=100.0, company="helio"):
    run, created = store.enqueue(company, "operator", "manual", now)
    assert created
    assert store.claim(company, now)["id"] == run["id"]
    store.finish(company, run["id"], {"directory": "ok", "crm": "ok"}, result, now)
    return run["id"]


@pytest.mark.parametrize("changes,coverage,state,reason", [
    ({}, True, "open", "inactive_owner_active_access"),
    ({"grant_status": "revoked"}, True, "resolved", "grant_revoked"),
    ({"employee_status": "active"}, True, "resolved", "owner_active"),
    ({"classification": "internal"}, True, "resolved", "resource_not_confidential"),
    ({"scopes": []}, True, "resolved", "no_customer_access_scope"),
    ({"present": False}, True, "unknown", "missing_evidence"),
    ({}, False, "unknown", "incomplete_coverage"),
])
def test_rule_requires_fresh_explicit_evidence(changes, coverage, state, reason):
    candidate = assess(evidence(**changes), coverage)
    assert (candidate["state"], candidate["reason"]) == (state, reason)


def test_findings_deduplicate_survive_restart_and_reopen_with_same_id(store):
    finish(store, evaluation())
    first = store.findings("helio", now=100)["items"][0]
    finish(store, evaluation(101), 101)
    restarted = PlatformStore(store.path)
    restarted.initialize()
    assert restarted.findings("helio", now=101)["total"] == 1
    assert [e["type"] for e in restarted.finding("helio", first["id"], now=101)["events"]] == ["opened"]
    finish(store, evaluation(102, rows=[evidence(grant_status="revoked", grant_version=2)]), 102)
    assert store.finding("helio", first["id"], now=102)["status"] == "resolved"
    finish(store, evaluation(103), 103)
    reopened = store.finding("helio", first["id"], now=103)
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    assert [e["type"] for e in reopened["events"]] == ["opened", "resolved", "reopened"]


@pytest.mark.parametrize("result", [evaluation(101, coverage=False, rows=[]),
    evaluation(101, rows=[]), evaluation(101, rows=[evidence(present=False)])])
def test_failed_or_missing_observation_never_resolves_open_finding(store, result):
    finish(store, evaluation())
    finding_id = store.findings("helio", now=100)["items"][0]["id"]
    finish(store, result, 101)
    item = store.finding("helio", finding_id, now=101)
    assert item["status"] == "open" and item["assessment"] == "unknown"
    assert item["last_verified"] == 100
    assert item["evidence"]["grant_status"] == "active"


def test_finding_freshness_expires_without_new_sync(store):
    finish(store, evaluation())
    item = store.findings("helio", now=1001)["items"][0]
    assert item["status"] == "open" and item["evidence_fresh"] is False
    assert item["assessment"] == "unknown"


def test_safe_rows_do_not_create_findings_and_company_data_is_isolated(store):
    finish(store, evaluation(rows=[evidence(grant_status="revoked")]))
    assert store.findings("helio", now=100)["total"] == 0
    finish(store, evaluation(101), 101)
    item = store.findings("helio", now=101)["items"][0]
    assert store.findings("other", now=101)["total"] == 0
    assert store.finding("other", item["id"], now=101) is None
    assert store.runs("other")["total"] == 0
    assert store.audit("other")["total"] == 0


def test_overlapping_sync_requests_create_one_durable_run(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.enqueue("helio", "operator", "manual", 100), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({run["id"] for run, _ in results}) == 1
    assert store.claim("helio", 101) is not None
    assert store.claim("helio", 102) is None
    assert store.enqueue("helio", "operator", "manual", 102)[1] is False
    assert store.audit("helio")["total"] == 1


def test_restart_marks_running_interrupted_but_retains_queued_jobs(store):
    first, _ = store.enqueue("helio", "operator", "manual", 100)
    store.claim("helio", 101)
    other, _ = store.enqueue("other", "operator", "manual", 102)
    store.recover("helio", 103)
    assert store.run("helio", first["id"])["status"] == "interrupted"
    assert store.run("other", other["id"])["status"] == "queued"
    assert store.enqueue("helio", "operator", "manual", 104)[1] is True


def test_old_evaluation_cannot_undo_newer_finding_state(store):
    finish(store, evaluation(101), 101)
    finish(store, evaluation(100, rows=[evidence(grant_status="revoked")]), 102)
    assert store.findings("helio", now=102)["items"][0]["status"] == "open"


def test_run_failure_preserves_findings_but_invalidates_assessment(store):
    finish(store, evaluation())
    run, _ = store.enqueue("helio", "operator", "manual", 101)
    store.claim("helio", 101)
    store.finish("helio", run["id"], {}, None, 101, error="observation_failed")
    assert store.run("helio", run["id"])["status"] == "failed"
    assert store.findings("helio", now=101)["items"][0]["assessment"] == "unknown"


def test_event_history_keeps_the_evidence_used_for_each_transition(store):
    finish(store, evaluation())
    finish(store, evaluation(101, rows=[evidence(grant_status="revoked", grant_version=2)]), 101)
    item = store.findings("helio", now=101)["items"][0]
    events = store.finding("helio", item["id"], now=101)["events"]
    assert events[0]["evidence"]["grant_status"] == "active"
    assert events[1]["evidence"]["grant_status"] == "revoked"


def test_invalid_evaluation_rolls_back_finding_and_run_completion_together(store):
    run, _ = store.enqueue("helio", "operator", "manual", 100)
    store.claim("helio", 100)
    result = evaluation()
    result["candidates"].append({"identity": ["malformed"]})
    with pytest.raises(KeyError):
        store.finish("helio", run["id"], {"directory": "ok", "crm": "ok"}, result, 100)
    assert store.findings("helio", now=100)["total"] == 0
    assert store.run("helio", run["id"])["status"] == "running"


def test_recovery_invalidates_findings_after_interrupted_observation(store):
    finish(store, evaluation())
    run, _ = store.enqueue("helio", "operator", "manual", 101)
    store.claim("helio", 101)
    store.recover("helio", 102)
    assert store.findings("helio", now=102)["items"][0]["assessment"] == "unknown"
    assert store.audit("helio")["items"][0]["action"] == "sync_interrupted"
