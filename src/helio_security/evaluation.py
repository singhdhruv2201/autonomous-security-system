"""Opt-in offline evidence evaluation. No Slack messages or remediation writes.

Fixtures are compact, synthetic metadata snapshots of the acceptance scenarios;
this measures assessment quality, not tool selection or connector reliability.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from .investigator import Assessment, LimitedChatModel, ModelLimiter, SYSTEM_PROMPT, extract_evidence_ids
from .live_ledger import LiveLedger

CASES = [
    dict(id='orphan', control=False, owner_active=False, expected_causes=['offboarding_gap'],
         required_refs=['agent:sales-research','emp-maya-chen'], evidence={
             'rule':'inactive_owner_agent_access', 'agent_id':'agent:sales-research',
             'owner':{'employee_id':'emp-maya-chen','status':'inactive'},
             'principal_status':'active','grant_id':'grant-sales-agent','permission':'read',
             'resource_id':'customer-database','record_count':6}),
    dict(id='public', control=False, owner_active=True, expected_causes=['unknown'],
         required_refs=['app-renewal-tracker'], evidence={
             'rule':'public_app_confidential_data','app_id':'app-renewal-tracker',
             'owner':{'employee_id':'emp-luca-rossi','status':'active'},'visibility':'public',
             'data_classification':'confidential','resource_id':'customer-database',
             'anonymous_views':0,'record_count':6,'owner_intent':'not established'}),
    dict(id='external', control=False, owner_active=True, expected_causes=['unapproved_tool'],
         required_refs=['agent-lead-enrichment'], evidence={
             'rule':'agent_unapproved_destination','agent_id':'agent-lead-enrichment',
             'owner':{'employee_id':'emp-zara-ali','status':'active'},
             'connection_id':'agent-lead-enrichment:enrich-leads','destination':'enrich-leads',
             'approved':False,'enabled':True,'sent_records':6,'data_classification':'confidential'}),
    dict(id='company_only', control=True, owner_active=True, expected_causes=['unknown'],
         required_refs=['app-support-lookup'], evidence={
             'app_id':'app-support-lookup','visibility':'company','anonymous_http_status':404,
             'owner':{'employee_id':'emp-nina-desai','status':'active'},'violations':[]}),
    dict(id='approved_destination', control=True, owner_active=True, expected_causes=['unknown'],
         required_refs=['agent-renewal-digest'], evidence={
             'agent_id':'agent-renewal-digest','destination':'partner-api','approved':True,
             'enabled':True,'sent_records':6,'owner':{'employee_id':'emp-hannah-reed','status':'active'},'violations':[]}),
    dict(id='active_owner', control=True, owner_active=True, expected_causes=['unknown'],
         required_refs=['agent:sales-research','emp-maya-chen'], evidence={
             'agent_id':'agent:sales-research','owner':{'employee_id':'emp-maya-chen','status':'active'},
             'principal_status':'active','permission':'read','resource_id':'customer-database','violations':[]}),
]


def score(case, raw):
    """Score original provider output before production sanitization drops bad IDs."""
    result=dict(cause_correct=False,evidence_valid=False,owner_question_valid=False,control_safe=False)
    try:
        assessment=Assessment.model_validate(raw)
        refs=set(assessment.evidence_refs)
        result.update(
            cause_correct=assessment.likely_cause in case['expected_causes'],
            evidence_valid=bool(refs) and refs <= extract_evidence_ids(case['evidence']) and set(case['required_refs']) <= refs,
            owner_question_valid=case['owner_active'] or not assessment.question_for_owner,
            control_safe=not case['control'] or assessment.recommended_playbook is None)
    except (ValueError, TypeError):
        pass
    result['passed']=all(result.values())
    return result


async def evaluate(model, ledger, output, model_name):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation protects against accidental paid reruns and concurrent CLI runs.
    with output.open('x') as stream:
        json.dump({'status':'running'},stream)
    report=dict(created_at=datetime.now(timezone.utc).isoformat(),model=model_name,
                fixture_sha256=hashlib.sha256(json.dumps(CASES,sort_keys=True).encode()).hexdigest(),
                prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                scope='offline assessment on synthetic metadata; not MCP execution',cases=[],passed=False)
    wrapped=LimitedChatModel(model,ledger=ledger,limiter=ModelLimiter(6),model_name=model_name,max_output=1024,
                             tool_schemas=[Assessment.model_json_schema()])
    stopped=False
    for case in CASES:
        row={'id':case['id']}
        if stopped:
            report['cases'].append({**row,'passed':False,'error':'not_run_after_request_failure'})
            continue
        try:
            response=await wrapped.ainvoke([SystemMessage(content=SYSTEM_PROMPT),HumanMessage(content=
                'The following is the complete recorded read-tool evidence. Assess whether it establishes a security problem; '
                'do not assume a finding exists. Use unknown when no cause is established.\n'+json.dumps(case['evidence']))])
            calls=response.tool_calls
            if len(calls)!=1 or calls[0]['name']!='Assessment': raise ValueError('Expected one assessment')
            raw=calls[0]['args']
            row.update(score(case,raw));row['assessment']=raw
        except Exception as exc:
            row.update(passed=False,error=type(exc).__name__)
            stopped=True
        report['cases'].append(row)
        output.write_text(json.dumps(report,indent=2)+'\n')
    report['passed']=all(row['passed'] for row in report['cases'])
    report['usage']=ledger.get_data()
    output.write_text(json.dumps(report,indent=2)+'\n')
    return report


def build_eval_model(model_name):
    # Provider strict schemas do not support length bounds. Keep those in the
    # local Assessment validator; require every nullable field explicitly.
    schema=Assessment.model_json_schema()
    def supported(value):
        if isinstance(value,dict):
            return {k:supported(v) for k,v in value.items() if k not in ('maxLength','maxItems','default')}
        if isinstance(value,list): return [supported(v) for v in value]
        return value
    schema=supported(schema)
    schema['required']=list(schema['properties'])
    tool=dict(name='Assessment',description='Return the evidence-grounded security assessment.',
              input_schema=schema,strict=True)
    return ChatAnthropic(model_name=model_name,max_tokens=1024,max_retries=0,timeout=45).bind_tools(
        [tool],tool_choice='Assessment')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Explicitly authorize paid requests')
    parser.add_argument('--output',default='data/phase7-evaluation.json')
    parser.add_argument('--ledger',default='data/phase7_live_ledger.json')
    parser.add_argument('--max-spend-usd',type=float,default=.50)
    parser.add_argument('--max-requests',type=int,default=40)
    args=parser.parse_args()
    if not args.live: parser.error('--live is required; this uses paid API credits')
    if not 0 < args.max_spend_usd <= .50 or not 1 <= args.max_requests <= 40:
        parser.error('Phase 7 allowance must be at most $0.50 and 40 requests')
    from dotenv import load_dotenv
    load_dotenv()
    if not os.getenv('ANTHROPIC_API_KEY'): parser.error('ANTHROPIC_API_KEY is missing')
    model_name=os.getenv('AWASI_MODEL','claude-haiku-5-5')
    ledger=LiveLedger(args.ledger,max_spend_usd=args.max_spend_usd,max_requests=args.max_requests)
    model=build_eval_model(model_name)
    report=asyncio.run(evaluate(model,ledger,args.output,model_name))
    for row in report['cases']: print(row['id']+': '+('PASS' if row['passed'] else 'FAIL'))
    print('Report: '+args.output)
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__': main()
