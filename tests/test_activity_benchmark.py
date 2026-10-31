"""Receipt qualification uses complete, durable evidence, not HTTP call timing."""
import json
from datetime import datetime, timezone

import pytest

from helio_security.activity_benchmark import summarize, source_snapshot, receipt_snapshot


def evidence(requests=5):
    events, receipts = [], []
    for index in range(requests * 4):
        event = {'sequence': index + 1, 'event_id': str(index), 'request_id': str(index // 4),
                 'source_instance_id': 'instance', 'event_type': (
                     'request.received', 'operation.completed', 'response.prepared', 'response.completed')[index % 4],
                 'occurred_at': datetime.fromtimestamp(1000, timezone.utc).isoformat(),
                 'payload_bytes': 700, 'acknowledged_at': 1000 + (index + 1) / 10}
        events.append(event)
        receipts.append({'sequence': index + 1, 'event_id': str(index),
                         'source_instance_id': 'instance', 'received_at': 1000 + index / 10})
    return events, receipts


def test_latency_percentiles_include_every_lifecycle_stage():
    events, receipts = evidence()
    result = summarize(events, receipts, expected_requests=5)
    assert result['events'] == 20 and result['requests'] == 5
    assert result['receipt_ms']['p95'] == pytest.approx(1800)
    assert result['acknowledgement_ms']['p95'] == pytest.approx(1900)
    assert result['target_met'] is True
    events[-2]['acknowledged_at'] = events[-1]['acknowledged_at'] = 1003
    assert summarize(events, receipts, expected_requests=5)['target_met'] is False


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'identity', 'clock', 'ack', 'stages', 'extra'])
def test_incomplete_or_inconsistent_evidence_cannot_pass(fault):
    events, receipts = evidence()
    if fault == 'missing':
        receipts.pop()
    elif fault == 'duplicate':
        receipts[-1] = receipts[0]
    elif fault == 'identity':
        receipts[-1]['source_instance_id'] = 'reset'
    elif fault == 'clock':
        receipts[-1]['received_at'] = 999
    elif fault == 'ack':
        events[-1]['acknowledged_at'] = None
    elif fault == 'stages':
        events[-1]['event_type'] = 'request.received'
    else:
        events.append(events[0])
    with pytest.raises(ValueError):
        summarize(events, receipts, expected_requests=5)


@pytest.mark.parametrize('options', [{'samples': 0}, {'samples': 201}, {'concurrency': 0},
                                     {'concurrency': 17}, {'warmup': -1}, {'timeout': 0}])
def test_unbounded_load_is_rejected_before_reading_credentials_or_calling_crm(tmp_path, options):
    from helio_security.activity_benchmark import qualify
    with pytest.raises(ValueError, match='samples'):
        qualify(tmp_path, **options)


@pytest.mark.parametrize('met,exit_code', [(True, 0), (False, 1)])
def test_cli_records_a_missed_target_and_returns_failure(tmp_path, monkeypatch, capsys, met, exit_code):
    from helio_security import activity_benchmark as benchmark
    output = tmp_path/'measurement.json'
    monkeypatch.setattr('sys.argv', ['benchmark', 'run', '--output', str(output)])
    monkeypatch.setattr(benchmark, 'qualify', lambda *a, **kw: {'target_met': met})
    assert benchmark.main() == exit_code
    assert json.loads(output.read_text())['target_met'] is met
    assert json.loads(capsys.readouterr().out)['target_met'] is met


def test_snapshots_measure_real_http_sqlite_delivery_without_exporting_contents(tmp_path, directory, monkeypatch):
    import secrets
    import sqlite3
    import time
    import urllib.request
    from helio_crm.api import create_app
    from helio_security.webhooks import create_app as receiver
    from test_graph import serve

    tokens = {name: secrets.token_urlsafe(32) for name in ('admin', 'maya', 'agent')}
    key = secrets.token_urlsafe(32)
    crm_path, inbox_path = tmp_path/'crm.sqlite3', tmp_path/'inbox.sqlite3'
    monkeypatch.setenv('AWASI_ACTIVITY_WORKER_ENABLED', '0')
    with serve(receiver(inbox_path, {'crm-1': {'secret': key, 'company_id': 'helio', 'source': 'helio-crm'}}, read_tokens={})) as ingest:
        monkeypatch.setenv('CRM_WEBHOOK_ENABLED', '1')
        monkeypatch.setenv('CRM_WEBHOOK_ALLOW_LOCAL', '1')
        monkeypatch.setenv('CRM_WEBHOOK_SECRET', key)
        monkeypatch.setenv('CRM_WEBHOOK_URL', ingest + '/webhooks/crm/activity')
        with serve(create_app(crm_path, tokens, directory.employees, activity_enabled=True)) as crm:
            head = source_snapshot(crm_path)
            request = urllib.request.Request(crm+'/customers', headers={'Authorization': 'Bearer '+tokens['agent']})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert len(json.load(response)) == 6
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                source = source_snapshot(crm_path, after=head['high_watermark'])
                if len(source['events']) == 4 and all(e['acknowledged_at'] is not None for e in source['events']):
                    break
                time.sleep(.02)
            receipts = receipt_snapshot(inbox_path, head['source_instance_id'], head['high_watermark'])
            result = summarize(source['events'], receipts, expected_requests=1)
            assert result['events'] == 4
            assert result['receipt_ms']['min'] >= 0
            serialized = json.dumps([source, receipts, result])
            assert all(secret not in serialized for secret in [*tokens.values(), key])
            assert 'Northstar' not in serialized and 'notes' not in serialized
            with sqlite3.connect(crm_path) as db:
                assert db.execute('SELECT count(*) FROM activity_events').fetchone()[0] == 4
