import json
import pytest
from helio_security.slack_app import SlackNotifier, SlackConfig
from helio_security.investigator import scripted_fallback
from test_phase5_workflow import setup, answer
from test_playbooks_workbench import workbench
from test_phase5_graph import APP_RULE,AGENT_RULE

@pytest.mark.parametrize('rule,leaking,expected',[(APP_RULE,False,'Luca Rossi'),(APP_RULE,True,'company-only'),(AGENT_RULE,True,'Remove the connection and resume')])
def test_slack_workbench_copy_and_buttons(tmp_path,workbench,rule,leaking,expected):
    deps,g,cid,cfg,r=setup(tmp_path,workbench,rule,leaking)
    notifier=SlackNotifier(deps.store,None,SlackConfig(channel='test'))
    q=r['question']
    body=notifier.render_body(cid,q['key'],q)
    text=json.dumps(body)
    assert expected in text
    assert 'Sales Research' not in text
    if rule==AGENT_RULE:
        assert "can't recall" in text
        assert 'paused' in text
        assert 'Allow this destination for 7 days' in text
    elif not leaking:
        assert '15 minutes' in text and '7 days' in text

@pytest.mark.parametrize('rule,expected',[(APP_RULE,'workbench.restrict_app'),(AGENT_RULE,'workbench.pause_agent')])
def test_fallback_matches_workbench_rule(rule,expected):
    value=scripted_fallback('id',{'rule':rule,'app_id':'app','agent_id':'agent','record_count':6})
    assert value['recommended_playbook']==expected
    assert 'offboard' not in value['summary']
