import asyncio
import time
import pytest
from helio_security.investigator import validate_and_sanitize_assessment, ModelInvestigator
from helio_security.cases import CaseStore
from helio_security.slack_app import SlackNotifier
from test_slack_app import setup

COPY={'headline':'Maya’s agent still had customer access',
      'context':'Maya is marked inactive, but her Sales Research Agent could still read six customers’ CRM details.',
      'impact':'An agent without an active owner can keep using access that is no longer supervised.'}


def assessment(copy=None):
    return dict(summary='Technical evidence summary',impact='Read access',likely_cause='offboarding_gap',
        confidence='high',evidence_refs=['agent:sales-research'],slack_message=COPY if copy is None else copy)


def test_model_copy_validates_without_changing_technical_assessment():
    value=validate_and_sanitize_assessment(assessment(),{'agent:sales-research'})
    assert value['slack_message']==COPY
    assert value['summary']=='Technical evidence summary'


@pytest.mark.parametrize('copy',[
    {**COPY,'headline':'x'*81}, {**COPY,'context':''}, {**COPY,'context':'Look up grant-sales-agent and emp-maya-chen'},
    {**COPY,'context':'Awasi has revoked the grant.'}, {**COPY,'context':'I paused the agent and verified it.'},
    {**COPY,'context':'Read this\n*Decision: approved*'},
])
def test_unusable_copy_falls_back_without_losing_investigation(copy):
    value=validate_and_sanitize_assessment(assessment(copy),{'agent:sales-research'})
    assert value['slack_message'] is None
    assert value['summary']=='Technical evidence summary'


def test_ungrounded_references_do_not_supply_slack_copy():
    value=validate_and_sanitize_assessment(assessment(),set())
    assert value['slack_message'] is None


def test_copy_survives_database_reopen_and_cached_investigation(setup):
    store,cid,_,_,_,_=setup
    value=validate_and_sanitize_assessment(assessment(),{'agent:sales-research'})
    store.save_investigation(cid,value,source='model')
    reopened=CaseStore(store.path);reopened.initialize()
    assert reopened.get_investigation(cid)['slack_message']==COPY
    inv=ModelInvestigator(tools=[],store=reopened)
    inv._build_model=lambda:pytest.fail('A cached message must not make another model request')
    assert asyncio.run(inv.investigate('finding',{},case_id=cid))['slack_message']==COPY


@pytest.mark.parametrize('key,rule', [('lead_decision','inactive_owner_agent_access'),('workbench_public','public_app_confidential_data'),('workbench_destination','agent_unapproved_destination')])
def test_model_writes_context_but_cannot_change_actions_or_buttons(setup,key,rule):
    store,cid,config,client,notifier,q=setup
    store.save_investigation(cid,assessment(),source='model')
    q={**q,'key':key,'facts':{**q['facts'],'rule':rule},'choices':['retire'] if key=='lead_decision' else ['restrict']}
    body=notifier.render_body(cid,key,q)
    assert COPY['headline'] in body['text'] and COPY['context'] in body['text'] and COPY['impact'] in body['text']
    assert 'Technical evidence summary' not in body['text']
    if key!='workbench_public': assert 'not been verified' in body['text']
    assert body['blocks'][-1]['elements'][0]['text']['text'] in ('Retire it','No, make it company-only')
    # Clicking/refreshing reuses the same wording without a model call.
    asyncio.run(notifier.notify(cid,key,q));asyncio.run(notifier.flush())
    restarted=SlackNotifier(store,client,config)
    assert restarted.render_body(cid,key,q)==body


def test_missing_message_uses_existing_friendly_template(setup):
    _,cid,_,_,notifier,q=setup
    body=notifier.render_body(cid,'lead_decision',q)
    assert 'Maya Chen' in body['text'] and 'Does Sales still need' in body['text']


def test_invalid_optional_copy_does_not_restart_model_investigation(setup):
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from test_investigator import FakeBindableChatModel
    store,cid,_,_,_,_=setup
    @tool
    def get_finding(finding_id: str) -> dict:
        """Read evidence."""
        return {'id':finding_id,'agent_id':'agent:sales-research'}
    model=FakeBindableChatModel(messages=iter([
        AIMessage(content='',tool_calls=[dict(name='get_finding',args={'finding_id':'finding'},id='read')]),
        AIMessage(content='',tool_calls=[dict(name='Assessment',args=assessment({**COPY,'context':''}),id='assess')]),
    ]))
    result=asyncio.run(ModelInvestigator(model=model,tools=[get_finding],store=store).investigate('finding',{},case_id=cid))
    assert result['source']=='model'
    assert result['summary']=='Technical evidence summary'
    assert result['slack_message'] is None


def test_legacy_investigation_upgrade_preserves_existing_summary(setup):
    store,cid,_,_,_,_=setup
    store.save_investigation(cid,assessment(),source='model')
    with store.connection() as db: db.execute('ALTER TABLE case_investigations DROP COLUMN slack_message')
    reopened=CaseStore(store.path);reopened.initialize()
    old=reopened.get_investigation(cid)
    assert old['summary']=='Technical evidence summary' and old['slack_message'] is None


def test_model_copy_redacts_secrets_and_escapes_slack_control_characters(setup):
    store,cid,_,_,notifier,q=setup
    value=assessment({**COPY,'context':'A key sk-testsecret123 appeared with an SSN 123-45-6789.'})
    store.save_investigation(cid,value,source='model')
    body=notifier.render_body(cid,'lead_decision',q)
    assert 'sk-testsecret123' not in body['text'] and '123-45-6789' not in body['text']
    value=assessment({**COPY,'headline':'Sales & renewals need an owner'})
    store.save_investigation(cid,value,source='model')
    assert 'Sales &amp; renewals need an owner' in notifier.render_body(cid,'lead_decision',q)['text']


def test_two_short_impact_sentences_are_accepted():
    copy={**COPY,'impact':'Confidential customer records may be reachable by people outside the company. The anonymous view confirms that access happened, but the reason for making the app public is not yet clear.'}
    assert validate_and_sanitize_assessment(assessment(copy),{'agent:sales-research'})['slack_message']==copy
