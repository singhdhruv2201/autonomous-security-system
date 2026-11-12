import asyncio
import json
import pytest
from langchain_core.messages import AIMessage
from helio_security.live_ledger import LiveLedger


def assessment(case):
    return dict(summary='Recorded evidence summary',impact='Access risk',likely_cause=case['expected_causes'][0],confidence='high',evidence_refs=list(case['required_refs']),question_for_owner=None,recommended_playbook=None)


def test_scores_raw_evidence_and_owner_not_sanitized_output():
    from helio_security.evaluation import CASES, score
    case=CASES[0];raw=assessment(case)
    assert score(case,raw)['passed']
    raw['evidence_refs'].append('invented-id')
    assert not score(case,raw)['evidence_valid']
    raw=assessment(case);raw['question_for_owner']='Still needed?'
    assert not score(case,raw)['owner_question_valid']
    raw=assessment(case);raw['evidence_refs']=[]
    assert not score(case,raw)['passed']
    raw=assessment(case);raw['likely_cause']='possible_compromise'
    assert not score(case,raw)['cause_correct']
    assert not score(case,{'invalid':'schema'})['passed']


def test_three_scenarios_three_controls_and_controls_cannot_recommend_action():
    from helio_security.evaluation import CASES, score
    assert len(CASES)==6 and len({c['id'] for c in CASES})==6
    controls=[c for c in CASES if c['control']]
    assert len(controls)==3
    for case in controls:
        raw=assessment(case);raw['recommended_playbook']='crm.pause_principal'
        assert not score(case,raw)['passed']


def test_report_preserves_raw_failures_and_uses_shared_budget(tmp_path):
    from helio_security.evaluation import CASES, evaluate
    class Model:
        calls=0
        async def ainvoke(self,*args,**kwargs):
            case=CASES[self.calls];self.calls+=1
            raw=assessment(case)
            if self.calls==2: raw['evidence_refs'].append('invented-id')
            return AIMessage(content='',tool_calls=[dict(name='Assessment',args=raw,id='answer')],usage_metadata=dict(input_tokens=100,output_tokens=100,total_tokens=200))
    model=Model();ledger=LiveLedger(tmp_path/'ledger.json',max_requests=2,max_spend_usd=.50)
    path=tmp_path/'report.json'
    report=asyncio.run(evaluate(model,ledger,path,'claude-haiku-5-5'))
    assert model.calls==2 and ledger.get_data()['total_requests']==2
    assert not report['passed'] and len(report['cases'])==6
    assert report['cases'][0]['passed']
    assert not report['cases'][1]['evidence_valid']
    assert report['cases'][2]['error']=='BudgetExceededError'
    assert json.loads(path.read_text())==report
    # Existing reports are immutable: an accidental repeat must not spend again.
    with pytest.raises(FileExistsError): asyncio.run(evaluate(model,ledger,path,'claude-haiku-5-5'))
    assert model.calls==2


def test_provider_error_is_failure_not_scripted_success(tmp_path):
    from helio_security.evaluation import evaluate
    class Model:
        calls=0
        async def ainvoke(self,*args,**kwargs):
            self.calls+=1
            raise TimeoutError('secret-provider-body')
    model=Model()
    ledger=LiveLedger(tmp_path/'ledger.json',max_requests=40)
    report=asyncio.run(evaluate(model,ledger,tmp_path/'report.json','claude-haiku-5-5'))
    assert model.calls==1
    assert not report['passed']
    assert report['cases'][0]['error']=='TimeoutError'
    assert 'secret-provider-body' not in json.dumps(report)
    assert ledger.get_data()['runs'][0]['usage_known'] is False


def test_worker_ledger_configuration_shares_limits(tmp_path,monkeypatch):
    from helio_security.live_ledger import configured_ledger
    path=tmp_path/'ledger.json'
    monkeypatch.setenv('AWASI_LIVE_LEDGER',str(path))
    monkeypatch.setenv('AWASI_LIVE_MAX_SPEND_USD','0.50')
    monkeypatch.setenv('AWASI_LIVE_MAX_REQUESTS','40')
    ledger=configured_ledger()
    assert ledger.max_spend_usd==.50 and ledger.max_requests==40
    for _ in range(11):
        key=ledger.check_and_reserve();ledger.record_request('claude-haiku-5-5',1,1,reservation_id=key)
    assert configured_ledger().get_data()['total_requests']==11
    monkeypatch.setenv('AWASI_LIVE_MAX_SPEND_USD','nan')
    with pytest.raises(ValueError): configured_ledger()
    monkeypatch.setenv('AWASI_LIVE_MAX_SPEND_USD','0.50')
    monkeypatch.setenv('AWASI_LIVE_MAX_REQUESTS','-1')
    with pytest.raises(ValueError): configured_ledger()


def test_recorded_control_metadata_matches_seeded_workbench(tmp_path):
    from helio_security.evaluation import CASES
    from helio_workbench.store import WorkbenchStore
    store=WorkbenchStore(tmp_path/'workbench.db');store.initialize()
    cases={c['id']:c['evidence'] for c in CASES}
    app=store.get_app_by_id('app-support-lookup')
    agent=store.get_agent('agent-renewal-digest')
    assert cases['company_only']['owner']['employee_id']==app['owner_employee_id']
    assert cases['company_only']['visibility']==app['visibility']
    assert cases['approved_destination']['owner']['employee_id']==agent['owner_employee_id']


def test_evaluation_requests_strict_schema_without_unsupported_constraints(monkeypatch):
    from helio_security.evaluation import build_eval_model
    monkeypatch.setenv('ANTHROPIC_API_KEY','fake-test-key')
    model=build_eval_model('claude-haiku-5-5')
    tool=model.kwargs['tools'][0]
    assert tool['name']=='Assessment' and tool['strict'] is True
    schema=tool['input_schema']
    assert schema['additionalProperties'] is False
    assert set(schema['required'])==set(schema['properties'])
    assert all(k not in json.dumps(schema) for k in ('maxLength','minLength','maxItems'))
    assert model.bound.max_retries==0
