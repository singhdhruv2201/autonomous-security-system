import asyncio
import pytest
from fastapi.testclient import TestClient
from helio_workbench.store import WorkbenchStore
from helio_workbench.api import create_app
from test_graph import serve

@pytest.fixture
def workbench(tmp_path):
    store=WorkbenchStore(tmp_path/'wb.db')
    store.update_app('app-renewal-tracker',1,visibility='public')
    app=create_app(store=store,observer_token='o'*32,action_token='a'*32,user_token='u'*32)
    with serve(app) as url:
        yield store,app,dict(workbench_url=url,workbench_observer_token='o'*32,workbench_action_token='a'*32)

@pytest.mark.parametrize('identifier,target,field,before,after',[
    ('workbench.restrict_app','app-renewal-tracker','visibility','public','company'),
    ('workbench.pause_agent','agent-lead-enrichment','status','active','paused'),
    ('workbench.disable_destination','agent-renewal-digest/partner-api','enabled',True,False),
])
def test_real_workbench_run_verify_undo_and_stale_version(workbench,identifier,target,field,before,after):
    from helio_security.playbooks import REGISTRY, PlaybookError
    store,app,config=workbench
    pb=REGISTRY[identifier]
    async def run():
        original=await pb.read(target,**config)
        assert original[field]==before
        assert not await pb.verify(target,**config)
        await pb.run(target,before_state=original,**config)
        assert await pb.verify(target,**config)
        current=await pb.read(target,**config)
        assert current[field]==after and current['version']==original['version']+1
        with pytest.raises(PlaybookError):
            await pb.run(target,before_state=original,**config)
        await pb.undo(target,before=original,before_state=current,**config)
        assert await pb.verify(target,expected_state=before,**config)
    asyncio.run(run())


def test_restrict_verification_requires_anonymous_404(workbench):
    from helio_security.playbooks import REGISTRY
    store,app,config=workbench
    # A lying/misconfigured endpoint must prevent verification even when inventory says company-only.
    app.router.routes=[r for r in app.router.routes if getattr(r,'path','')!='/public/{slug}']
    @app.get('/public/{slug}')
    def exposed(slug): return []
    store.update_app('app-renewal-tracker',2,visibility='company')
    assert not asyncio.run(REGISTRY['workbench.restrict_app'].verify('app-renewal-tracker',**config))
