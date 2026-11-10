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
