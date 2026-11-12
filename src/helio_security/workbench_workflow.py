"""Deterministic workbench questions and durable follow-up actions."""
import inspect
import os
from .actions import call_playbook, dispatch

APP_RULE = 'public_app_confidential_data'
AGENT_RULE = 'agent_unapproved_destination'


def is_workbench(state):
    return state.get('facts', {}).get('rule') in (APP_RULE, AGENT_RULE)


def facts_from_finding(finding):
    e = finding.get('evidence', {})
    rule = finding.get('rule', 'inactive_owner_agent_access')
    wb = rule in (APP_RULE, AGENT_RULE)
    app = rule == APP_RULE
    return {**e, 'rule': rule, 'target_kind': 'app' if app else 'agent',
        'owner_status': e.get('employee_status', 'unknown'),
        'data_classification': e.get('classification', 'confidential'),
        'record_count': e.get('record_count', 0),
        'exposure': ('public' if app else 'external') if wb else 'none',
        'leaking_now': (e.get('anonymous_views_1h' if app else 'records_sent_1h') or 0) > 0 if wb else False,
        'dependents': e.get('dependents', 0), 'human_override': False, 'has_lead': not wb,
        'addressed_to': e.get('employee_id') if wb else 'emp-hannah-reed',
        'agent_id': e.get('agent_id', 'agent:sales-research') if not app else None}


def target_for(state, playbook=None):
    f = state['facts']
    pb = playbook or state['decision'].get('playbook')
    if pb == 'workbench.restrict_app':
        return f.get('app_id')
    if pb == 'workbench.disable_destination':
        return f"{f['agent_id']}/{f['host']}"
    return f.get('agent_id')


async def notify(deps, case_id, state):
    f, d = state['facts'], state['decision']
    app = f['rule'] == APP_RULE
    contained = bool(state.get('verified'))
    if app and contained:
        key, role, choices = 'workbench_restricted', 'owner', ['keep_restricted', 'undo']
    elif app and d['action'] == 'ask_owner':
        key, role, choices = 'workbench_public', 'owner', ['restrict', 'intentional']
    elif not app and contained:
        key, role, choices = 'workbench_destination', 'approver', ['remove_connection', 'keep_paused', 'allow_7_days']
    else:
        key, role, choices = 'workbench_approval', 'approver', ['approve', 'reject']
    addressed = 'emp-casey-wong' if role == 'approver' else f.get('addressed_to')
    old = next((e for e in deps.store.get_events(case_id) if e['key'] == f'question:{key}'), None)
    deadline = old['data']['deadline'] if old else deps.clock() + deps.owner_deadline_seconds
    q = dict(key=key, role=role, choices=choices, addressed_to=addressed, deadline=deadline, deadline_seconds=deps.owner_deadline_seconds, facts=f)
    deps.store.offer_question(case_id, q, now=deps.clock())
    deps.store.add_event(case_id, key=f'question:{key}', type='notification', actor='awasi',
        text=f'Awaiting decision from {addressed}', data=q, now=deps.clock())
    if hasattr(deps.notifier, 'notify'):
        result = deps.notifier.notify(case_id, key, {**q, 'decision':d})
        if inspect.isawaitable(result): await result
        if not app and contained:
            result = deps.notifier.notify(case_id, 'workbench_owner_notice',
                {'facts':f, 'addressed_to':f.get('addressed_to'), 'choices':[]})
            if inspect.isawaitable(result): await result
    return {'question':q}


async def verified_action(deps, case_id, pb_id, target, employee, undo_of=None):
    action = await dispatch(deps, case_id, pb_id, target, requested_by=employee, undo_of=undo_of)
    pb = deps.playbooks[pb_id]
    expected = {'expected_state':undo_of['before'][pb.state_field]} if undo_of else {}
    ok = action['status'] == 'executed' and await call_playbook(pb.verify, target, deps, **expected)
    if ok:
        deps.store.update_action(action['id'], status='executed', verified_at=deps.clock(), now=deps.clock())
        if undo_of: deps.store.update_action(undo_of['id'], status='undone', now=deps.clock())
    deps.store.add_event(case_id, key=f"verify:{action['id']}", type='verification' if ok else 'error',
        actor='awasi', text=f'{pb_id}: '+('verified' if ok else 'requires review'), now=deps.clock())
    return bool(ok)


async def resume_safe(deps, company, agent_id):
    current = await call_playbook(deps.playbooks['workbench.pause_agent'].read, agent_id, deps)
    hosts = current.get('enabled_destinations')
    if not isinstance(hosts, list):
        return False
    approved = {h.strip().lower() for h in os.getenv('AWASI_APPROVED_DESTINATIONS', 'partner-api').split(',') if h.strip()}
    return all(host in approved or (deps.platform_store and deps.platform_store.exception_active(
        company, AGENT_RULE, [agent_id, host], deps.clock())) for host in hosts)


async def process(deps, case_id, state, update):
    q, a, f = state['question'], state.get('human_input', {}), state['facts']
    choice, employee = a.get('choice'), a.get('employee_id')
    timeout = choice == 'timeout' and employee == 'system' and deps.clock() >= q['deadline']
    valid = a.get('key', q['key']) == q['key'] and (
        timeout or (employee in ('operator', q['addressed_to']) and choice in q['choices']))
    if not valid:
        deps.store.close_case(case_id, status='needs_review', now=deps.clock())
        return {**update, 'verified':False}
    app = f['rule'] == APP_RULE
    pb_id = 'workbench.restrict_app' if app else 'workbench.pause_agent'
    if (q['key'] == 'workbench_public' and choice in ('restrict', 'timeout')) or (
            q['key'] == 'workbench_approval' and choice == 'approve'):
        return {**update, 'verified':False, 'next_step':'execute', 'decision':{
            **state['decision'], 'action':'fix', 'playbook':pb_id,
            'followup':'tell_owner' if app else 'ask_approver'}}
    # Use the durable human decision time, so recovery cannot renew an old approval.
    recorded = deps.store.decision(case_id, q['key'])
    approved_at = recorded['occurred_at'] if recorded else q['deadline'] - deps.owner_deadline_seconds
    expires_at = approved_at + 7 * 86400
    ok = False
    try:
        if q['key'] == 'workbench_public' and choice == 'intentional':
            deps.platform_store.approve_exception(state['company_id'], state['finding_id'], employee,
                expires_at=expires_at, now=deps.clock())
            ok = True
        elif q['key'] == 'workbench_restricted' and choice in ('keep_restricted', 'timeout'):
            ok = await call_playbook(deps.playbooks[pb_id].verify, target_for(state, pb_id), deps)
        else:
            original = next((a for a in deps.store.get_actions(case_id) if a['playbook'] == pb_id
                and not a['undo_of'] and a['before'] and a['status'] in ('executed','undone')), None)
            if q['key'] == 'workbench_restricted' and choice == 'undo' and original:
                deps.store.set_human_override(case_id, now=deps.clock())
                await verified_action(deps, case_id, pb_id, original['target'], employee, original)
                # Restoring exposure requires review even when the undo succeeded.
            elif q['key'] == 'workbench_destination' and original:
                if choice == 'keep_paused':
                    ok = await call_playbook(deps.playbooks[pb_id].verify, original['target'], deps)
                elif choice in ('remove_connection', 'allow_7_days'):
                    if choice == 'remove_connection':
                        safe = await verified_action(deps, case_id, 'workbench.disable_destination',
                            target_for(state, 'workbench.disable_destination'), employee)
                    else:
                        deps.platform_store.approve_exception(state['company_id'], state['finding_id'], employee,
                            expires_at=expires_at, now=deps.clock())
                        safe = True
                    if safe:
                        safe = await resume_safe(deps, state['company_id'], original['target'])
                        if not safe:
                            deps.store.add_event(case_id, key='resume_blocked', type='error', actor='awasi',
                                text='Agent kept paused: another enabled destination lacks approval.', now=deps.clock())
                    if safe:
                        ok = await verified_action(deps, case_id, pb_id, original['target'], employee, original)
                        # Recheck the removed connection after resuming as well.
                        if ok and choice == 'remove_connection':
                            ok = await call_playbook(deps.playbooks['workbench.disable_destination'].verify,
                                target_for(state, 'workbench.disable_destination'), deps)
    except Exception:
        ok = False
    if ok and deps.platform_store:
        deps.platform_store.enqueue(state['company_id'], 'awasi', 'manual', deps.clock())
    deps.store.close_case(case_id, status='resolved' if ok else 'needs_review', now=deps.clock())
    return {**update, 'verified':bool(ok)}
