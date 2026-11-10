"""Unit tests for CaseStore — idempotency, one-active-case-per-finding, event keys.

TDD: written first, before cases.py exists.
"""

import time
import uuid

import pytest

from helio_security.cases import CaseStore


@pytest.fixture
def store(tmp_path):
    s = CaseStore(tmp_path / "security.sqlite3")
    s.initialize()
    return s


FINDING_ID = "finding-aaa"
COMPANY = "helio"


class TestCaseCreation:
    def test_create_case_returns_id(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        assert isinstance(case_id, str)
        assert len(case_id) > 0

    def test_only_one_active_case_per_finding(self, store):
        id1 = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        id2 = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=2.0)
        # second call returns the existing open case id
        assert id1 == id2

    def test_new_case_allowed_after_previous_resolved(self, store):
        id1 = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.close_case(id1, status="resolved", now=2.0)
        id2 = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=3.0)
        assert id2 != id1

    def test_case_has_expected_initial_fields(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0,
                                  summary="Agent still has access", addressed_to="emp-hannah-reed")
        case = store.get_case(case_id)
        assert case["company_id"] == COMPANY
        assert case["finding_id"] == FINDING_ID
        assert case["status"] == "investigating"
        assert case["route"] == "ask_lead"
        assert case["summary"] == "Agent still has access"
        assert case["addressed_to"] == "emp-hannah-reed"
        assert case["created_at"] == pytest.approx(1.0)


class TestCaseEvents:
    def test_add_event_stores_text(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.add_event(case_id, key="paused", type="action_taken",
                        actor="awasi", text="Paused principal agent:sales-research", now=2.0)
        events = store.get_events(case_id)
        assert len(events) == 1
        assert events[0]["text"] == "Paused principal agent:sales-research"
        assert events[0]["key"] == "paused"
        assert events[0]["actor"] == "awasi"

    def test_duplicate_event_key_is_noop(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.add_event(case_id, key="paused", type="action_taken", actor="awasi",
                        text="First write", now=2.0)
        store.add_event(case_id, key="paused", type="action_taken", actor="awasi",
                        text="Second write", now=3.0)
        events = store.get_events(case_id)
        assert len(events) == 1
        assert events[0]["text"] == "First write"

    def test_different_keys_both_stored(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.add_event(case_id, key="e1", type="info", actor="awasi", text="A", now=2.0)
        store.add_event(case_id, key="e2", type="info", actor="awasi", text="B", now=3.0)
        assert len(store.get_events(case_id)) == 2


class TestActions:
    def test_record_action_stores_pending(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        action_id = store.record_action(
            case_id, playbook="crm.pause_principal", target="agent:sales-research",
            idempotency_key="pause-aaa", requested_by="awasi", now=2.0,
        )
        action = store.get_action(action_id)
        assert action["status"] == "pending"
        assert action["playbook"] == "crm.pause_principal"
        assert action["target"] == "agent:sales-research"

    def test_duplicate_idempotency_key_returns_existing(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        id1 = store.record_action(
            case_id, playbook="crm.pause_principal", target="agent:sales-research",
            idempotency_key="pause-aaa", requested_by="awasi", now=2.0,
        )
        id2 = store.record_action(
            case_id, playbook="crm.pause_principal", target="agent:sales-research",
            idempotency_key="pause-aaa", requested_by="awasi", now=3.0,
        )
        assert id1 == id2

    def test_update_action_status(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        action_id = store.record_action(
            case_id, playbook="crm.pause_principal", target="agent:sales-research",
            idempotency_key="pause-bbb", requested_by="awasi", now=2.0,
        )
        store.update_action(action_id, status="executed", result={"access_version": 2},
                            before={"principal_status": "active"}, now=3.0)
        action = store.get_action(action_id)
        assert action["status"] == "executed"
        assert action["result"]["access_version"] == 2
        assert action["before"]["principal_status"] == "active"

    def test_actions_listed_for_case(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.record_action(case_id, playbook="crm.pause_principal", target="a",
                            idempotency_key="k1", requested_by="awasi", now=2.0)
        store.record_action(case_id, playbook="crm.revoke_grant", target="b",
                            idempotency_key="k2", requested_by="emp-casey-wong", now=3.0)
        actions = store.get_actions(case_id)
        assert len(actions) == 2


class TestDecisions:
    def test_record_decision(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0)
        store.record_decision(case_id, key="q1", employee_id="emp-hannah-reed",
                              source="slack", choice="retire", now=2.0)
        decision = store.consume_decision(case_id, key="q1")
        assert decision is not None
        assert decision["choice"] == "retire"
        assert decision["employee_id"] == "emp-hannah-reed"

    def test_consume_marks_as_consumed(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0)
        store.record_decision(case_id, key="q1", employee_id="emp-hannah-reed",
                              source="slack", choice="retire", now=2.0)
        store.consume_decision(case_id, key="q1")
        # Consuming again returns None
        assert store.consume_decision(case_id, key="q1") is None

    def test_unconsumed_decision_found(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0)
        store.record_decision(case_id, key="q1", employee_id="emp-hannah-reed",
                              source="slack", choice="retire", now=2.0)
        pending = store.unconsumed_decisions(case_id)
        assert len(pending) == 1

    def test_no_pending_decisions_after_consume(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0)
        store.record_decision(case_id, key="q1", employee_id="emp-hannah-reed",
                              source="slack", choice="retire", now=2.0)
        store.consume_decision(case_id, key="q1")
        assert store.unconsumed_decisions(case_id) == []


class TestCaseListing:
    def test_active_cases_for_company(self, store):
        store.open_case(COMPANY, "f1", route="fix_then_ask", now=1.0)
        store.open_case(COMPANY, "f2", route="ask_lead", now=1.0)
        cases = store.active_cases(COMPANY)
        assert len(cases) == 2

    def test_closed_cases_not_in_active(self, store):
        id1 = store.open_case(COMPANY, "f1", route="fix_then_ask", now=1.0)
        store.open_case(COMPANY, "f2", route="ask_lead", now=1.0)
        store.close_case(id1, status="resolved", now=2.0)
        cases = store.active_cases(COMPANY)
        assert len(cases) == 1

    def test_waiting_cases_past_deadline(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="ask_lead", now=1.0)
        store.update_case_status(case_id, status="waiting_owner", now=1.0, wait_until=5.0)
        # at t=4 not yet due
        assert store.overdue_waiting_cases(COMPANY, now=4.0) == []
        # at t=6 it is due
        due = store.overdue_waiting_cases(COMPANY, now=6.0)
        assert len(due) == 1
        assert due[0]["id"] == case_id

    def test_cases_with_pending_findings(self, store):
        """open_findings_without_case returns finding IDs with no active case."""
        result = store.open_findings_without_case(COMPANY, open_finding_ids=["f1", "f2"])
        assert set(result) == {"f1", "f2"}
        store.open_case(COMPANY, "f1", route="fix_then_ask", now=1.0)
        result2 = store.open_findings_without_case(COMPANY, open_finding_ids=["f1", "f2"])
        assert result2 == ["f2"]

    def test_update_case_status(self, store):
        case_id = store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=1.0)
        store.update_case_status(case_id, status="fixing", now=2.0)
        assert store.get_case(case_id)["status"] == "fixing"
