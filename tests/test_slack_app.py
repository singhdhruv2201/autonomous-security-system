"""Slack transport and authorization tests; no real credentials or messages."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from helio_security.cases import CaseStore
from helio_security.slack_app import SlackConfig, SlackNotifier, SlackActions, LogNotifier


class FakeClient:
    def __init__(self):
        self.posts = []
        self.updates = []
        self.ephemeral = []
        self.fail_post = False
        self.fail_update = False

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        if self.fail_post:
            raise TimeoutError('a secret must never be logged')
        return {'ok': True, 'channel': kwargs['channel'], 'ts': f'{len(self.posts)}.000001'}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        if self.fail_update:
            raise TimeoutError('update interrupted')
        return {'ok': True, 'channel': kwargs['channel'], 'ts': kwargs['ts']}

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)
        return {'ok': True}


@pytest.fixture
def setup(tmp_path):
    store = CaseStore(tmp_path / 'security.sqlite3'); store.initialize()
    cid = store.open_case('helio', 'finding', route='investigating', now=time.time())
    config = SlackConfig(bot_token='xoxb-test', app_token='xapp-test', channel='C123', default_user='U123')
    client = FakeClient()
    notifier = SlackNotifier(store, client, config)
    question = {'key': 'lead_decision', 'role': 'lead', 'choices': ['retire', 'keep', 'undo'],
                'addressed_to': 'emp-hannah-reed', 'deadline': time.time()+900,
                'facts': {'agent_id': 'agent:sales-research', 'owner_status': 'inactive', 'record_count': 6}}
    store.offer_question(cid, question, now=time.time())
    return store, cid, config, client, notifier, question


def click(cid, choice='retire', user='U123', ts='1.000001', key='lead_decision'):
    return {'user': {'id': user}, 'channel': {'id': 'C123'}, 'team': {'id': 'T123'},
            'container': {'message_ts': ts}, 'actions': [{'action_id': 'awasi_undo' if choice=='undo' else 'awasi_choice',
            'value': json.dumps({'case_id': cid, 'key': key, 'choice': choice})}]}


def publish(setup):
    _, cid, _, _, notifier, question = setup
    asyncio.run(notifier.notify(cid, question['key'], question))
    asyncio.run(notifier.flush())


def test_notification_survives_restart_and_updates_in_same_thread(setup):
    store, cid, config, client, notifier, question = setup
    publish(setup)
    notifier = SlackNotifier(store, client, config)
    asyncio.run(notifier.notify(cid, question['key'], question))
    asyncio.run(notifier.flush())
    assert len(client.posts) == 1
    followup = {**question, 'key': 'approve:crm.revoke_grant', 'role': 'approver',
                'addressed_to': 'emp-casey-wong', 'choices': ['approve','reject']}
    store.offer_question(cid, followup, now=time.time())
    asyncio.run(notifier.notify(cid, followup['key'], followup))
    asyncio.run(notifier.flush())
    assert len(client.posts) == 2
    assert client.posts[1]['thread_ts'] == '1.000001'
    assert 'To: Hannah Reed (Sales Lead)' in client.posts[0]['text']
    assert '<@U123>' in client.posts[0]['text']
    assert 'To: Casey Wong (CTO)' in client.posts[1]['text']
    assert '<@U123>' not in client.posts[1]['text']


def test_ack_precedes_validation_and_decision_is_durable(setup):
    store, cid, config, client, notifier, _ = setup
    publish(setup)
    wake = []
    async def ack():
        assert store.unconsumed_decisions(cid) == []
    handler = SlackActions(store, notifier, config, team_id='T123', wake=lambda: wake.append(True))
    asyncio.run(handler.handle(ack=ack, body=click(cid)))
    decision = store.unconsumed_decisions(cid)[0]
    assert decision['employee_id'] == 'emp-hannah-reed'
    assert decision['choice'] == 'retire' and decision['source'] == 'slack'
    assert wake
    assert client.updates
    assert not any(b['type']=='actions' for b in client.updates[-1]['blocks'])
    assert 'Hannah Reed' in client.updates[-1]['text']


@pytest.mark.parametrize('change', ['unauthorized','wrong_team','wrong_channel','wrong_message','bad_choice','bad_key','closed','malformed'])
def test_rejected_click_changes_nothing(setup, change):
    store, cid, config, client, notifier, _ = setup
    publish(setup)
    body = click(cid)
    if change=='unauthorized': body['user']['id']='U999'
    if change=='wrong_team': body['team']['id']='T999'
    if change=='wrong_channel': body['channel']['id']='C999'
    if change=='wrong_message': body['container']['message_ts']='999.0'
    if change=='bad_choice': body=click(cid, choice='approve')
    if change=='bad_key': body=click(cid, key='stale')
    if change=='closed': store.close_case(cid,status='needs_review',now=time.time())
    if change=='malformed': body['actions'][0]['value']='not-json'
    handler=SlackActions(store,notifier,config,team_id='T123',wake=lambda: None)
    ack=AsyncMock()
    before=store.get_case_full(cid)
    asyncio.run(handler.handle(ack=ack, body=body))
    ack.assert_awaited_once()
    assert store.unconsumed_decisions(cid)==[]
    assert store.get_case_full(cid)==before
    assert client.ephemeral and not client.updates


def test_double_click_and_message_update_failure_do_not_lose_decision(setup):
    store,cid,config,client,notifier,question=setup
    publish(setup)
    client.fail_update=True
    handler=SlackActions(store,notifier,config,team_id='T123',wake=lambda: None)
    for _ in range(2):
        asyncio.run(handler.handle(ack=AsyncMock(),body=click(cid)))
    assert len(store.unconsumed_decisions(cid))==1
    client.fail_update=False
    # Replay the workflow notification too: it must not resurrect the buttons.
    asyncio.run(notifier.notify(cid,question['key'],question))
    asyncio.run(notifier.flush())
    assert len(client.posts)==1
    assert not any(b['type']=='actions' for b in client.updates[-1]['blocks'])


def test_uncertain_post_is_not_blindly_reposted(setup):
    store,cid,config,client,notifier,question=setup
    client.fail_post=True
    publish(setup)
    client.fail_post=False
    notifier=SlackNotifier(store,client,config)
    asyncio.run(notifier.notify(cid,question['key'],question))
    asyncio.run(notifier.flush())
    assert len(client.posts)==1
    assert notifier.delivery_status()['unknown']==1


def test_rendering_ignores_untrusted_free_text_and_secret_values(setup):
    store,cid,_,client,notifier,question=setup
    question['summary']='CUSTOMER SECRET xoxb-credential <@U999>'
    question['facts']['owner_name']='CUSTOMER SECRET'
    asyncio.run(notifier.notify(cid,question['key'],question)); asyncio.run(notifier.flush())
    message=json.dumps(client.posts)
    assert 'CUSTOMER SECRET' not in message and 'xoxb-credential' not in message and 'U999' not in message
    assert '6 customers' in message
    assert 'paused' not in client.posts[0]['text'].lower()  # no verified action exists in this fixture
    for block in client.posts[0]['blocks']:
        if block['type']=='actions':
            for button in block['elements']:
                assert set(json.loads(button['value']))=={'case_id','key','choice'}


def test_missing_configuration_uses_log_notifier():
    config=SlackConfig.from_environment({})
    assert not config.configured
    assert 'xoxb-' not in repr(config)
