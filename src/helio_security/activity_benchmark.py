"""Repeatable local Compose receipt qualification; metadata-only output."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import time
import urllib.request

from .connectors import NoRedirect

STAGES = {'request.received', 'operation.completed', 'response.prepared', 'response.completed'}


def distribution(values):
    ordered = sorted(values)
    return {'min': min(ordered), 'median': statistics.median(ordered),
            'p95': ordered[math.ceil(.95 * len(ordered)) - 1], 'max': max(ordered)}


def summarize(events, receipts, *, expected_requests):
    """Nearest-rank p95; missing evidence and clock anomalies invalidate the run."""
    identity = lambda row: (row['source_instance_id'], row['event_id'], row['sequence'])
    if len(events) != expected_requests * 4 or len(receipts) != len(events) or not events:
        raise ValueError('Expected exactly four source events and receipts per request')
    received = {identity(row): row['received_at'] for row in receipts}
    if len(received) != len(receipts) or len({identity(row) for row in events}) != len(events):
        raise ValueError('Duplicate evidence')
    requests = {}
    latency, acknowledgement = [], []
    for row in events:
        requests.setdefault(row['request_id'], []).append(row['event_type'])
        occurred = datetime.fromisoformat(row['occurred_at'])
        arrival = received.get(identity(row))
        ack = row['acknowledged_at']
        if occurred.tzinfo is None or arrival is None or ack is None:
            raise ValueError('Unproven receipt or acknowledgement')
        delays = [(arrival - occurred.timestamp()) * 1000, (ack - occurred.timestamp()) * 1000]
        if any(not math.isfinite(value) or value < 0 for value in delays):
            raise ValueError('Invalid clock evidence')
        latency.append(delays[0])
        acknowledgement.append(delays[1])
    if len(requests) != expected_requests or any(Counter(stages) != Counter(STAGES) for stages in requests.values()):
        raise ValueError('Incomplete or duplicate request stages')
    receipt_ms, acknowledgement_ms = distribution(latency), distribution(acknowledgement)
    return {'requests': expected_requests, 'events': len(events), 'receipt_ms': receipt_ms,
            'acknowledgement_ms': acknowledgement_ms,
            'event_payload_bytes': distribution([row['payload_bytes'] for row in events]),
            'target_ms': 2000, 'target_met': max(receipt_ms['p95'], acknowledgement_ms['p95']) <= 2000}


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def source_snapshot(path, after=None):
    with closing(readonly(path)) as db, db:
        db.execute('BEGIN')
        instance = db.execute("SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()[0]
        result = {'source_instance_id': instance,
                  'high_watermark': db.execute("SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='activity_events'),0)").fetchone()[0],
                  'retained_security': db.execute('SELECT count(*) FROM activity_events').fetchone()[0],
                  'retained_operational': db.execute('SELECT count(*) FROM operational_events').fetchone()[0],
                  'undelivered': db.execute("SELECT count(*) FROM activity_outbox WHERE status!='delivered'").fetchone()[0],
                  'events': []}
        if after is not None:
            for row in db.execute('SELECT * FROM activity_events WHERE sequence>? ORDER BY sequence', (after,)):
                event = dict(row)
                payload = json.loads(event.pop('payload'))
                result['events'].append({**event, 'source_instance_id': instance,
                    'event_type': payload['event_type'],
                    'payload_bytes': len(json.dumps({**payload, **event}, sort_keys=True, separators=(',', ':')).encode()),
                    'acknowledged_at': db.execute("SELECT min(occurred_at) FROM delivery_audit WHERE event_sequence=? AND outcome='delivered'", (row['sequence'],)).fetchone()[0]})
        return result


def receipt_snapshot(path, instance, after):
    with closing(readonly(path)) as db:
        return [dict(row) for row in db.execute('''SELECT source_instance_id,event_id,sequence,received_at FROM inbox
            WHERE company_id='helio' AND source='helio-crm' AND source_instance_id=? AND sequence>? ORDER BY sequence''', (instance, after))]


def command(args, root):
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Local command failed; check Compose service health')
    return result.stdout.strip()


def snapshot(root, kind, after=None, instance=None):
    args = ['docker', 'compose', 'exec', '-T', 'crm' if kind == 'source' else 'awasi-ingest',
            'python', '-m', 'helio_security.activity_benchmark', 'snapshot', kind]
    if after is not None:
        args += ['--after', str(after)]
    if instance:
        args += ['--instance', instance]
    return json.loads(command(args, root))


def qualify(root, *, samples=100, concurrency=4, warmup=10, timeout=120):
    if not 1 <= samples <= 200 or not 1 <= concurrency <= 16 or not 0 <= warmup <= 100 or not 1 <= timeout <= 600:
        raise ValueError('Use 1–200 samples, 1–16 concurrency, 0–100 warmups and a 1–600 second timeout')
    env = dict(line.split('=', 1) for line in (root/'.env').read_text().splitlines()
               if '=' in line and not line.lstrip().startswith('#'))
    token = env.get('CRM_AGENT_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Run make setup before qualification')
    head = snapshot(root, 'source')
    if not head['capture_enabled'] or not head['delivery_enabled'] or head['undelivered']:
        raise ValueError('Enable capture and delivery and drain the existing delivery queue first')
    if head['retained_security'] + 4*(samples+warmup) > 10000 or head['retained_operational'] > 9000:
        raise ValueError('Insufficient retained capacity; preserve evidence and review the local environment')

    def request(_):
        req = urllib.request.Request('http://127.0.0.1:8001/customers', headers={'Authorization': 'Bearer '+token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=10) as response:
            body = response.read(2*1024*1024+1)
            if response.status != 200 or len(body) > 2*1024*1024 or len(json.loads(body)) != 6:
                raise ValueError('Expected the six synthetic CRM customers')
            return len(body)

    def batch(count):
        start = snapshot(root, 'source')
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            sizes = list(pool.map(request, range(count)))
        submitted = time.monotonic()-started
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            source = snapshot(root, 'source', start['high_watermark'])
            if source['source_instance_id'] != start['source_instance_id'] or len(source['events']) > count*4:
                raise ValueError('Source reset or concurrent CRM activity invalidated this sample')
            if len(source['events']) == count*4 and all(e['acknowledged_at'] is not None for e in source['events']):
                receipts = snapshot(root, 'receipt', start['high_watermark'], start['source_instance_id'])
                result = summarize(source['events'], receipts, expected_requests=count)
                return {**result, 'response_bytes': distribution(sizes), 'submit_seconds': submitted,
                        'drain_seconds': time.monotonic()-started, 'sequence_start': start['high_watermark']+1,
                        'sequence_end': source['high_watermark']}
            time.sleep(.25)
        raise ValueError('Timed out waiting for all durable receipts and webhook acknowledgements')

    if warmup:
        batch(warmup)
    result = batch(samples)
    hardware = {'host_os': platform.platform(), 'host_arch': platform.machine(),
                'docker': command(['docker', 'version', '--format', '{{.Server.Version}}'], root),
                'docker_vm': json.loads(command(['docker', 'info', '--format', '{"arch":"{{.Architecture}}","cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}'], root)),
                'container_python': command(['docker', 'compose', 'exec', '-T', 'crm', 'python', '--version'], root)}
    if platform.system() == 'Darwin':
        hardware['cpu'] = command(['sysctl', '-n', 'machdep.cpu.brand_string'], root)
        hardware['host_memory_bytes'] = int(command(['sysctl', '-n', 'hw.memsize'], root))
    images = {}
    for service in ('crm', 'awasi-ingest'):
        container = command(['docker', 'compose', 'ps', '-q', service], root)
        images[service] = command(['docker', 'inspect', '--format', '{{.Image}}', container], root)
    return {**result, 'measured_at': datetime.now(timezone.utc).isoformat(), 'concurrency': concurrency,
            'warmup_requests': warmup, 'load': 'Closed-loop GET /customers with no pacing; four events per request',
            'percentile_method': 'nearest rank over every event, warmup excluded',
            'receipt_definition': 'Persisted inbox admission timestamp minus source occurred_at; push and recovery share first receipt',
            'acknowledgement_definition': 'CRM recorded HTTP 200/202 after durable receiver commit, minus source occurred_at; conservative receipt bound',
            'recovery_enabled': snapshot(root, 'receipt')['worker_enabled'],
            'hardware': hardware, 'running_images': images, 'git_commit': command(['git', 'rev-parse', 'HEAD'], root),
            'git_dirty': bool(command(['git', 'status', '--porcelain'], root))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    run = modes.add_parser('run', help='Generate bounded read-only CRM load against the local Compose stack')
    run.add_argument('--samples', type=int, default=100)
    run.add_argument('--concurrency', type=int, default=4)
    run.add_argument('--warmup', type=int, default=10)
    run.add_argument('--timeout', type=int, default=120)
    run.add_argument('--output', type=Path, default=Path('work/activity-latency.json'))
    read = modes.add_parser('snapshot', help='Internal metadata-only, read-only container probe')
    read.add_argument('kind', choices=['source', 'receipt'])
    read.add_argument('--after', type=int)
    read.add_argument('--instance')
    args = parser.parse_args()
    try:
        if args.mode == 'snapshot':
            if args.kind == 'source':
                result = {**source_snapshot(os.environ['CRM_DB_PATH'], args.after),
                          'capture_enabled': os.getenv('CRM_ACTIVITY_ENABLED') == '1',
                          'delivery_enabled': os.getenv('CRM_WEBHOOK_ENABLED') == '1'}
            elif args.after is None:
                result = {'worker_enabled': os.getenv('AWASI_ACTIVITY_WORKER_ENABLED') == '1'}
            else:
                result = receipt_snapshot(os.environ['AWASI_INBOX_DB_PATH'], args.instance, args.after)
        else:
            result = qualify(Path.cwd(), samples=args.samples, concurrency=args.concurrency,
                             warmup=args.warmup, timeout=args.timeout)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2))
        return 0 if args.mode == 'snapshot' or result['target_met'] else 1
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        # Never echo response bodies, credentials or subprocess environments.
        print('Qualification incomplete: verify local configuration, delivery queue, capacity and exclusive CRM load.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
