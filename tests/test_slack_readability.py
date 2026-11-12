import asyncio
import json
import time
from helio_security.slack_app import SlackNotifier
from test_slack_app import setup, publish


def test_investigation_stays_in_case_not_slack_and_copy_is_scannable(setup):
    store,cid,_,_,notifier,q=setup
    store.save_investigation(cid,dict(summary='Finding id-123 grant-sales-agent HTTP 503 leaking_now false',
        impact='technical',likely_cause='offboarding_gap',confidence='high',evidence_refs=['grant-sales-agent'],
        question_for_owner='Should emp-maya-chen approve grant-sales-agent?'),source='model')
    aid=store.record_action(cid,playbook='crm.pause_principal',target='agent:sales-research',idempotency_key='pause',requested_by='awasi',now=time.time())
    store.update_action(aid,status='executed',verified_at=time.time(),now=time.time())
    body=notifier.render_body(cid,'lead_decision',q)
    assert "Maya Chen" in body['text'] and '6 customers' in body['text']
    assert 'paused' in body['text'] and 'verified' in body['text']
    assert 'Does Sales still need' in body['text']
    assert all(word not in body['text'] for word in ('grant-sales-agent','HTTP 503','leaking_now','offboarding_gap','confidence:','emp-maya'))
    sections=[b['text']['text'] for b in body['blocks'] if b['type']=='section']
    assert len(sections)>=3 and max(map(len,sections))<450
    assert len(body['text'])<850
    assert 'grant-sales-agent' in store.get_investigation(cid)['summary']


def test_refresh_after_restart_preserves_original_public_context_and_recipient(setup):
    store,cid,config,client,notifier,_=setup
    q=dict(key='workbench_public',role='owner',deadline=time.time()+900,addressed_to='emp-luca-rossi',choices=['restrict','intentional'],
        facts=dict(rule='public_app_confidential_data',asset_name='Renewal Tracker',record_count=12),deadline_seconds=900)
    store.offer_question(cid,q,now=time.time())
    asyncio.run(notifier.notify(cid,q['key'],q));asyncio.run(notifier.flush())
    store.record_decision(cid,key=q['key'],employee_id='emp-luca-rossi',source='slack',choice='restrict',now=time.time())
    # A later approver question must not replace the original message's facts or address.
    store.offer_question(cid,dict(key='workbench_approval',role='approver',deadline=time.time()+900,addressed_to='emp-casey-wong',choices=['approve'],facts={}),now=time.time())
    notifier=SlackNotifier(store,client,config)
    asyncio.run(notifier.flush())
    text=client.updates[-1]['text']
    assert 'Renewal Tracker' in text and '12 customers' in text and 'Luca Rossi' in text
    assert 'Casey Wong' not in text and 'Security decision required' not in text
    assert 'company-only' in text
    assert not any(b['type']=='actions' for b in client.updates[-1]['blocks'])


def test_decision_label_describes_choice_not_completed_action(setup):
    store,cid,_,_,notifier,_=setup
    q=dict(key='workbench_destination',addressed_to='emp-casey-wong',choices=['remove_connection'],
        facts=dict(rule='agent_unapproved_destination',asset_name='Lead Enrichment Agent',host='enrich-leads',records_sent_1h=6))
    store.record_decision(cid,key=q['key'],employee_id='emp-casey-wong',source='slack',choice='remove_connection',now=time.time())
    text=notifier.render_body(cid,q['key'],q)['text']
    assert 'Remove the connection and resume' in text and 'Casey Wong' in text
    assert 'Remove_Connection' not in text and 'connection disabled' not in text
    assert 'not been verified' in text


def test_workbench_untrusted_names_cannot_create_mentions(setup):
    _,cid,_,_,notifier,_=setup
    q=dict(facts=dict(rule='agent_unapproved_destination',asset_name='<@U999>',host='<!channel>',records_sent_1h=0))
    text=notifier.render_body(cid,'workbench_destination',q)['text']
    assert '<@U999>' not in text and '<!channel>' not in text
    assert '0 customer' not in text  # no observed transfer, not a claim that a transfer happened
    assert 'No customer records' in text


def test_click_update_uses_saved_context_despite_trimmed_question(setup):
    from unittest.mock import AsyncMock
    from helio_security.slack_app import SlackActions
    from test_slack_app import click
    store,cid,config,client,notifier,_=setup
    q=dict(key='workbench_destination',role='approver',addressed_to='emp-casey-wong',deadline=time.time()+900,
        choices=['remove_connection'],facts=dict(rule='agent_unapproved_destination',asset_name='Lead Enrichment Agent',host='enrich-leads',records_sent_1h=6))
    store.offer_question(cid,q,now=time.time())
    asyncio.run(notifier.notify(cid,q['key'],q));asyncio.run(notifier.flush())
    assert 'facts' not in store.question(cid)
    handler=SlackActions(store,notifier,config,team_id='T123')
    asyncio.run(handler.handle(ack=AsyncMock(),body=click(cid,choice='remove_connection',key=q['key'])))
    text=client.updates[-1]['text']
    assert 'Lead Enrichment Agent' in text and '6 customer records' in text and 'enrich-leads' in text
    assert 'Remove the connection and resume' in text
    assert 'payload' not in client.updates[-1]


def test_closed_question_without_decision_has_no_active_buttons(setup):
    store,cid,_,client,notifier,_=setup
    publish(setup)
    store.close_case(cid,status='needs_review',now=time.time())
    asyncio.run(notifier.flush())
    assert not any(b['type']=='actions' for b in client.updates[-1]['blocks'])


def test_existing_outbox_migrates_and_recovers_legacy_workbench_context(setup):
    store,cid,config,client,notifier,q=setup
    # Simulate an existing pre-migration outbox row and persisted workflow event.
    publish(setup)
    with store.connection() as db: db.execute('ALTER TABLE slack_messages DROP COLUMN payload')
    event=dict(key='workbench_public',role='owner',choices=['restrict'],addressed_to='emp-luca-rossi',
               deadline=time.time()+900,facts=dict(rule='public_app_confidential_data',asset_name='Renewal Tracker',record_count=12))
    store.add_event(cid,key='question:workbench_public',type='notification',actor='awasi',text='question',data=event,now=time.time())
    with store.connection() as db: db.execute("UPDATE slack_messages SET key='workbench_public'")
    store.record_decision(cid,key='workbench_public',choice='restrict',employee_id='emp-luca-rossi',source='slack',now=time.time())
    restarted=SlackNotifier(store,client,config)
    asyncio.run(restarted.flush())
    assert 'Renewal Tracker' in client.updates[-1]['text'] and '12 customers' in client.updates[-1]['text']
    assert len(client.posts)==1


def test_answered_message_does_not_keep_asking_or_threaten_timeout(setup):
    store,cid,_,_,notifier,_=setup
    q=dict(key='workbench_public',addressed_to='emp-luca-rossi',choices=['restrict','intentional'],
        facts=dict(rule='public_app_confidential_data',asset_name='Renewal Tracker',record_count=6))
    store.record_decision(cid,key=q['key'],choice='restrict',employee_id='emp-luca-rossi',source='slack',now=time.time())
    text=notifier.render_body(cid,q['key'],q)['text']
    assert 'Your decision' not in text and 'If you do not respond' not in text
    assert 'Renewal Tracker' in text and 'Decision recorded:' in text


def test_legacy_callback_can_read_original_workflow_notification(setup):
    store,cid,_,_,notifier,_=setup
    q=dict(key='workbench_public',role='owner',addressed_to='emp-luca-rossi',deadline=time.time()+900,
        choices=['restrict'],facts=dict(rule='public_app_confidential_data',asset_name='Renewal Tracker',record_count=9))
    store.offer_question(cid,q,now=time.time())
    store.add_event(cid,key='question:workbench_public',type='notification',actor='awasi',text='question',data=q,now=time.time())
    text=notifier.render_body(cid,q['key'],store.question(cid))['text']
    assert 'Renewal Tracker' in text and '9 customers' in text
