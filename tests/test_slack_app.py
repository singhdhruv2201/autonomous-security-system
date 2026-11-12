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


def test_handle_properly_awaits_bolt_async_ack(setup):
    from slack_bolt.context.ack.async_ack import AsyncAck
    store, cid, config, client, notifier, _ = setup
    publish(setup)
    handler = SlackActions(store, notifier, config, team_id="T123")

    # 1. Accepted interaction
    ack = AsyncAck()
    asyncio.run(handler.handle(ack=ack, body=click(cid)))
    assert ack.response is not None
    assert ack.response.status == 200

    # 2. Rejected interaction (unauthorized user)
    ack_rejected = AsyncAck()
    asyncio.run(handler.handle(ack=ack_rejected, body=click(cid, user="U_UNAUTHORIZED")))
    assert ack_rejected.response is not None
    assert ack_rejected.response.status == 200


def test_approval_and_outcome_rendering_distinguishes_actions(setup):
    store, cid, config, client, notifier, question = setup
    now = time.time()

    # A) Low-confidence pause approval (key: approve:crm.pause_principal)
    pause_q = {
        "key": "approve:crm.pause_principal",
        "role": "approver",
        "addressed_to": "emp-casey-wong",
        "choices": ["approve", "reject"],
        "facts": {"record_count": 6},
    }
    rendered = notifier.render_body(cid, pause_q["key"], pause_q)
    assert "pause" in rendered["text"].lower()
    assert "Hannah Reed" not in rendered["text"]
    assert "retire" not in rendered["text"].lower()
    assert "revok" not in rendered["text"].lower()
    action_buttons = [
        btn["text"]["text"]
        for block in rendered["blocks"] if block.get("type") == "actions"
        for btn in block.get("elements", [])
    ]
    assert "Approve pause" in action_buttons
    assert "Reject pause" in action_buttons

    # B) Retirement approval with Hannah's prior decision
    store.record_decision(cid, key="lead_decision", employee_id="emp-hannah-reed", source="slack", choice="retire", now=now)
    retire_q = {
        "key": "approve:crm.revoke_grant",
        "role": "approver",
        "addressed_to": "emp-casey-wong",
        "choices": ["approve", "reject"],
        "facts": {"record_count": 6},
    }
    rendered_retire = notifier.render_body(cid, retire_q["key"], retire_q)
    assert "Hannah Reed requested to retire" in rendered_retire["text"]
    assert "revoking" in rendered_retire["text"].lower()
    action_buttons_retire = [
        btn["text"]["text"]
        for block in rendered_retire["blocks"] if block.get("type") == "actions"
        for btn in block.get("elements", [])
    ]
    assert "Approve revocation" in action_buttons_retire
    assert "Reject revocation" in action_buttons_retire

    # C) Retirement approval WITHOUT prior lead decision (cid2)
    cid2 = store.open_case("helio", "finding-direct", route="investigating", now=now)
    rendered_direct = notifier.render_body(cid2, retire_q["key"], retire_q)
    assert "Hannah Reed" not in rendered_direct["text"]
    assert "revoking" in rendered_direct["text"].lower()

    # D) Outcome text: Undo executed and verified
    act_pause_id = store.record_action(cid, playbook="crm.pause_principal", target="grant:1", requested_by="awasi", idempotency_key="act1", now=now)
    store.update_action(act_pause_id, status="undone", now=now)
    act_undo_id = store.record_action(cid, playbook="crm.pause_principal", target="grant:1", requested_by="emp-hannah-reed", idempotency_key="act2", now=now, undo_of=act_pause_id)
    store.update_action(act_undo_id, status="executed", verified_at=now, now=now)
    store.close_case(cid, status="needs_review", now=now)
    rendered_undo = notifier.render_body(cid, "outcome", {})
    assert "restored" in rendered_undo["text"].lower() or "undo" in rendered_undo["text"].lower()
    assert "revoked" not in rendered_undo["text"].lower()

    # E) Outcome text: Pause executed and verified (resolved case)
    cid3 = store.open_case("helio", "finding-pause", route="investigating", now=now)
    act3_id = store.record_action(cid3, playbook="crm.pause_principal", target="grant:3", requested_by="awasi", idempotency_key="act3", now=now)
    store.update_action(act3_id, status="executed", verified_at=now, now=now)
    store.close_case(cid3, status="resolved", now=now)
    rendered_pause = notifier.render_body(cid3, "outcome", {})
    assert "paused" in rendered_pause["text"].lower()
    assert "revoked" not in rendered_pause["text"].lower()

    # F) Outcome text: Action executed but NOT verified (verified_at is None)
    cid4 = store.open_case("helio", "finding-unverified", route="investigating", now=now)
    act4_id = store.record_action(cid4, playbook="crm.revoke_grant", target="grant:4", requested_by="awasi", idempotency_key="act4", now=now)
    store.update_action(act4_id, status="executed", now=now)
    store.close_case(cid4, status="needs_review", now=now)
    rendered_unverified = notifier.render_body(cid4, "outcome", {})
    assert "could not be verified" in rendered_unverified["text"].lower() or "unverified" in rendered_unverified["text"].lower()
    assert "revoked" not in rendered_unverified["text"].lower()


def test_flush_handles_rate_limiting_without_marking_unknown(setup):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse
    store, cid, config, client, notifier, question = setup

    # 1. 429 Rate limit on chat_postMessage
    asyncio.run(notifier.notify(cid, question["key"], question))

    resp_429 = AsyncSlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/chat.postMessage",
        req_args={},
        headers={"retry-after": "3"},
        status_code=429,
        data={"ok": False, "error": "ratelimited"},
    )

    async def fail_429(**kwargs):
        raise SlackApiError("ratelimited", resp_429)

    client.chat_postMessage = fail_429

    asyncio.run(notifier.flush())
    # Must NOT be marked unknown!
    status = notifier.delivery_status()
    assert status["unknown"] == 0
    assert status["pending"] == 1

    with store.connection() as db:
        msg = db.execute("SELECT * FROM slack_messages WHERE case_id=?", (cid,)).fetchone()
        assert msg["status"] == "queued"
        assert msg["retry_at"] > time.time()

    # 2. Ambiguous network timeout on chat_postMessage (ts is None)
    async def fail_timeout(**kwargs):
        raise TimeoutError("connection dropped")

    client.chat_postMessage = fail_timeout
    with store.connection() as db:
        db.execute("UPDATE slack_messages SET retry_at=0 WHERE case_id=?", (cid,))

    asyncio.run(notifier.flush())
    status = notifier.delivery_status()
    assert status["unknown"] == 1
    assert status["pending"] == 0


@pytest.mark.parametrize('status,verified', [('executed', False), ('undone', True), ('executed', True)])
def test_lead_copy_requires_a_current_verified_pause(setup, status, verified):
    store, cid, _, _, notifier, question = setup
    now = time.time()
    aid = store.record_action(cid, playbook='crm.pause_principal', target='agent:sales-research',
        idempotency_key='pause-copy', requested_by='awasi', now=now)
    store.update_action(aid, status=status, now=now, verified_at=now if verified else None)
    text = notifier.render_body(cid, 'lead_decision', question)['text'].lower()
    if status == 'undone':
        assert 'undone' in text
        assert 'disabled' not in text
    elif not verified:
        assert 'not been verified' in text
        assert 'disabled' not in text
    else:
        assert 'disabled' in text and 'verified' in text


def test_refreshed_lead_message_does_not_describe_undo_as_current_pause(setup):
    store, cid, _, client, notifier, question = setup
    now = time.time()
    original = store.record_action(cid, playbook='crm.pause_principal', target='agent:sales-research',
        idempotency_key='pause-copy', requested_by='awasi', now=now)
    store.update_action(original, status='executed', verified_at=now, now=now)
    publish(setup)
    undo = store.record_action(cid, playbook='crm.pause_principal', target='agent:sales-research',
        idempotency_key='undo-copy', requested_by='emp-hannah-reed', undo_of=original, now=now+1)
    store.update_action(undo, status='executed', verified_at=now+1, now=now+1)
    store.update_action(original, status='undone', now=now+1)
    store.record_decision(cid, key='lead_decision', choice='undo', employee_id='emp-hannah-reed', source='slack', now=now+1)
    store.close_case(cid, status='needs_review', now=now+1)
    asyncio.run(notifier.flush())
    text = client.updates[-1]['text'].lower()
    assert 'restored' in text
    assert 'disabled' not in text
    assert not any(block['type']=='actions' for block in client.updates[-1]['blocks'])


@pytest.mark.parametrize('undo_status', ['pending', 'unknown', 'executed'])
def test_unverified_undo_does_not_claim_access_is_disabled_or_restored(setup, undo_status):
    store, cid, _, _, notifier, question = setup
    now = time.time()
    original = store.record_action(cid, playbook='crm.pause_principal', target='agent:sales-research',
        idempotency_key='pause-copy', requested_by='awasi', now=now)
    store.update_action(original, status='executed', verified_at=now, now=now)
    undo = store.record_action(cid, playbook='crm.pause_principal', target='agent:sales-research',
        idempotency_key='undo-copy', requested_by='emp-hannah-reed', undo_of=original, now=now+1)
    store.update_action(undo, status=undo_status, now=now+1)
    text = notifier.render_body(cid, 'lead_decision', question)['text'].lower()
    assert 'current crm access has not been verified' in text
    assert 'disabled' not in text
    assert 'restored' not in text


def test_lead_message_renders_investigation_summary(setup):
    store, cid, _, _, notifier, question = setup
    now = time.time()
    store.save_investigation(
        cid,
        {
            "summary": "Agent sales-research retained customer database access without active human supervisor.",
            "impact": "6 customer records accessible",
            "likely_cause": "Owner offboarded without credential deprovisioning",
            "confidence": "high",
            "question_for_owner": "Should we decommission this agent?",
            "evidence_refs": ["finding-1", "agent:sales-research"],
            "recommended_playbook": "crm.pause_principal",
        },
        source="model",
        now=now,
    )
    rendered = notifier.render_body(cid, "lead_decision", question)
    text = rendered["text"]
    assert "retained customer database access without active human supervisor" in text
    assert "Likely cause: Owner offboarded without credential deprovisioning" in text
    assert "confidence: high" in text
    assert "evidence count: 2" in text
    assert "Should we decommission this agent?" in text


def test_lead_message_escapes_slack_markup(setup):
    from helio_security.slack_app import escape_slack_text
    assert escape_slack_text("A & B <admin>") == "A &amp; B &lt;admin&gt;"

    store, cid, _, _, notifier, question = setup
    now = time.time()
    store.save_investigation(
        cid,
        {
            "summary": "Agent accessed <internal> customer db & records.",
            "impact": "Exposed <data> & details",
            "likely_cause": "unapproved_tool",
            "confidence": "high",
            "question_for_owner": "Should we remove <agent> & revoke?",
            "evidence_refs": [],
        },
        source="model",
        now=now,
    )
    rendered = notifier.render_body(cid, "lead_decision", question)
    text = rendered["text"]
    assert "&lt;internal&gt;" in text
    assert "&amp;" in text
    assert "&lt;agent&gt;" in text
    assert "<internal>" not in text
    assert "<agent>" not in text
