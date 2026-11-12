import secrets
import time

import pytest

from helio_crm.store import CRMStore
from helio_security.models import normalize
from helio_security.reader import GraphReader
from test_graph import graph, query

pytestmark = pytest.mark.neo4j


def test_recovery_reader_grant_is_observed_as_its_own_resource(graph, directory, tmp_path):
    tokens = {name:secrets.token_urlsafe(32) for name in ('admin','maya','agent','observer','recovery')}
    store = CRMStore(tmp_path/'crm.sqlite3',activity_enabled=True)
    store.initialize(directory.employees(),tokens)
    graph.apply(normalize('directory',directory.snapshot()),time.time())
    graph.apply(normalize('crm',store.inventory(tokens['observer'])),time.time())
    rows = query(graph,"MATCH (g:AccessGrant {company_id:$company,id:'grant-activity-reader'})-[:ON]->(r:DataResource) RETURN r.id AS resource,g.scopes AS scopes")
    assert len(rows)==1 and rows[0]['resource']=='crm-activity' and rows[0]['scopes']==['events:read']
    assert all(c['state'] != 'open' for c in GraphReader(graph).evaluate(time.time())['candidates'])
