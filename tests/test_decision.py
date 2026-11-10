"""Unit tests for the pure decision function.

TDD: these are written first. Each test maps to one row in the decision table
(awasi-v1-plan.md, section 'Decision model') or to an edge case.
"""

import pytest

from helio_security.decision import Facts, Decision, decide


def base_facts(**overrides):
    """Return minimal open-finding facts that would otherwise reach the fallback."""
    return Facts(
        rule="inactive_owner_agent_access",
        target_kind="agent",
        owner_status="inactive",
        data_classification="confidential",
        record_count=6,
        exposure="none",
        leaking_now=False,
        dependents=0,
        human_override=False,
    )._replace(**overrides)


# ---------------------------------------------------------------------------
# Rule 1: human_override → ask approver
# ---------------------------------------------------------------------------

class TestHumanOverride:
    def test_override_sends_to_approver(self):
        facts = base_facts(human_override=True)
        d = decide(facts, confidence="high")
        assert d.action == "ask_approver"
        assert d.playbook is None

    def test_override_beats_leaking_now(self):
        facts = base_facts(human_override=True, leaking_now=True)
        d = decide(facts, confidence="high")
        assert d.action == "ask_approver"


# ---------------------------------------------------------------------------
# Rule 2: leaking_now + confidential + reversible containment → contain first
# ---------------------------------------------------------------------------

class TestLeakingNow:
    def test_leaking_inactive_owner_agent_contains_immediately(self):
        facts = base_facts(leaking_now=True)
        d = decide(facts, confidence="high")
        assert d.action == "contain"
        assert d.playbook == "crm.pause_principal"

    def test_leaking_then_ask_approver_for_lasting_fix(self):
        # inactive_owner_agent_access has a lasting fix (revoke_grant) → ask approver
        facts = base_facts(leaking_now=True)
        d = decide(facts, confidence="high")
        assert d.followup == "ask_approver"

    def test_leaking_no_separate_lasting_fix_tells_owner(self):
        # If the only playbook is reversible (no lasting fix), tell the owner and offer Undo.
        # We model this with the public_app rule which has only restrict_app (reversible, no lasting).
        facts = base_facts(
            rule="public_app_confidential_data",
            target_kind="app",
            leaking_now=True,
        )
        d = decide(facts, confidence="high")
        assert d.action == "contain"
        assert d.followup == "tell_owner"


# ---------------------------------------------------------------------------
# Rule 3: inactive/unknown owner + reversible fix + no dependents → fix then ask lead
# ---------------------------------------------------------------------------

class TestInactiveOwnerFix:
    def test_inactive_owner_no_dependents_fixes_and_asks_lead(self):
        facts = base_facts()
        d = decide(facts, confidence="high")
        assert d.action == "fix"
        assert d.playbook == "crm.pause_principal"
        assert d.followup == "ask_lead"

    def test_unknown_owner_also_triggers_fix(self):
        facts = base_facts(owner_status="unknown")
        d = decide(facts, confidence="high")
        assert d.action == "fix"

    def test_no_lead_falls_back_to_approver(self):
        facts = base_facts(has_lead=False)
        d = decide(facts, confidence="high")
        assert d.followup == "ask_approver"

    def test_dependents_above_zero_escalates_to_approver(self):
        facts = base_facts(dependents=2)
        d = decide(facts, confidence="high")
        assert d.action == "ask_approver"

    def test_low_confidence_downgrades_auto_fix_to_approver(self):
        """confidence='low' must turn an automatic fix into an approver question."""
        facts = base_facts()
        d = decide(facts, confidence="low")
        assert d.action == "ask_approver"

    def test_medium_confidence_still_auto_fixes(self):
        facts = base_facts()
        d = decide(facts, confidence="medium")
        assert d.action == "fix"


# ---------------------------------------------------------------------------
# Rule 4: active owner + exposure → ask owner with deadline
# ---------------------------------------------------------------------------

class TestActiveOwnerExposure:
    def test_active_owner_exposed_asks_owner(self):
        facts = base_facts(
            rule="public_app_confidential_data",
            target_kind="app",
            owner_status="active",
            exposure="public",
        )
        d = decide(facts, confidence="high")
        assert d.action == "ask_owner"
        assert d.deadline_seconds is not None

    def test_active_owner_external_exposure_asks_owner(self):
        facts = base_facts(
            rule="agent_unapproved_destination",
            target_kind="agent",
            owner_status="active",
            exposure="external",
        )
        d = decide(facts, confidence="high")
        assert d.action == "ask_owner"


# ---------------------------------------------------------------------------
# Rule 5: irreversible fix or dependents → approver must approve
# ---------------------------------------------------------------------------

class TestApproverRequired:
    def test_irreversible_fix_requires_approver(self):
        # crm.revoke_grant is irreversible; force it as the only playbook
        facts = base_facts(force_irreversible=True)
        d = decide(facts, confidence="high")
        assert d.action == "ask_approver"


# ---------------------------------------------------------------------------
# Rule 6: fallback – record and send nothing
# ---------------------------------------------------------------------------

class TestFallback:
    def test_active_owner_no_exposure_records_only(self):
        facts = base_facts(owner_status="active", exposure="none")
        d = decide(facts, confidence="high")
        assert d.action == "record"
        assert d.playbook is None


# ---------------------------------------------------------------------------
# Decision is a named-tuple / dataclass; check the shape
# ---------------------------------------------------------------------------

class TestDecisionShape:
    def test_decision_has_required_fields(self):
        facts = base_facts()
        d = decide(facts, confidence="high")
        assert hasattr(d, "action")
        assert hasattr(d, "playbook")
        assert hasattr(d, "followup")
        assert hasattr(d, "deadline_seconds")
        assert hasattr(d, "addressed_to")

    def test_decide_is_pure_no_io(self):
        """decide() must not perform I/O – calling it twice gives the same result."""
        facts = base_facts()
        assert decide(facts, confidence="high") == decide(facts, confidence="high")
