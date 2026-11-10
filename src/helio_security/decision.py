"""Pure decision function. No I/O, no model calls, no side effects.

A decision is computed from the Facts struct and a confidence level.
The model's assessment can only lower autonomy; it cannot pick playbooks
outside the rule's list or skip approvals.

Decision table (first match wins):
1. human_override → ask_approver
2. leaking_now + confidential + reversible containment → contain (then ask approver or tell owner)
3. inactive/unknown owner + reversible fix + no dependents → fix (then ask lead or approver)
4. active owner + exposure != none → ask_owner with deadline
5. irreversible fix or dependents > 0 → ask_approver
6. otherwise → record
"""

from dataclasses import dataclass
from typing import Literal, Optional

# Playbook metadata: which playbook is the reversible containment/fix per rule,
# and which is the lasting (irreversible) fix.
_RULE_PLAYBOOKS = {
    "inactive_owner_agent_access": {
        "reversible": "crm.pause_principal",
        "lasting": "crm.revoke_grant",
    },
    "public_app_confidential_data": {
        "reversible": "workbench.restrict_app",
        "lasting": None,
    },
    "agent_unapproved_destination": {
        "reversible": "workbench.pause_agent",
        "lasting": "workbench.disable_destination",
    },
}


@dataclass(frozen=True)
class Facts:
    rule: str
    target_kind: Literal["agent", "app"]
    owner_status: Literal["active", "inactive", "unknown"]
    data_classification: Literal["internal", "confidential"]
    record_count: int
    exposure: Literal["none", "public", "external"]
    leaking_now: bool
    dependents: int
    human_override: bool
    has_lead: bool = True
    addressed_to: Optional[str] = None
    # Escape hatch for tests: force the irreversible branch.
    force_irreversible: bool = False

    def _replace(self, **kwargs):
        from dataclasses import replace
        return replace(self, **kwargs)


@dataclass(frozen=True)
class Decision:
    action: Literal["contain", "fix", "ask_owner", "ask_approver", "record"]
    playbook: Optional[str]
    followup: Optional[Literal["ask_approver", "ask_lead", "tell_owner"]]
    deadline_seconds: Optional[int]
    addressed_to: Optional[str]
    reasoning: str


def decide(facts: Facts, *, confidence: Literal["high", "medium", "low"]) -> Decision:
    """Compute a Decision from facts and confidence. Pure function; no I/O."""

    playbooks = _RULE_PLAYBOOKS.get(facts.rule, {})
    reversible = playbooks.get("reversible")
    lasting = playbooks.get("lasting")
    is_confidential = facts.data_classification == "confidential"

    # Rule 1: human previously undid Awasi's fix → always ask approver.
    if facts.human_override:
        return Decision(
            action="ask_approver",
            playbook=None,
            followup=None,
            deadline_seconds=None,
            addressed_to=facts.addressed_to,
            reasoning="Human override: escalate to approver",
        )

    # Rule 2: actively leaking + confidential + reversible containment exists
    if facts.leaking_now and is_confidential and reversible:
        followup = "ask_approver" if lasting else "tell_owner"
        return Decision(
            action="contain",
            playbook=reversible,
            followup=followup,
            deadline_seconds=None,
            addressed_to=facts.addressed_to,
            reasoning="Leaking now: contain immediately",
        )

    # Rule 3: inactive/unknown owner + reversible fix + no dependents → auto-fix
    # Low confidence downgrades to ask_approver.
    if (facts.owner_status in ("inactive", "unknown")
            and reversible
            and facts.dependents == 0
            and not facts.force_irreversible):
        if confidence == "low":
            return Decision(
                action="ask_approver",
                playbook=reversible,
                followup=None,
                deadline_seconds=None,
                addressed_to=facts.addressed_to,
                reasoning="Low confidence: escalate to approver instead of auto-fixing",
            )
        followup = "ask_lead" if facts.has_lead else "ask_approver"
        return Decision(
            action="fix",
            playbook=reversible,
            followup=followup,
            deadline_seconds=None,
            addressed_to=facts.addressed_to,
            reasoning="Inactive/unknown owner, reversible fix, no dependents: fix and ask lead",
        )

    # Rule 4: active owner + exposure → ask owner with deadline
    if facts.owner_status == "active" and facts.exposure != "none":
        return Decision(
            action="ask_owner",
            playbook=reversible,
            followup=None,
            deadline_seconds=900,  # overridden by settings at runtime
            addressed_to=facts.addressed_to,
            reasoning="Active owner with exposure: ask owner",
        )

    # Rule 5: irreversible fix required, or dependents > 0 → approver must approve
    if facts.force_irreversible or facts.dependents > 0 or not reversible:
        return Decision(
            action="ask_approver",
            playbook=lasting or reversible,
            followup=None,
            deadline_seconds=None,
            addressed_to=facts.addressed_to,
            reasoning="Irreversible fix or dependents: approver required",
        )

    # Rule 6: record and send nothing
    return Decision(
        action="record",
        playbook=None,
        followup=None,
        deadline_seconds=None,
        addressed_to=facts.addressed_to,
        reasoning="No automatic action warranted",
    )
