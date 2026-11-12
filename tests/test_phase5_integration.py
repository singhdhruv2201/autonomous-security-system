"""Full local loop: HTTP sources -> Neo4j -> observer -> cases -> MCP fixes -> Slack callbacks."""
import asyncio
import secrets
import time
from unittest.mock import AsyncMock
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI, Request
from helio_simulator.api import create_app as directory_app
from helio_crm.api import create_app as crm_app
from helio_workbench.api import create_app as workbench_app
from helio_workbench.store import WorkbenchStore
from helio_security.platform_store import PlatformStore
from helio_security.reader import GraphReader
from helio_security.worker import ObservationWorker
from helio_security.agent_worker import AgentWorker
from helio_security.mcp_runtime import mcp_connections
from helio_security.mcp_actions import create_mcp_playbooks
from helio_security.slack_app import SlackActions, SlackConfig, SlackNotifier
from test_graph import graph, serve
from test_workflow import make_deps
from test_slack_app import FakeClient, click

@pytest.mark.neo4j
@pytest.mark.parametrize('scenario',['public','external'])
def test_workbench_scenario_full_loop(graph,directory,tmp_path,monkeypatch,scenario):
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent','observer','actions','renewal_tracker','support_lookup','lead_enrichment','renewal_digest')}
    wb=WorkbenchStore(tmp_path/'wb.db')
    receiver=FastAPI(); received=[]
    @receiver.post('/collect')
    async def collect(request:Request):
        received.append(await request.json())
        return {'ok':True}
    with serve(directory_app(directory.path)) as durl, serve(crm_app(tmp_path/'crm.db',tokens,directory.employees)) as curl, serve(receiver) as rurl:
        app=workbench_app(store=wb,crm_url=curl,observer_token=tokens['observer'],action_token=tokens['actions'],user_token=tokens['maya'],
            crm_tokens={'service:renewal-tracker':tokens['renewal_tracker'],'service:support-lookup':tokens['support_lookup'],
                        'agent:lead-enrichment':tokens['lead_enrichment'],'agent:renewal-digest':tokens['renewal_digest']})
        with serve(app) as wurl:
            for key,value in {'WORKBENCH_URL':wurl,'WORKBENCH_OBSERVER_TOKEN':tokens['observer'],'WORKBENCH_ACTION_TOKEN':tokens['actions']}.items():
                monkeypatch.setenv(key,value)
            monkeypatch.setenv('AWASI_APPROVED_DESTINATIONS','partner-api')
            deps=make_deps(tmp_path)
            platform=PlatformStore(deps.store.path);platform.initialize();deps.platform_store=platform
            reader=GraphReader(graph)
            # Keep graph nodes isolated while exercising the demo's Helio tenant.
            scoped_graph=SimpleNamespace(company_id='helio',apply=graph.apply,fail=graph.fail)
            observer=ObservationWorker(platform,scoped_graph,reader,durl,curl,tokens['observer'],workbench_url=wurl,workbench_token=tokens['observer'],interval=0)
            def observe():
                platform.enqueue('helio','test','manual',time.time())
                assert observer.run_once()
            config=SlackConfig(channel='C123',default_user='U123')
            client=FakeClient();notifier=SlackNotifier(deps.store,client,config);deps.notifier=notifier
            worker=AgentWorker(platform,deps.store,deps,company_id='helio')
            handler=SlackActions(deps.store,notifier,config,team_id='T123')
            async def run():
                async with mcp_connections() as (_,actions):
                    deps.playbooks=create_mcp_playbooks(actions)
                    observe()
                    await worker.run_once()
                    assert platform.findings('helio')['items']==[]
                    assert deps.store.active_cases('helio')==[]
                    assert client.posts==[]
                    async with httpx.AsyncClient() as http:
                        if scenario=='public':
                            res=await http.patch(wurl+'/apps/app-renewal-tracker',headers={'Authorization':'Bearer '+tokens['actions']},json={'expected_version':1,'visibility':'public'})
                        else:
                            res=await http.post(wurl+'/agents/agent-lead-enrichment/destinations',headers={'Authorization':'Bearer '+tokens['maya']},json={'host':'receiver','url':rurl+'/collect'})
                            assert res.status_code==200
                            res=await http.post(wurl+'/agents/agent-lead-enrichment/run',headers={'Authorization':'Bearer '+tokens['maya']})
                        assert res.status_code==200
                    observe()
                    await worker.run_once();await notifier.flush()
                    findings=platform.findings('helio',status='open')['items']
                    assert len(findings)==1
                    cases=deps.store.active_cases('helio');assert len(cases)==1
                    cid=cases[0]['id']
                    if scenario=='public':
                        assert 'Luca Rossi' in client.posts[0]['text']
                        choices=[('workbench_public','restrict'),('workbench_restricted','keep_restricted')]
                    else:
                        assert len(received)==1 and len(received[0])==6
                        assert wb.get_agent('agent-lead-enrichment')['status']=='paused'
                        assert "can't recall" in client.posts[0]['text']
                        choices=[('workbench_destination','remove_connection')]
                    for key,choice in choices:
                        ts=notifier.slack_store.message(cid,key)['ts']
                        await handler.handle(ack=AsyncMock(),body=click(cid,choice=choice,key=key,ts=ts))
                        await worker.run_once();await notifier.flush()
                    assert deps.store.get_case(cid)['status']=='resolved'
                    assert all(a['verified_at'] is not None for a in deps.store.get_actions(cid))
                    observe()
                    assert platform.findings('helio',status='open')['items']==[]
                    if scenario=='public':
                        async with httpx.AsyncClient() as http: assert (await http.get(wurl+'/public/renewals')).status_code==404
                    else:
                        assert wb.get_agent('agent-lead-enrichment')['status']=='active'
                        assert wb.list_destinations('agent-lead-enrichment')[0]['enabled'] is False
                    assert 'contact@northstar.example' not in str(client.posts)
                    assert tokens['observer'] not in str(deps.store.get_case_full(cid))
            asyncio.run(run())
