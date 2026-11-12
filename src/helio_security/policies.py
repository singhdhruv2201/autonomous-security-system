"""Versioned capabilities. Deployment mode is separate from implementation support."""
from dataclasses import asdict, dataclass

from .rules import RULE_ID, RULES


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    description: str
    lifecycle: str = 'incident'
    version: int = 1
    implemented: bool = False
    mode: str = 'off'
    severity: str = 'high'
    sources: tuple = ('crm',)
    conditions: tuple = ()
    suggested_fix: str = 'Review the recorded decision and the affected access.'

    @property
    def enabled(self):
        return self.implemented and self.mode != 'off'

    def view(self):
        return {**asdict(self), 'enabled': self.enabled,
                'coverage': ('Requires fresh ' + ', '.join(self.sources) + ' evidence. Missing evidence never resolves a finding.'
                             if self.lifecycle == 'posture' else 'Request evidence; historical incidents do not expire with graph freshness.')}


POLICIES = {p.id: p for p in (
    Policy(RULE_ID, 'Inactive owner with agent access',
           'Detect an agent that retains customer access after its owner becomes inactive.',
           lifecycle='posture', implemented=True, mode='observe', sources=('directory', 'crm'),
           conditions=('Owner is inactive', 'Agent has an active grant', 'Grant permits customer read or write', 'Target resource is confidential'),
           suggested_fix='Review ownership and revoke the affected CRM grant if this access is no longer required.'),
)}


for rule in RULES.values():
    if rule.id != RULE_ID:
        POLICIES[rule.id] = Policy(rule.id, rule.title, rule.title,
            lifecycle='posture', implemented=True, mode='observe', sources=rule.sources,
            suggested_fix='Restrict public access or contain the unapproved connection after reviewing evidence.')


def policy(rule):
    try:
        return POLICIES[rule]
    except KeyError:
        raise ValueError('Unknown policy') from None


def workflow_catalog():
    from .decision import DECISION_RULES
    from .playbooks import REGISTRY
    descriptions = {
        'crm.pause_principal':'Pause the principal’s CRM access; undo restores its saved state.',
        'crm.revoke_grant':'Revoke the CRM grant after explicit approval.',
        'workbench.restrict_app':'Make the app company-only and verify that its public link returns 404.',
        'workbench.pause_agent':'Pause the agent; resumption checks remaining destinations.',
        'workbench.disable_destination':'Disable the destination connection; undo restores its saved state.',
    }
    return {'decision_rules':list(DECISION_RULES), 'playbooks':[
        {'id':identifier, 'reversible':book.reversible, 'description':descriptions[identifier]}
        for identifier,book in REGISTRY.items()]}
