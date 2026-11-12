import asyncio
import json
import pytest
import httpx
from fastmcp import Client
from helio_security.mcp_read import read_server
from helio_security.mcp_actions import actions_server, create_mcp_playbooks
from test_playbooks_workbench import workbench


def test_workbench_action_mcp_roundtrip(workbench,monkeypatch):
    for key,value in workbench[2].items(): monkeypatch.setenv(key.upper(),value)
    async def run():
        async with Client(actions_server) as client:
            books=create_mcp_playbooks(client)
            assert set(books)>= {'workbench.restrict_app','workbench.pause_agent','workbench.disable_destination'}
            pb=books['workbench.restrict_app']
            before=await pb.read('app-renewal-tracker')
            await pb.run('app-renewal-tracker',before_state=before)
            assert await pb.verify('app-renewal-tracker')
            current=await pb.read('app-renewal-tracker')
            await pb.undo('app-renewal-tracker',before=before,before_state=current)
            assert await pb.verify('app-renewal-tracker',expected_state='public')
            dest=books['workbench.disable_destination']
            before=await dest.read('agent-renewal-digest/partner-api')
            await dest.run('agent-renewal-digest/partner-api',before_state=before)
            assert await dest.verify('agent-renewal-digest/partner-api',expected_state=False)
    asyncio.run(run())


def test_workbench_read_tools_are_bounded_and_do_not_expose_values(monkeypatch):
    from helio_security import mcp_read
    from helio_security.mcp_actions import _decode_mcp_result
    async def handler(request):
        if request.url.path=='/inventory':
            return httpx.Response(200,json={'apps':[{'id':'app-renewal-tracker','slug':'renewals'}]})
        if request.url.path=='/events':
            return httpx.Response(200,json={'items':[{'id':1,'subject_id':'app-renewal-tracker','kind':'public_view',
                'record_count':1,'fields':['email'],'secret':'hidden','customer_email':'private@customer.com'}],'has_more':False})
        assert 'authorization' not in request.headers
        return httpx.Response(200,json=[{'email':'private@customer.com','name':'Private Customer'}])
    original=httpx.AsyncClient
    monkeypatch.setattr(mcp_read.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    async def run():
        async with Client(read_server) as client:
            names={t.name for t in await client.list_tools()}
            assert {'probe_public_app','get_workbench_events'}<=names
            probe=_decode_mcp_result(await client.call_tool('probe_public_app',{'app_id':'app-renewal-tracker'}))
            assert probe['record_count']==1
            assert probe['fields']==['email','name']
            events=_decode_mcp_result(await client.call_tool('get_workbench_events',{'subject_id':'app-renewal-tracker'}))
            assert len(events['items'])==1
            assert 'Private' not in json.dumps(probe)+json.dumps(events)
            assert 'private@customer.com' not in json.dumps(probe)+json.dumps(events)
            assert 'hidden' not in json.dumps(events)
    asyncio.run(run())

@pytest.mark.parametrize('tool,identifier,kind',[('get_entity','app-renewal-tracker','App'),('get_neighbors','agent-lead-enrichment','Agent'),('get_entity','service:renewal-tracker','ServiceAccount')])
def test_workbench_ids_resolve_to_graph_entities(monkeypatch,tool,identifier,kind):
    from helio_security import mcp_read
    from helio_security.reader import entity_id
    encoded=entity_id(json.dumps(['helio',kind,identifier],separators=(',',':')))
    def handler(request):
        return httpx.Response(200,json={'found':True,'id':encoded}) if encoded in request.url.path else httpx.Response(404)
    original=httpx.AsyncClient
    monkeypatch.setattr(mcp_read.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    async def run():
        async with Client(read_server) as client:
            from helio_security.mcp_actions import _decode_mcp_result
            result=_decode_mcp_result(await client.call_tool(tool,{'entity_id':identifier}))
            assert result['found'] is True
    asyncio.run(run())
