"""Investigation budgets must leave room to return a grounded assessment."""
import asyncio
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from test_investigator import FakeBindableChatModel
from helio_security.investigator import ModelInvestigator

COPY = {'headline': 'An app exposes customer details', 'context': 'Renewal Tracker is publicly accessible.',
        'impact': 'People outside the company could access confidential records.'}


@pytest.mark.parametrize('batches', [[4, 2], [1, 1], [7]])
def test_budgeted_reads_finish_with_model_message_instead_of_aborting(batches):
    bindings, calls = [], []

    @tool
    def get_finding(finding_id: str) -> dict:
        """Read finding evidence."""
        calls.append(finding_id)
        return {'id': finding_id}

    class AdaptiveModel(FakeBindableChatModel):
        def bind_tools(self, tools, **kwargs):
            bindings.append({getattr(t, 'name', None) for t in tools})
            return self

    def responses():
        for round_no in range(5):
            if bindings[-1] == {'Assessment'}:
                yield AIMessage(content='', tool_calls=[{'name': 'Assessment', 'id': 'final', 'args': {
                    'summary': 'Public customer access observed.', 'impact': 'Confidential data exposed.',
                    'likely_cause': 'accidental_sharing', 'confidence': 'medium',
                    'evidence_refs': ['finding-1'], 'slack_message': COPY}}])
                return
            count = batches[round_no] if round_no < len(batches) else 1
            yield AIMessage(content='', tool_calls=[{'name': 'get_finding', 'id': f'{round_no}-{i}',
                'args': {'finding_id': 'finding-1'}} for i in range(count)])

    result = asyncio.run(ModelInvestigator(model=AdaptiveModel(messages=responses()), tools=[get_finding])
                         .investigate('finding-1', {'rule': 'public_app_confidential_data'}))
    assert result['source'] == 'model'
    assert result['slack_message'] == COPY
    assert len(calls) <= 6
    assert len(bindings) <= 4


def test_fallback_keeps_specific_safe_reason():
    class FailingInvestigator(ModelInvestigator):
        def _build_model(self):
            raise RuntimeError('provider failed with secret xoxb-do-not-log')
    result = asyncio.run(FailingInvestigator().investigate('finding-1', {'rule': 'public_app_confidential_data'}))
    assert result['fallback_reason'] == 'runtime_error'


@pytest.mark.parametrize('invalid', [
    {'slack_message': {**COPY, 'context': 'x' * 261}},
    {'slack_message': None},
    {'evidence_refs': ['invented-id'], 'slack_message': COPY},
    {'summary': 'x' * 601, 'slack_message': COPY},
])
def test_invalid_copy_is_repaired_automatically_within_call_budget(invalid, tmp_path):
    @tool
    def get_finding(finding_id: str) -> dict:
        """Read finding evidence."""
        return {'id': finding_id}
    assessment = {'summary': 'Public access.', 'impact': 'Customer exposure.',
                  'likely_cause': 'unknown', 'confidence': 'medium', 'evidence_refs': ['finding-1']}
    messages = [AIMessage(content='', tool_calls=[{'name': 'get_finding', 'id': 'read', 'args': {'finding_id': 'finding-1'}}]),
                AIMessage(content='', tool_calls=[{'name': 'Assessment', 'id': 'bad',
                    'args': {**assessment, **invalid}}]),
                AIMessage(content='', tool_calls=[{'name': 'Assessment', 'id': 'repaired',
                    'args': {**assessment, 'slack_message': COPY}}])]
    from helio_security.live_ledger import LiveLedger
    ledger = LiveLedger(tmp_path / 'ledger.json')
    result = asyncio.run(ModelInvestigator(model=FakeBindableChatModel(messages=iter(messages)), tools=[get_finding], ledger=ledger)
                         .investigate('finding-1', {'rule': 'public_app_confidential_data'}))
    assert result['source'] == 'model'
    assert result['slack_message'] == COPY

    assert ledger.get_data()['total_requests'] == 3
