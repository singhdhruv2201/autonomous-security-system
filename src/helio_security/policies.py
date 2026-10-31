"""Versioned capabilities. Deployment mode is separate from implementation support."""
from dataclasses import asdict, dataclass, replace

from .rules import RULE_ID


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
                'coverage': ('Requires fresh directory and CRM evidence. Missing evidence never resolves a finding.'
                             if self.lifecycle == 'posture' else 'Request evidence; historical incidents do not expire with graph freshness.')}


POLICIES = {p.id: p for p in (
    Policy(RULE_ID, 'Inactive owner with agent access',
           'Detect an agent that retains customer access after its owner becomes inactive.',
           lifecycle='posture', implemented=True, mode='observe', sources=('directory', 'crm'),
           conditions=('Owner is inactive', 'Agent has an active grant', 'Grant permits customer read or write', 'Target resource is confidential'),
           suggested_fix='Review ownership and revoke the affected CRM grant if this access is no longer required.'),
    Policy('resource_permissions', 'Resource and action permissions', 'Require the exact application, resource and action grant.'),
    Policy('owner_status', 'Inactive owner or employee', 'Use fresh directory evidence before customer access.'),
    Policy('credential_lifecycle', 'Principal and credential validity', 'Reject disabled principals and revoked or expired credentials.'),
    Policy('extraction_limits', 'CRM extraction limits', 'Apply shared rolling request and record budgets and a bulk result limit.'),
    Policy('authentication_failures', 'Repeated access failures', 'Temporarily throttle repeated authentication or authorization failures.'),
    Policy('sensitive_content', 'Sensitive CRM content', 'Scan notes and customer responses for supported secrets and PII.'),
)}


def policy(rule):
    try:
        return POLICIES[rule]
    except KeyError:
        raise ValueError('Unknown policy') from None

# Implemented capabilities remain off until the authoritative CRM mode is observed.
POLICIES = {key: replace(value,implemented=True) for key,value in POLICIES.items()}
