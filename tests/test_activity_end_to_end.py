"""Real HTTP CRM -> push/pull -> inbox -> API -> dashboard, temporary state only."""
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from unittest.mock import Mock

from helio_crm.api import create_app as crm_app
from helio_security.webhooks import create_app as ingest_app
from helio_security.api import create_app as security_app
from helio_security.activity_client import ActivityClient
from helio_security.activity_worker import ActivityWorker, RecoveryClient
from helio_security.settings import Settings
from helio_dashboard.api import create_app as dashboard_app
from helio_dashboard.gateway import Gateway
from test_graph import serve


def get(url, token=None):
    req = urllib.request.Request(url,headers={'Authorization':'Bearer '+token} if token else {})
    with urllib.request.urlopen(req,timeout=5) as response:
        return json.load(response)


def until(check, timeout=10):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Activity pipeline did not converge')


def test_live_activity_and_denial_reach_dashboard_and_recover_after_outage(tmp_path,directory,monkeypatch):
    tokens = {name:secrets.token_urlsafe(32) for name in ('admin','maya','agent','recovery')}
    viewer,operator,key = [secrets.token_urlsafe(32) for _ in range(3)]
    inbox_path,crm_path = tmp_path/'inbox.sqlite3',tmp_path/'crm.sqlite3'
    keys = {'crm-1':{'secret':key,'company_id':'helio','source':'helio-crm'}}
    app = ingest_app(inbox_path,keys,read_tokens={'viewer':viewer,'operator':operator})
    with serve(app) as ingest_url:
        monkeypatch.setenv('CRM_WEBHOOK_ENABLED','1')
        monkeypatch.setenv('CRM_WEBHOOK_URL',ingest_url+'/webhooks/crm/activity')
        monkeypatch.setenv('CRM_WEBHOOK_ALLOW_LOCAL','1')
        monkeypatch.setenv('CRM_WEBHOOK_SECRET',key)
        with serve(crm_app(crm_path,tokens,directory.employees,activity_enabled=True)) as crm_url:
            worker = ActivityWorker(app.state.inbox,RecoveryClient(crm_url,tokens['recovery']))
            worker.start()
            try:
                settings = Settings(operator_token=operator,viewer_token=viewer,observer_token=secrets.token_urlsafe(32),db_path=tmp_path/'platform.sqlite3',sync_interval=0)
                with serve(security_app(settings,graph=Mock(company_id='helio'),reader=Mock(),start_worker=False,
                        activity_client=ActivityClient(ingest_url,viewer))) as api_url:
                    with serve(dashboard_app(Gateway(api_url,viewer,operator))) as dashboard_url:
                        get(crm_url+'/customers',tokens['agent'])
                        try:
                            get(crm_url+'/customers')
                        except urllib.error.HTTPError as error:
                            assert error.code == 401
                            error.close()
                        page = until(lambda: (p if len(p['items'])==2 and all(len(r['stages'])==4 for r in p['items']) else None)
                                     if (p:=get(dashboard_url+'/api/activity')) else None)
                        assert {r['outcome'] for r in page['items']} == {'allowed','denied'}
                        assert all(r['scan_status']=='not_run' for r in page['items'])
                        assert all(token not in json.dumps(page) for token in [*tokens.values(),viewer,operator,key])
                        assert get(dashboard_url+'/api/activity?outcome=denied')['total'] == 1
            finally:
                worker.stop()
    # Both services restart with retained SQLite. No push worker: recovery must repair the outage.
    monkeypatch.setenv('CRM_WEBHOOK_ENABLED','0')
    with serve(crm_app(crm_path,tokens,directory.employees,activity_enabled=True)) as crm_url:
        get(crm_url+'/customers',tokens['agent'])
        monkeypatch.setenv('AWASI_ACTIVITY_WORKER_ENABLED','1')
        monkeypatch.setenv('CRM_URL',crm_url)
        monkeypatch.setenv('CRM_RECOVERY_TOKEN',tokens['recovery'])
        restarted = ingest_app(inbox_path,keys,read_tokens={'viewer':viewer,'operator':operator})
        with serve(restarted) as ingest_url:
            until(lambda: get(ingest_url+'/activity',viewer)['total']==3)
            until(lambda: get(ingest_url+'/activity/health',viewer)['coverage_complete'])
            with sqlite3.connect(inbox_path) as db:
                assert db.execute('SELECT count(*) FROM inbox').fetchone()[0] == 12
                assert db.execute('SELECT count(*) FROM normalized_events').fetchone()[0] == 12


def test_enforced_secret_decision_arrives_as_dashboard_finding_over_signed_webhook(tmp_path,directory,monkeypatch):
    from helio_crm.policies import PolicyConfig
    from helio_security.policy_worker import PolicyWorker
    from helio_security.platform_store import PlatformStore
    tokens={name:secrets.token_urlsafe(32) for name in ('admin','maya','agent','recovery','observer')}
    viewer,operator,key=[secrets.token_urlsafe(32) for _ in range(3)]
    app=ingest_app(tmp_path/'inbox.db',{'crm-1':dict(secret=key,company_id='helio',source='helio-crm')},read_tokens=dict(viewer=viewer,operator=operator))
    with serve(app) as ingest_url:
        for name,value in dict(CRM_WEBHOOK_ENABLED='1',CRM_WEBHOOK_URL=ingest_url+'/webhooks/crm/activity',CRM_WEBHOOK_ALLOW_LOCAL='1',CRM_WEBHOOK_SECRET=key).items():monkeypatch.setenv(name,value)
        with serve(crm_app(tmp_path/'crm.db',tokens,directory.employees,activity_enabled=True,policy_config=PolicyConfig(content_mode='enforce'))) as crm_url:
            recovery=ActivityWorker(app.state.inbox,RecoveryClient(crm_url,tokens['recovery']));recovery.start()
            platform=PlatformStore(tmp_path/'platform.db');platform.initialize()
            policies=PolicyWorker(platform,'helio',activity_client=ActivityClient(ingest_url,viewer),crm_client=RecoveryClient(crm_url,tokens['observer']));policies.start()
            try:
                settings=Settings(operator_token=operator,viewer_token=viewer,observer_token=tokens['observer'],db_path=platform.path,sync_interval=0)
                graph=Mock(company_id='helio')
                graph.status.return_value={'company_id':'helio','sources':{},'coverage_complete':False}
                with serve(security_app(settings,graph=graph,reader=Mock(),store=platform,start_worker=False)) as security_url:
                    with serve(dashboard_app(Gateway(security_url,viewer,operator))) as dashboard_url:
                        secret='sk_live_'+'q'*24
                        req=urllib.request.Request(crm_url+'/customers/cust-northstar',method='PATCH',headers={'Authorization':'Bearer '+tokens['maya'],'Content-Type':'application/json'},data=json.dumps({'expected_version':1,'notes':secret}).encode())
                        try: urllib.request.urlopen(req,timeout=5)
                        except urllib.error.HTTPError as error: assert error.code==403;error.close()
                        else: raise AssertionError('Secret write was not blocked')
                        result=until(lambda:(p if p['items'] else None) if (p:=get(security_url+'/findings',viewer)) else None)
                        assert len(result['items'])==1
                        finding=get(dashboard_url+'/api/findings/'+result['items'][0]['id'])
                        assert finding['rule']=='sensitive_content' and finding['assessment']=='current'
                        assert secret not in json.dumps(result)
                        assert get(crm_url+'/customers/cust-northstar',tokens['agent'])['notes']==''
                        with sqlite3.connect(tmp_path/'crm.db') as db:
                            until(lambda:db.execute("SELECT count(*) FROM activity_outbox WHERE status='delivered'").fetchone()[0]>=4)
            finally:
                policies.stop();recovery.stop()
