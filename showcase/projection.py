"""Project normalized Helio snapshots into a disposable, in-memory graph."""
from collections import defaultdict

from helio_security.rules import RULE_ID, RULES

PUBLIC_RULE = "public_app_confidential_data"
EXTERNAL_RULE = "agent_unapproved_destination"
APPROVED_DESTINATIONS = frozenset({"partner-api"})


class Projection:
    company_id = "helio"

    def __init__(self):
        self.snapshots = {}
        self.sources = {}

    def apply(self, snapshot, observed_at):
        self.snapshots[snapshot.source] = snapshot
        self.sources[snapshot.source] = {
            "status": "ok", "last_attempt": observed_at, "last_success": observed_at,
        }
        return True

    def fail(self, source, error, observed_at):
        self.sources[source] = {
            "status": "error", "error": error, "last_attempt": observed_at,
            "last_success": self.sources.get(source, {}).get("last_success"),
        }

    def graph(self):
        return ObservedGraph(self.snapshots.values(), self.sources)

    def evaluate(self, now):
        graph = self.graph()
        evaluations = []
        for rule in RULES.values():
            complete = all(graph.sources.get(source, {}).get("status") == "ok"
                           for source in rule.sources)
            candidates = {}
            rank = {"resolved": 0, "unknown": 1, "open": 2}
            for path in graph.paths(rule.id):
                candidate = rule.assess(graph.evidence(rule.id, path), complete, APPROVED_DESTINATIONS)
                key = tuple(candidate["identity"])
                if key not in candidates or rank[candidate["state"]] > rank[candidates[key]["state"]]:
                    candidates[key] = candidate
            evaluations.append({
                "rule": rule.id, "coverage_complete": complete, "sources": graph.sources,
                "observed_at": now, "candidates": list(candidates.values()),
            })
        return {"evaluations": evaluations}


class ObservedGraph:
    def __init__(self, snapshots, sources):
        self.sources = {source: dict(state) for source, state in sources.items()}
        self.nodes = {}
        self.edges = []
        self.links = defaultdict(list)
        for snapshot in snapshots:
            for node in snapshot.nodes:
                props = {key: value for key, value in node["properties"].items() if key != "email"}
                self.nodes[(node["kind"], node["id"])] = {
                    **props, "kind": node["kind"], "source": snapshot.source,
                }
        for snapshot in snapshots:
            for edge in snapshot.edges:
                start, end = tuple(edge["start"]), tuple(edge["end"])
                if start in self.nodes and end in self.nodes:
                    self.links[(start, edge["kind"])].append(end)
                    self.edges.append((start, edge["kind"], end))

    def paths(self, rule, *, target=None, include_unconnected=False):
        for owner, kind, asset in self.edges:
            if kind != "OWNS" or (target and asset[1] != target):
                continue
            source = self.nodes[asset]["source"]
            if rule == RULE_ID and (source, asset[0]) == ("crm", "Agent"):
                principals = [asset]
            elif source == "workbench" and (
                (rule, asset[0]) in {(PUBLIC_RULE, "App"), (EXTERNAL_RULE, "Agent")}
            ):
                principals = self.links[asset, "ACTS_AS"]
            else:
                continue

            connections = [conn for conn in self.links[asset, "HAS_CONNECTION"]
                           if self.links[conn, "TO"]]
            if rule != EXTERNAL_RULE or (include_unconnected and not connections):
                connections = [None]
            for principal in principals:
                for grant in self.links[principal, "HAS_GRANT"]:
                    for resource in self.links[grant, "ON"]:
                        for conn in connections:
                            yield owner, asset, principal, grant, resource, conn

    def evidence(self, rule, path):
        owner, asset, principal, grant, resource = (self.nodes[node] for node in path[:5])
        common = dict(
            employee_id=owner["id"], employee_name=owner["name"], employee_status=owner["status"],
            grant_id=grant["id"], grant_status=grant["status"], grant_version=grant["version"],
            resource_id=resource["id"], classification=resource["classification"],
            scopes=grant["scopes"], record_count=resource["record_count"], present=True,
            principal_status=principal["principal_status"],
            directory_observed_at=self.sources["directory"]["last_success"],
            crm_observed_at=self.sources["crm"]["last_success"],
        )
        if rule == RULE_ID:
            return {**common, "agent_id": asset["id"]}
        workbench = dict(
            asset_name=asset["name"], version=asset["version"], principal_id=principal["id"],
            workbench_observed_at=self.sources["workbench"]["last_success"], dependents=0,
        )
        if rule == PUBLIC_RULE:
            return {**common, **workbench, "app_id": asset["id"],
                    **{key: asset[key] for key in ("visibility", "slug", "anonymous_views_1h", "last_anonymous_view_at")}}
        conn = self.nodes.get(path[5], {})
        return {
            **common, **workbench, "agent_id": asset["id"], "agent_status": asset["status"],
            "connection_id": conn.get("id"), "host": conn.get("host"),
            "enabled": conn.get("enabled"), "connection_version": conn.get("version"),
            "records_sent_1h": conn.get("records_sent_1h", 0),
            "records_sent_total": conn.get("records_sent_total", 0),
            "last_sent_at": conn.get("last_sent_at"),
        }

    def focus(self, rule, target):
        path = next(self.paths(rule, target=target, include_unconnected=True), None)
        if not path:
            return {}, {"nodes": [], "edges": []}
        owner, asset, principal, grant, resource, conn = path
        ordered = list(dict.fromkeys([owner, asset, principal, grant, resource]))
        ordered.extend(self.links[asset, "EXPOSED_TO"])
        if conn:
            ordered.extend([conn, *self.links[conn, "TO"]])
        ids = {key: f"{key[0]}/{key[1]}" for key in ordered}
        return self.evidence(rule, path), {
            "nodes": [{**self.nodes[key], "source_id": key[1], "id": ids[key]} for key in ordered],
            "edges": [
                {"from": ids[start], "to": ids[end], "kind": kind}
                for start, kind, end in self.edges if start in ids and end in ids
                and not (kind == "OWNS" and end != asset)
            ],
        }
