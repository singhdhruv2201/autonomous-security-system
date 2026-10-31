import secrets
import time

from fastapi.testclient import TestClient

from helio_security.webhooks import create_app
from test_activity_recovery import recovery, auth
from test_crm_activity import events
from test_graph import serve


def test_activity_api_requires_read_credentials_and_bounds_filters(tmp_path, recovery):
    _, crm, tokens = recovery
    crm.list_customers(tokens['agent'])
    viewer, operator, key = [secrets.token_urlsafe(32) for _ in range(3)]
    app = create_app(tmp_path/'inbox.sqlite3',{'crm-1':{'secret':key,'company_id':'helio','source':'helio-crm'}},
                     read_tokens={'viewer':viewer,'operator':operator})
    with TestClient(app) as client:
        app.state.inbox.accept(events(crm)[0],time.time())
        app.state.inbox.process_one()
        for route in ('/activity','/activity/health'):
            assert client.get(route).status_code == 401
            assert client.get(route,headers=auth(key)).status_code == 401
            assert client.get(route,headers=auth(viewer)).status_code == 200
        response = client.get('/activity',headers=auth(operator)).json()
        assert response['total'] == 1 and response['company_id'] == 'helio'
        assert viewer not in str(response)
        for query in ('limit=201','before=-1','outcome=invalid','actor_id='+('x'*201),'snapshot=-1'):
            assert client.get('/activity?'+query,headers=auth(viewer)).status_code == 422
        assert client.get('/activity?company_id=other',headers=auth(viewer)).json()['company_id'] == 'helio'


def test_security_api_proxies_activity_with_company_check(tmp_path, recovery):
    from unittest.mock import Mock
    from helio_security.activity_client import ActivityClient
    from helio_security.api import create_app as security_app
    from helio_security.settings import Settings
    _, crm, tokens = recovery
    crm.list_customers(tokens['agent'])
    settings = Settings(operator_token=secrets.token_urlsafe(32),viewer_token=secrets.token_urlsafe(32),
                        observer_token=secrets.token_urlsafe(32),db_path=tmp_path/'security.sqlite3',sync_interval=0)
    key = secrets.token_urlsafe(32)
    ingest = create_app(tmp_path/'inbox.sqlite3',{'crm-1':{'secret':key,'company_id':'helio','source':'helio-crm'}},
                        read_tokens={'viewer':settings.viewer_token,'operator':settings.operator_token})
    graph = Mock(company_id='helio')
    with serve(ingest) as url:
        ingest.state.inbox.accept(events(crm)[0],time.time())
        ingest.state.inbox.process_one()
        backend = ActivityClient(url,settings.viewer_token)
        with TestClient(security_app(settings,graph=graph,reader=Mock(),start_worker=False,activity_client=backend)) as client:
            assert client.get('/activity').status_code == 401
            assert client.get('/activity/health').status_code == 401
            assert client.get('/activity',headers=auth(settings.viewer_token)).json()['total'] == 1
            assert client.get('/activity?limit=201',headers=auth(settings.viewer_token)).status_code == 422
        import pytest
        with pytest.raises(Exception,match='scope'):
            backend.get('/activity','another-company')


def test_dashboard_forwards_only_bounded_activity_filters():
    from unittest.mock import Mock
    from helio_dashboard.api import create_app as dashboard_app
    gateway = Mock()
    gateway.request.return_value = (200, {'items':[],'enabled':True})
    with TestClient(dashboard_app(gateway)) as client:
        assert client.get('/api/activity?limit=20&outcome=denied&actor_id=agent%3Asales-research').status_code == 200
        path = gateway.request.call_args.args[0]
        assert path.startswith('/activity?') and 'outcome=denied' in path and 'agent%3Asales-research' in path
        assert client.get('/api/activity?limit=201').status_code == 422
        assert client.get('/api/activity/health').status_code == 200
