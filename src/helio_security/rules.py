"""Explainable policy assessment. No model calls or autonomous remediation."""

RULE_ID = "inactive_owner_agent_access"
EVIDENCE_FIELDS = {
    "employee_id", "employee_name", "employee_status", "agent_id", "grant_id",
    "grant_status", "grant_version", "resource_id", "classification", "scopes",
    "present", "directory_observed_at", "crm_observed_at",
    "principal_status", "record_count",
}


def assess(row, coverage_complete):
    if not coverage_complete:
        state, reason = "unknown", "incomplete_coverage"
    elif not row["present"]:
        state, reason = "unknown", "missing_evidence"
    elif row["grant_status"] == "revoked":
        state, reason = "resolved", "grant_revoked"
    elif row.get("principal_status") == "disabled":
        state, reason = "resolved", "agent_paused"
    elif row["employee_status"] == "active":
        state, reason = "resolved", "owner_active"
    elif row["classification"] != "confidential":
        state, reason = "resolved", "resource_not_confidential"
    elif not {"customers:read", "customers:write"}.intersection(row["scopes"]):
        state, reason = "resolved", "no_customer_access_scope"
    else:
        state, reason = "open", "inactive_owner_active_access"
    return {"identity": [row[k] for k in ("employee_id", "agent_id", "grant_id", "resource_id")],
            "state": state, "reason": reason,
            "evidence": {k: row.get(k, "active" if k == "principal_status" else None) for k in EVIDENCE_FIELDS}}


from dataclasses import dataclass

WORKBENCH_FIELDS = EVIDENCE_FIELDS | {
    'app_id', 'asset_name', 'visibility', 'slug', 'version', 'agent_status',
    'connection_id', 'host', 'enabled', 'connection_version', 'principal_id',
    'workbench_observed_at', 'anonymous_views_1h', 'last_anonymous_view_at',
    'records_sent_1h', 'records_sent_total', 'last_sent_at', 'dependents',
}


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    sources: tuple
    query: str
    identity: tuple
    evidence_fields: frozenset

    def assess(self, row, coverage_complete, approved_destinations=frozenset({'partner-api'})):
        if self.id == RULE_ID:
            return assess(row, coverage_complete)
        if not coverage_complete:
            state, reason = 'unknown', 'incomplete_coverage'
        elif not row.get('present'):
            state, reason = 'unknown', 'missing_evidence'
        elif row.get('grant_status') == 'revoked':
            state, reason = 'resolved', 'grant_revoked'
        elif row.get('principal_status') != 'active':
            state, reason = 'resolved', 'principal_disabled'
        elif row.get('classification') != 'confidential':
            state, reason = 'resolved', 'resource_not_confidential'
        elif 'customers:read' not in row.get('scopes', []):
            state, reason = 'resolved', 'no_customer_access_scope'
        elif self.id == 'public_app_confidential_data':
            state, reason = (('open', 'public_confidential_access') if row['visibility'] == 'public'
                             else ('resolved', 'app_restricted'))
        elif row['agent_status'] != 'active':
            state, reason = 'resolved', 'agent_paused'
        elif not row['enabled']:
            state, reason = 'resolved', 'connection_disabled'
        elif row['host'] in approved_destinations:
            state, reason = 'resolved', 'destination_approved'
        else:
            state, reason = 'open', 'unapproved_destination'
        return {'identity': [row[k] for k in self.identity], 'state': state, 'reason': reason,
                'evidence': {k: row.get(k) for k in self.evidence_fields if k in row}}


INACTIVE_QUERY = """
MATCH p=(e:Employee {company_id:$company})-[:OWNS]->(a:Agent)
    -[:HAS_GRANT]->(g:AccessGrant)-[:ON]->(d:DataResource)
WHERE all(n IN nodes(p) WHERE n.company_id=$company)
RETURN {employee_id:e.id,employee_name:e.name,employee_status:e.status,
    agent_id:a.id,grant_id:g.id,grant_status:g.status,grant_version:g.version,
    resource_id:d.id,classification:d.classification,scopes:g.scopes,
    principal_status:coalesce(a.principal_status,'active'),record_count:d.record_count,
    present:all(n IN nodes(p) WHERE n.present=true) AND all(r IN relationships(p) WHERE r.present=true),
    directory_observed_at:e.last_observed_at,crm_observed_at:g.last_observed_at} AS candidate
"""


def workbench_query(kind):
    connection = "MATCH q=(a)-[:HAS_CONNECTION]->(c:Connection)-[:TO]->(dst:Destination)" if kind == 'Agent' else ''
    connection_present = " AND all(n IN nodes(q) WHERE n.present=true AND n.company_id=$company) AND all(r IN relationships(q) WHERE r.present=true)" if kind == 'Agent' else ''
    extra = "agent_id:a.id,agent_status:a.status,connection_id:c.id,host:dst.host,enabled:c.enabled,connection_version:c.version,records_sent_1h:c.records_sent_1h,records_sent_total:c.records_sent_total,last_sent_at:c.last_sent_at," if kind == 'Agent' else "app_id:a.id,visibility:a.visibility,slug:a.slug,anonymous_views_1h:a.anonymous_views_1h,last_anonymous_view_at:a.last_anonymous_view_at,"
    return f"""
    MATCH p=(e:Employee {{company_id:$company}})-[:OWNS]->(a:{kind} {{source:'workbench'}})
        -[:ACTS_AS]->(principal)-[:HAS_GRANT]->(g:AccessGrant)-[:ON]->(d:DataResource)
    {connection}
    WHERE all(n IN nodes(p) WHERE n.company_id=$company)
    RETURN {{employee_id:e.id,employee_name:e.name,employee_status:e.status,
        {extra}
        asset_name:a.name,version:a.version,principal_id:principal.id,
        grant_id:g.id,grant_status:g.status,grant_version:g.version,resource_id:d.id,
        classification:d.classification,scopes:g.scopes,record_count:d.record_count,
        principal_status:coalesce(principal.principal_status,'active'),
        present:all(n IN nodes(p) WHERE n.present=true) AND all(r IN relationships(p) WHERE r.present=true){connection_present},
        directory_observed_at:e.last_observed_at,crm_observed_at:g.last_observed_at,
        workbench_observed_at:a.last_observed_at,dependents:0}} AS candidate
    """


RULES = {rule.id: rule for rule in (
    Rule(RULE_ID, 'Inactive owner with agent access', ('directory','crm'), INACTIVE_QUERY,
         ('employee_id','agent_id','grant_id','resource_id'), frozenset(EVIDENCE_FIELDS)),
    Rule('public_app_confidential_data', 'Public app exposes confidential data', ('directory','crm','workbench'),
         workbench_query('App'), ('app_id','grant_id','resource_id'), frozenset(WORKBENCH_FIELDS)),
    Rule('agent_unapproved_destination', 'Agent sends to an unapproved destination', ('directory','crm','workbench'),
         workbench_query('Agent'), ('agent_id','host'), frozenset(WORKBENCH_FIELDS)),
)}
