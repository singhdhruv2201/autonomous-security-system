import time

import pytest

from test_graph import graph, sources, sync
from helio_security.graph import GraphStore
from helio_security.reader import GraphReader, entity_id

pytestmark = pytest.mark.neo4j


def test_inventory_pages_and_ids_are_stable_and_company_scoped(graph, sources):
    sync(graph, sources, time.time())
    reader = GraphReader(graph)
    first = reader.entities(kind="Employee", limit=3, offset=0)
    second = reader.entities(kind="Employee", limit=3, offset=3)
    assert first["total"] == 20 and len(first["items"]) == 3
    assert not {x["id"] for x in first["items"]} & {x["id"] for x in second["items"]}
    item = first["items"][0]
    assert reader.entity(item["id"]) == item
    assert "source_id" in item and "key" not in item
    other = GraphReader(GraphStore(graph.driver, "other-company"))
    assert other.entities()["total"] == 0
    assert other.entity(item["id"]) is None
    assert reader.entity("malformed-id") is None


def test_graph_and_neighborhood_are_bounded_and_keep_endpoints(graph, sources):
    sync(graph, sources, time.time())
    reader = GraphReader(graph)
    small = reader.graph(limit=5, edge_limit=2)
    assert len(small["nodes"]) == 5 and len(small["edges"]) <= 2
    assert small["truncated"] is True
    ids = {n["id"] for n in small["nodes"]}
    assert all(e["start"] in ids and e["end"] in ids for e in small["edges"])
    complete = reader.graph(limit=100, edge_limit=200)
    assert len(complete["nodes"]) == 54 and len(complete["edges"]) == 75
    assert complete["truncated"] is False
    agent = entity_id(graph.key("Agent", "agent:sales-research"))
    neighborhood = reader.relationships(agent, limit=100)
    assert {e["kind"] for e in neighborhood["edges"]} == {"OWNS", "USES", "HAS_GRANT"}
    assert reader.relationships("missing") is None


def test_rule_evaluation_keeps_missing_evidence_unknown(graph, sources):
    now = time.time()
    sources[0].set_employee_status("emp-maya-chen", "inactive")
    sync(graph, sources, now)
    reader = GraphReader(graph)
    result = reader.evaluate(now)
    assert result["coverage_complete"] is True
    assert result["candidates"][0]["state"] == "open"
    crm, tokens = sources[1:]
    payload = crm.inventory(tokens["observer"])
    payload["grants"] = [g for g in payload["grants"] if g["id"] != "grant-sales-agent"]
    from helio_security.models import normalize
    graph.apply(normalize("crm", payload), now + 1)
    result = reader.evaluate(now + 1)
    assert result["candidates"][0]["state"] == "unknown"
    assert result["candidates"][0]["reason"] == "missing_evidence"
    crm.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
    sync(graph, sources, now + 2)
    assert reader.evaluate(now + 2)["candidates"][0]["reason"] == "grant_revoked"
    graph.fail("crm", "http_403", now + 3)
    assert reader.evaluate(now + 3)["candidates"][0]["state"] == "unknown"
    assert reader.evaluate(now + 1000)["coverage_complete"] is False


def test_cli_and_platform_rule_agree_when_agent_has_no_customer_scope(graph, sources):
    sources[0].set_employee_status("emp-maya-chen", "inactive")
    with sources[1].connection() as db:
        db.execute("UPDATE grants SET scopes='[]' WHERE id='grant-sales-agent'")
    now = time.time()
    sync(graph, sources, now)
    assert graph.risks(now)["findings"] == []
    assert GraphReader(graph).evaluate(now)["candidates"][0]["reason"] == "no_customer_access_scope"
