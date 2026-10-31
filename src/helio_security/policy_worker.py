"""Independent identity publication and durable activity policy consumption."""
import json
import time
import threading
import logging
import urllib.request

from .activity_worker import RecoveryClient, RecoveryError
from .connectors import NoRedirect, fetch_snapshot


class IdentityPublisher:
    def __init__(self,directory_url,crm_url,token,*,clock=time.time):
        self.directory_url,self.clock=directory_url,clock
        self.crm=RecoveryClient(crm_url,token)

    def run_once(self):
        observed=self.clock()
        snapshot=fetch_snapshot('directory',self.directory_url)
        employees=[{'id':n['id'],'status':n['properties']['status']} for n in snapshot.nodes if n['kind']=='Employee']
        payload=dict(company_id='helio',source='directory',version=int(observed*1000000),observed_at=observed,
                     expires_at=observed+120,complete=True,employees=employees)
        req=urllib.request.Request(self.crm.url+'/identity-projection',method='POST',
            headers={'Authorization':'Bearer '+self.crm.token,'Content-Type':'application/json'},data=json.dumps(payload).encode())
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=5) as response:
            body=response.read(4097)
            if len(body)>4096 or json.loads(body).get('version')!=payload['version']:
                raise RecoveryError('invalid_publication')
        return payload['version']


class PolicyWorker:
    def __init__(self,store,company,*,activity_client=None,publisher=None,crm_client=None,clock=time.time):
        self.store,self.company,self.activity_client=store,company,activity_client
        self.publisher,self.crm_client,self.clock=publisher,crm_client,clock
        self.stop_event=threading.Event()
        self.thread=None
        self.next_publish=0
        self.next_configuration=0

    def _task(self,name,operation):
        try:
            operation()
            value={'status':'ok'}
        except Exception:
            value={'status':'unavailable'}
            logging.getLogger(__name__).warning('%s unavailable; retaining durable evidence',name)
        self.store.set_runtime(self.company,name,value,self.clock())
        return value['status']=='ok'

    def _import(self):
        for _ in range(4):
            after=self.store.activity_cursor(self.company)
            page=self.activity_client.get('/activity/policy-events',self.company,{'after':after,'limit':200})
            self.store.import_activity(self.company,page,self.clock())
            if page['next_after']>=page['high_watermark']: break

    def _configuration(self):
        from .policies import POLICIES
        data=self.crm_client.get('/inventory')
        config=data.get('policy_configuration',{})
        modes=config.get('modes',{})
        if (data.get('company_id')!=self.company or not data.get('complete') or config.get('version')!=1
                or set(modes)!=(set(POLICIES)-{'inactive_owner_agent_access'})
                or any(v not in {'off','observe','enforce'} for v in modes.values())):
            raise ValueError('Invalid CRM policy configuration')
        self.store.set_runtime(self.company,'configuration',config,self.clock())

    def run_once(self):
        now=self.clock()
        if self.publisher and now>=self.next_publish:
            succeeded=self._task('identity_publisher',self.publisher.run_once)
            self.next_publish=now+(60 if succeeded else 5)
        if self.crm_client and now>=self.next_configuration:
            self._task('policy_configuration_worker',self._configuration)
            self.next_configuration=now+5
        if self.activity_client:
            self._task('activity_policy_worker',self._import)

    @property
    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        self.thread=threading.Thread(target=self._loop,name='security-policy-worker',daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try: self.run_once()
            except Exception: logging.getLogger(__name__).error('Policy worker storage unavailable')
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(30)
            if self.thread.is_alive(): raise RuntimeError('Policy worker did not stop')
