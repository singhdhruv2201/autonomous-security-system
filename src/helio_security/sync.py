"""Synchronize independent sources; one outage must not hide another's updates."""

import time

from neo4j.exceptions import Neo4jError

from .connectors import ConnectorError, fetch_snapshot


def sync_all(graph, directory_url, crm_url, observer_token, *, fetch=fetch_snapshot, clock=time.time):
    results = {}
    for source, url, token in (("directory", directory_url, None), ("crm", crm_url, observer_token)):
        # Timestamp the start of observation, not completion; a slow old request
        # must not overwrite a more recent snapshot.
        observed_at = clock()
        try:
            snapshot = fetch(source, url, token)
            applied = graph.apply(snapshot, observed_at)
            results[source] = "ok" if applied else "skipped_older"
        except ConnectorError as error:
            graph.fail(source, str(error), observed_at)
            results[source] = str(error)
        except (ValueError, Neo4jError):
            graph.fail(source, "import_failed", observed_at)
            results[source] = "import_failed"
    return results
