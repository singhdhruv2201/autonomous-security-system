"""Local operator interface for the security graph, before dashboard and Slack."""

import argparse
import json
import os

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, DriverError

from .graph import GraphStore
from .sync import sync_all


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "status", "risks"])
    command = parser.parse_args().command
    try:
        with GraphDatabase.driver(os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"),
                auth=(os.getenv("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]),
                connection_timeout=5, max_transaction_retry_time=10) as driver:
            graph = GraphStore(driver)
            if command == "sync":
                graph.initialize()
                result = sync_all(graph, os.getenv("HELIO_DIRECTORY_URL", "http://127.0.0.1:8000"),
                    os.getenv("CRM_URL", "http://127.0.0.1:8001"), os.getenv("CRM_OBSERVER_TOKEN"))
                print(json.dumps({"sync": result, **graph.status()}, indent=2))
                return 0 if all(v == "ok" for v in result.values()) else 1
            print(json.dumps(graph.status() if command == "status" else graph.risks(), indent=2))
            return 0
    except (KeyError, ValueError, OSError, Neo4jError, DriverError):
        print(json.dumps({"error": "Graph unavailable or configuration invalid; check local services and credentials."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
