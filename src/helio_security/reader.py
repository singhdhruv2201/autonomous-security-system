"""Bounded graph views and evidence-based rule evaluations for the platform API."""

import base64
import binascii
import json
import time

from .graph import NODE_KINDS
from .rules import assess

KINDS = set.union(*NODE_KINDS.values())


def entity_id(key):
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def node_view(properties):
    result = dict(properties)
    result["source_id"] = result["id"]
    result["id"] = entity_id(result.pop("key"))
    return result


def edge_view(row):
    result = dict(row["properties"])
    result.update(id=entity_id(result.pop("key")), kind=row["kind"],
                  start=entity_id(row["start"]), end=entity_id(row["end"]))
    return result


class GraphReader:
    def __init__(self, graph):
        self.store = graph

    def query(self, cypher, **params):
        return self.store.driver.execute_query(cypher, company=self.store.company_id,
                                               database_="neo4j", **params).records

    def _key(self, identifier):
        if len(identifier) > 2048:
            return None
        try:
            raw = base64.b64decode(identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True).decode()
            parts = json.loads(raw)
            if (isinstance(parts, list) and len(parts) == 3 and parts[0] == self.store.company_id
                    and parts[1] in KINDS and isinstance(parts[2], str) and entity_id(raw) == identifier):
                return raw
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            pass
        return None

    def entities(self, kind=None, limit=50, offset=0, present=True):
        if kind is not None and kind not in KINDS:
            raise ValueError("Invalid entity kind")
        if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
            raise ValueError("Invalid pagination")
        where = "n.company_id=$company AND n.present=$present AND ($kind IS NULL OR n.kind=$kind)"
        rows = self.query(f"MATCH (n:Entity) WHERE {where} RETURN properties(n) AS node ORDER BY n.key SKIP $offset LIMIT $limit",
                          kind=kind, present=present, offset=offset, limit=limit)
        total = self.query(f"MATCH (n:Entity) WHERE {where} RETURN count(n) AS total", kind=kind, present=present)[0]["total"]
        return {"items": [node_view(r["node"]) for r in rows], "total": total, "limit": limit, "offset": offset}

    def entity(self, identifier):
        key = self._key(identifier)
        if key is None:
            return None
        rows = self.query("MATCH (n:Entity {company_id:$company,key:$key}) RETURN properties(n) AS node", key=key)
        return node_view(rows[0]["node"]) if rows else None

    def graph(self, kind=None, limit=100, edge_limit=200):
        if not 1 <= edge_limit <= 500:
            raise ValueError("Invalid edge limit")
        page = self.entities(kind, limit)
        keys = [self._key(n["id"]) for n in page["items"]]
        rows = self.query("""MATCH (a:Entity {company_id:$company})-[r]->(b:Entity {company_id:$company})
            WHERE a.key IN $keys AND b.key IN $keys AND r.present=true AND a.present=true AND b.present=true
            RETURN properties(r) AS properties, type(r) AS kind, a.key AS start, b.key AS end
            ORDER BY r.key LIMIT $limit""", keys=keys, limit=edge_limit + 1)
        return {"nodes": page["items"], "edges": [edge_view(r) for r in rows[:edge_limit]],
                "total_matching_nodes": page["total"], "truncated": page["total"] > limit or len(rows) > edge_limit}

    def relationships(self, identifier, limit=100):
        if not 1 <= limit <= 200:
            raise ValueError("Invalid relationship limit")
        anchor = self.entity(identifier)
        if anchor is None:
            return None
        rows = self.query("""MATCH (a:Entity {company_id:$company,key:$key})-[r]-(b:Entity {company_id:$company})
            WHERE a.present=true AND b.present=true AND r.present=true
            RETURN properties(r) AS properties, type(r) AS kind, startNode(r).key AS start,
                endNode(r).key AS end, properties(b) AS neighbor ORDER BY r.key LIMIT $limit""",
            key=self._key(identifier), limit=limit + 1)
        nodes = {anchor["id"]: anchor}
        for row in rows[:limit]:
            neighbor = node_view(row["neighbor"])
            nodes[neighbor["id"]] = neighbor
        return {"nodes": list(nodes.values()), "edges": [edge_view(r) for r in rows[:limit]],
                "truncated": len(rows) > limit}

    def evaluate(self, now=None):
        now = time.time() if now is None else now
        # Read source evidence and all known paths in one query, including retired
        # paths. Missing evidence must stay distinguishable from explicit safety.
        result = self.query("""
            OPTIONAL MATCH (s:Source {company_id:$company})
            WITH collect(properties(s)) AS sources
            OPTIONAL MATCH p=(e:Employee {company_id:$company})-[:OWNS]->(a:Agent)
                -[:HAS_GRANT]->(g:AccessGrant)-[:ON]->(d:DataResource)
            WHERE all(n IN nodes(p) WHERE n.company_id=$company)
            RETURN sources, collect({employee_id:e.id,employee_name:e.name,employee_status:e.status,
                agent_id:a.id,grant_id:g.id,grant_status:g.status,grant_version:g.version,
                resource_id:d.id,classification:d.classification,scopes:g.scopes,
                present:all(n IN nodes(p) WHERE n.present=true) AND all(r IN relationships(p) WHERE r.present=true),
                directory_observed_at:e.last_observed_at,crm_observed_at:g.last_observed_at}) AS candidates
            """)[0]
        sources = {}
        for source in result["sources"]:
            last = source.get("last_success")
            sources[source["source"]] = {"status": source.get("status"), "last_success": last,
                "last_attempt": source.get("last_attempt"), "error": source.get("error"),
                "fresh": source.get("status") == "ok" and last is not None and 0 <= now - last <= 900}
        complete = all(sources.get(s, {}).get("fresh", False) for s in ("directory", "crm"))
        return {"coverage_complete": complete, "sources": sources, "observed_at": now,
                "candidates": [assess(r, complete) for r in result["candidates"] if r["grant_id"] is not None]}
