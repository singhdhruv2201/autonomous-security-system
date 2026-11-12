"""Versioned workbench containment; never retries an ambiguous mutation."""
from urllib.parse import quote
import httpx

from .playbook_errors import PlaybookError, UnknownOutcome


class WorkbenchPlaybook:
    reversible = True

    def __init__(self, identifier, collection, state_field, desired):
        self.id, self.collection = identifier, collection
        self.state_field, self.desired = state_field, desired

    def path(self, target):
        if self.collection == 'destinations':
            try:
                agent, host = target.split('/', 1)
            except ValueError:
                raise PlaybookError('Invalid destination target') from None
            return f'/agents/{quote(agent, safe="")}/destinations/{quote(host, safe="")}'
        return f'/{self.collection}/{quote(target, safe="")}'

    async def read(self, target, workbench_url, workbench_observer_token, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(workbench_url.rstrip('/')+'/inventory',
                    headers={'Authorization':f'Bearer {workbench_observer_token}'})
            if response.status_code != 200:
                raise PlaybookError('Workbench inventory unavailable')
            inventory = response.json()
            if inventory.get('complete') is not True or inventory.get('company_id') != 'helio':
                raise PlaybookError('Invalid workbench inventory')
            if self.collection == 'destinations':
                agent, host = target.split('/', 1)
                asset = next(a for a in inventory['agents'] if a['id'] == agent)
                item = next(d for d in asset['destinations'] if d['host'] == host)
            else:
                item = next(a for a in inventory[self.collection] if a['id'] == target)
            state = {key:item[key] for key in (self.state_field,'version','slug') if key in item}
            if self.collection == 'agents':
                state['enabled_destinations'] = [d['host'] for d in item['destinations'] if d['enabled']]
            return state
        except PlaybookError:
            raise
        except Exception:
            raise PlaybookError('Workbench target unavailable') from None

    async def _write(self, target, desired, before_state, workbench_url, workbench_observer_token, workbench_action_token):
        before = before_state or await self.read(target,workbench_url,workbench_observer_token)
        body = {'expected_version':before['version'], self.state_field:desired}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.patch(workbench_url.rstrip('/')+self.path(target),json=body,
                    headers={'Authorization':f'Bearer {workbench_action_token}'})
            if response.status_code == 200:
                return {'status':'executed'}
            if response.status_code < 500:
                raise PlaybookError('Workbench mutation rejected or version changed')
        except httpx.TransportError:
            pass
        raise UnknownOutcome('Workbench mutation response unavailable')

    async def run(self, target, workbench_url, workbench_observer_token, workbench_action_token, before_state=None, **kwargs):
        return await self._write(target,self.desired,before_state,workbench_url,workbench_observer_token,workbench_action_token)

    async def undo(self, target, before, workbench_url, workbench_observer_token, workbench_action_token, before_state=None, **kwargs):
        return await self._write(target,before[self.state_field],before_state,workbench_url,workbench_observer_token,workbench_action_token)

    async def verify(self, target, workbench_url, workbench_observer_token, expected_state=None, **kwargs):
        desired = self.desired if expected_state is None else expected_state
        current = await self.read(target,workbench_url,workbench_observer_token)
        if current[self.state_field] != desired:
            return False
        if self.collection == 'apps' and desired != 'public':
            async with httpx.AsyncClient(timeout=5,follow_redirects=False) as client:
                response = await client.get(workbench_url.rstrip('/')+'/public/'+quote(current['slug'],safe=''))
            return response.status_code == 404
        return True


WORKBENCH_PLAYBOOKS = {pb.id: pb for pb in (
    WorkbenchPlaybook('workbench.restrict_app','apps','visibility','company'),
    WorkbenchPlaybook('workbench.pause_agent','agents','status','paused'),
    WorkbenchPlaybook('workbench.disable_destination','destinations','enabled',False),
)}
