"""Short, deterministic Slack messages using observed facts and verified actions."""

LABELS = {
    'restrict':'No, make it company-only', 'intentional':"Yes, it's intentional",
    'keep_restricted':'Keep it company-only', 'undo':'Undo restriction',
    'remove_connection':'Remove the connection and resume', 'keep_paused':'Keep it paused',
    'allow_7_days':'Allow this destination for 7 days', 'approve':'Approve containment', 'reject':'Reject containment',
    'timeout':'Response deadline passed',
}


def message(key, payload, actions, case, format_role, escape):
    f = payload.get('facts', {})
    app = f.get('rule') == 'public_app_confidential_data'
    name = escape(str(f.get('asset_name') or ('Renewal Tracker' if app else 'Lead Enrichment Agent'))[:160])
    address = payload.get('addressed_to') or f.get('addressed_to')
    header = f'To: {escape(format_role(address))}\n\n' if address else ''
    count = f.get('record_count', 0)
    expected = 'workbench.restrict_app' if app else 'workbench.pause_agent'
    original = next((a for a in reversed(actions) if a['playbook'] == expected and not a.get('undo_of')), None)
    undo = next((a for a in reversed(actions) if original and a.get('undo_of') == original['id']), None)
    contained = bool(original and original['status']=='executed' and original.get('verified_at') is not None and not undo)
    restored = bool(undo and undo['status']=='executed' and undo.get('verified_at') is not None)
    minutes = max(1, int(payload.get('deadline_seconds', 900)/60))
    if key == 'workbench_public':
        return header + (f'*{name} was shared publicly*\nAnyone with its link could see CRM details for {count} customers. '
            'No anonymous views were observed in the last hour.\n\n'
            '*Your decision*\nWas this intentional? Make it company-only, or allow public sharing for 7 days.\n\n'
            f'If you do not respond within {minutes} minutes, Awasi will make it company-only.')
    if key == 'workbench_restricted':
        text = ('The restriction was undone. Public access was restored and verified.' if restored else
                'The app is company-only. Awasi verified that its public link no longer opens.' if contained else
                'The current sharing setting has not been verified.')
        return header + f'*{name}: sharing update*\n{text}\n\n*Your decision*\nKeep it company-only, or undo the restriction within {minutes} minutes. Undo restores public access and requires an approver to decide the next step.'
    if key in ('workbench_destination','workbench_owner_notice'):
        host = escape(str(f.get('host', 'an unknown destination'))[:160])
        sent = f.get('records_sent_1h', 0)
        observation = (f'It sent {sent} customer records to {host} in the last hour.' if sent else
                       f'It has a connection to {host}. No customer records were observed being sent in the last hour.')
        text = ('The agent was resumed and verified.' if restored else
                'Awasi paused the agent and verified the pause.' if contained else 'The pause has not been verified.')
        choice = ('The approver will decide what happens next.' if key=='workbench_owner_notice' else
                  'Remove the connection and resume the agent, keep it paused, or allow this destination for 7 days.')
        return header + (f'*{name}: unapproved connection*\n{observation} This destination is not approved by Helio.\n\n'
            f'*What Awasi did*\n{text} We can\'t recall any records already sent.'+
            f'\n\n*Next step*\n{choice}')
    if key == 'workbench_approval':
        action = 'make it company-only' if app else 'pause the agent'
        return header + f'*Approval needed for {name}*\nAn earlier override or the available evidence requires your approval.\n\n*Your decision*\nApprove to {action}, or reject to leave the current access unchanged.'
    if key == 'outcome':
        status = case.get('status', 'needs_review') if case else 'needs_review'
        if status != 'resolved': return f'*{name}: review needed*\nThe case remains unresolved. Check the case history for any uncertain changes or an earlier override.'
        choice = payload.get('human_choice')
        if choice in ('intentional','allow_7_days'):
            return f'*{name}: exception approved*\nThe exception lasts 7 days. Awasi will check the exposure again after it expires.'
        if choice == 'remove_connection': return f'*{name}: resolved*\nThe connection is disabled and the agent has resumed. Both changes were verified.'
        if choice == 'keep_paused': return f'*{name}: resolved*\nThe agent remains paused; state verified.'
        return f'*{name}: resolved*\nCompany-only access verified. Its public link no longer opens.'
    return header + f'Security review for {name}.'
