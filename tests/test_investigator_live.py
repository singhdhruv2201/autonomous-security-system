"""Live integration test for ModelInvestigator with Claude Haiku 5.5 and MCP read tools.

Opt-in only via:
    .venv/bin/python -m pytest tests/test_investigator_live.py --live

Enforces:
    - Maximum spend $1.00 across all runs.
    - Maximum 10 live API requests total across the whole task.
    - Persistent tracking in data/live_test_ledger.json.
    - No secret exposure in logs or traces.
"""
import asyncio
import os
import time
from pathlib import Path
import pytest
from langchain_core.tools import tool

from helio_security.cases import CaseStore
from helio_security.investigator import ModelInvestigator
from helio_security.live_ledger import LiveLedger, BudgetExceededError


def _load_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key and Path(".env").exists():
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and not line.startswith("#"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return key


@pytest.mark.live
def test_live_claude_haiku_investigation(tmp_path):
    api_key = _load_api_key()
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not configured in environment or .env")

    # Persistent ledger in repository data/ directory
    ledger_path = Path("data/live_test_ledger.json")
    ledger = LiveLedger(ledger_path=ledger_path, max_spend_usd=1.00, max_requests=10)

    # Set up case store and trace storage
    db_path = tmp_path / "test_live_cases.sqlite3"
    store = CaseStore(db_path)
    store.initialize()

    case_id = store.open_case("helio", "finding-live-haiku-1", route="investigating", now=time.time())

    # Create bounded read tools
    @tool
    def get_finding(finding_id: str) -> dict:
        """Fetch finding details by ID."""
        return {
            "finding_id": finding_id,
            "rule": "inactive_owner_agent_access",
            "target_kind": "agent",
            "evidence": {
                "agent_id": "agent:sales-research",
                "employee_id": "emp-maya-chen",
                "employee_status": "inactive",
                "record_count": 6,
                "grant_id": "grant-crm-sales",
            },
            "status": "open",
        }

    @tool
    def get_entity(entity_id: str) -> dict:
        """Fetch entity details by ID."""
        if "agent" in entity_id:
            return {
                "entity_id": entity_id,
                "kind": "agent",
                "name": "Sales Research Agent",
                "owner_id": "emp-maya-chen",
            }
        return {
            "entity_id": entity_id,
            "kind": "employee",
            "name": "Maya Chen",
            "status": "inactive",
        }

    model_name = os.getenv("AWASI_MODEL", "claude-haiku-5-5")
    investigator = ModelInvestigator(
        tools=[get_finding, get_entity],
        store=store,
        model_name=model_name,
        api_key=api_key,
        timeout_seconds=45.0,
        ledger=ledger,
    )

    facts = {
        "rule": "inactive_owner_agent_access",
        "agent_id": "agent:sales-research",
        "owner_status": "inactive",
        "record_count": 6,
        "grant_id": "grant-crm-sales",
    }

    # Run investigation
    result = asyncio.run(investigator.investigate("finding-live-haiku-1", facts, case_id=case_id))

    # Assertions on model assessment
    assert result["source"] == "model"
    assert len(result["summary"]) > 0
    assert len(result["summary"]) <= 600
    assert len(result["impact"]) <= 300
    assert result["confidence"] in ("high", "medium", "low")
    assert result["recommended_playbook"] in ("crm.pause_principal", "crm.revoke_grant", "notify_only", None)
    assert "finding-live-haiku-1" in result["evidence_refs"] or "agent:sales-research" in result["evidence_refs"]

    # Verify traces were recorded in SQLite
    spans = store.get_spans(case_id)
    assert len(spans) > 0
    model_spans = [s for s in spans if s["kind"] == "model"]
    assert len(model_spans) > 0
    first_model = model_spans[0]
    duration = first_model["ended_at"] - first_model["started_at"]
    assert duration >= 0
    assert first_model["tokens_in"] is not None and first_model["tokens_in"] > 0
    assert first_model["tokens_out"] is not None and first_model["tokens_out"] > 0
    # Secrets were not recorded
    for s in spans:
        dumped = str(s)
        assert api_key not in dumped

    # Verify case investigation was persisted
    inv_row = store.get_investigation(case_id)
    assert inv_row is not None
    assert inv_row["source"] == "model"
    assert inv_row["summary"] == result["summary"]

    # Verify persistent ledger recorded this run
    data = ledger.get_data()
    assert data["total_requests"] >= 1
    assert data["total_requests"] <= 10
    assert data["total_estimated_cost_usd"] < 1.00
    assert data["runs"][-1]["status"] == "success"
    assert data["runs"][-1]["input_tokens"] > 0
    assert data["runs"][-1]["output_tokens"] > 0
