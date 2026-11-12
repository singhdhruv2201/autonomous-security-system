"""Transactional, source-owned graph projection; never changes customer systems."""

import json
import time
from collections import defaultdict

NODE_KINDS = {"directory": {"Employee", "Team"},
              "crm": {"App", "Agent", "ServiceAccount", "AppAccount", "DataResource", "AccessGrant"},
              "workbench": {"App", "Agent", "Connection", "Destination", "Audience"}}
EDGE_KINDS = {"directory": {"MEMBER_OF"},
              "crm": {"OWNS", "USES", "STORES", "IDENTITY_OF", "HAS_GRANT", "ON"},
              "workbench": {"OWNS", "ACTS_AS", "EXPOSED_TO", "HAS_CONNECTION", "TO"}}


class GraphStore:
    def __init__(self, driver, company_id="helio"):
        self.driver = driver
        self.company_id = company_id

    def key(self, *parts):
        return json.dumps([self.company_id, *parts], separators=(",", ":"))

    def initialize(self):
        for label in ("Entity", "Source"):
            self.driver.execute_query(
                f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:{label}) REQUIRE n.key IS UNIQUE",
                database_="neo4j")

    def _lock_source(self, tx, source, observed_at):
        row = tx.run("""MERGE (s:Source {key:$key})
            ON CREATE SET s.company_id=$company, s.source=$source
            SET s.lock_version=coalesce(s.lock_version,0)+1
            RETURN s.last_attempt AS previous""", key=self.key("Source", source),
            company=self.company_id, source=source).single()
        return row["previous"] is None or observed_at > row["previous"]

    def apply(self, snapshot, observed_at):
        source = snapshot.source
        if source not in NODE_KINDS:
            raise ValueError("Unknown source")
        nodes, edges = defaultdict(list), defaultdict(list)
        for node in snapshot.nodes:
            if node["kind"] not in NODE_KINDS[source]:
                raise ValueError("Invalid node kind")
            nodes[node["kind"]].append({"key": self.key(node["kind"], node["id"]),
                                        "properties": node["properties"]})
        for edge in snapshot.edges:
            if edge["kind"] not in EDGE_KINDS[source]:
                raise ValueError("Invalid edge kind")
            edges[edge["kind"]].append({"start": self.key(*edge["start"]), "end": self.key(*edge["end"]),
                "key": self.key(source, edge["kind"], edge["start"], edge["end"])})

        def transaction(tx):
            if not self._lock_source(tx, source, observed_at):
                return False
            params = {"company": self.company_id, "source": source, "at": observed_at}
            # Retire only after a complete, validated observation. Failure rolls
            # back retirement as well as upserts. Missing does not mean revoked.
            tx.run("MATCH (n:Entity {company_id:$company,source:$source}) SET n.present=false", **params).consume()
            tx.run("MATCH (:Entity {company_id:$company})-[r {source:$source}]->() SET r.present=false", **params).consume()
            for kind, rows in nodes.items():
                tx.run(f"""UNWIND $rows AS row
                    MERGE (n:Entity:{kind} {{key:row.key}})
                    ON CREATE SET n.first_seen=$at
                    SET n += row.properties, n.kind=$kind, n.company_id=$company,
                        n.source=$source, n.present=true, n.last_observed_at=$at""",
                    rows=rows, kind=kind, **params).consume()
            for kind, rows in edges.items():
                result = tx.run(f"""UNWIND $rows AS row
                    MATCH (a:Entity {{key:row.start}}), (b:Entity {{key:row.end}})
                    MERGE (a)-[r:{kind} {{key:row.key}}]->(b)
                    ON CREATE SET r.first_seen=$at
                    SET r.company_id=$company, r.source=$source, r.present=true, r.last_observed_at=$at
                    RETURN count(r) AS count""", rows=rows, **params).single()
                if result["count"] != len(rows):
                    raise ValueError("Missing graph endpoint; sync the directory first")
            tx.run("""MATCH (s:Source {key:$key}) SET s.status='ok', s.last_attempt=$at,
                s.last_success=$at, s.error=null""", key=self.key("Source", source), at=observed_at).consume()
            return True

        with self.driver.session(database="neo4j") as session:
            return session.execute_write(transaction)

    def fail(self, source, error, observed_at):
        if source not in NODE_KINDS:
            raise ValueError("Unknown source")
        # Only controlled error codes may persist. Never serialize an exception.
        allowed = {"unavailable", "invalid_payload", "oversized", "missing_credential", "import_failed"}
        if error not in allowed and not (error.startswith("http_") and error[5:].isdigit()):
            error = "import_failed"

        def transaction(tx):
            if not self._lock_source(tx, source, observed_at):
                return False
            tx.run("""MATCH (s:Source {key:$key}) SET s.status='error', s.last_attempt=$at,
                s.error=$error""", key=self.key("Source", source), at=observed_at, error=error).consume()
            return True

        with self.driver.session(database="neo4j") as session:
            return session.execute_write(transaction)

    def status(self, now=None):
        now = time.time() if now is None else now
        rows = self.driver.execute_query("""MATCH (s:Source {company_id:$company})
            RETURN s.source AS source, s.status AS status, s.last_success AS last_success,
                s.last_attempt AS last_attempt, s.error AS error""",
            company=self.company_id, database_="neo4j").records
        sources = {}
        for row in rows:
            entry = dict(row)
            entry["fresh"] = (entry["status"] == "ok" and entry["last_success"] is not None
                              and 0 <= now - entry["last_success"] <= 900)
            sources[entry.pop("source")] = entry
        for source in NODE_KINDS:
            sources.setdefault(source, {"status": "never_synced", "fresh": False,
                                        "last_success": None, "last_attempt": None, "error": None})
        counts = self.driver.execute_query("""MATCH (n:Entity {company_id:$company,present:true})
            RETURN n.kind AS kind, count(*) AS count""", company=self.company_id, database_="neo4j").records
        return {"company_id": self.company_id, "sources": sources,
                "counts": {r["kind"]: r["count"] for r in counts},
                "coverage_complete": all(s["fresh"] for s in sources.values())}
