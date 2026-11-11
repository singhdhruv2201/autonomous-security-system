"""Recovery across closed-case checkpoints and Slack startup outages."""
import asyncio

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from helio_security.agent_worker import AgentWorker, SlackTransport
from helio_security.platform_store import PlatformStore
from helio_security.slack_app import SlackNotifier, SlackConfig
from helio_security.workflow import build_graph
from test_case_recovery import setup_case
from test_slack_app import FakeClient


@pytest.mark.parametrize('after_enqueue', [False, True])
def test_closed_case_report_recovers_after_restart_without_duplicate_post(tmp_path, after_enqueue):
    async def scenario():
        deps, graph, cid, config, state, pb = setup_case(tmp_path, sqlite=True)
        pb.fail = True
        client = FakeClient()
        cfg = SlackConfig(channel='C123', default_user='U123')
        notifier = SlackNotifier(deps.store, client, cfg)
        class InterruptedNotifier:
            async def notify(self, *args):
                if after_enqueue:
                    await notifier.notify(*args)
                raise RuntimeError('simulated crash at outcome enqueue boundary')
        deps.notifier = InterruptedNotifier()
        with pytest.raises(RuntimeError):
            await graph.ainvoke(state, config)
        assert deps.store.get_case(cid)['status'] == 'needs_review'
        assert (await graph.aget_state(config)).next == ('report',)
        deps.notifier = SlackNotifier(deps.store, client, cfg)
        deps.checkpointer = SqliteSaver.from_conn_string(str(tmp_path / 'checkpoints.sqlite3'))
        platform = PlatformStore(tmp_path / 'platform.sqlite3'); platform.initialize()
        worker = AgentWorker(platform, deps.store, deps, graph=build_graph(deps))
        for _ in range(3):
            await worker.run_once()
        assert len(client.posts) == 1
        assert 'requires review' in client.posts[0]['text'].lower()
        assert (await worker.graph.aget_state(config)).next == ()
        assert deps.store.get_case(cid)['status'] == 'needs_review'
        assert pb.calls == 1
        assert deps.store.recovery_cases('helio') == []
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_slack_startup_retry_and_shutdown_during_connection(cancel):
    from helio_security.agent_worker import run_worker

    async def scenario():
        retrying = asyncio.Event()
        cancelled = asyncio.Event()
        stop = asyncio.Event()

        class Handler:
            calls = 0
            closed = False

            async def connect_async(self):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError('Slack unavailable')
                retrying.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            async def close_async(self):
                self.closed = True

        class Worker:
            async def run_forever(self):
                await stop.wait()

        handler = Handler()
        transport = SlackTransport(handler)
        task = asyncio.create_task(run_worker(Worker(), handler, transport, retry_seconds=0))
        try:
            await asyncio.wait_for(retrying.wait(), 1)
            assert await transport.check_status() == 'connection_failed'
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            else:
                stop.set()
                await asyncio.wait_for(task, 1)
            assert cancelled.is_set()
            assert handler.closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_slack_transport_recovers_from_startup_failure():
    class Handler:
        async def is_connected(self):
            return True

    transport = SlackTransport(Handler())
    transport.record_startup_failure(ConnectionError())
    assert asyncio.run(transport.check_status()) == 'connected'


def test_worker_runs_without_slack_configuration():
    from helio_security.agent_worker import run_worker

    class Worker:
        ran = False

        async def run_forever(self):
            self.ran = True

    worker = Worker()
    asyncio.run(run_worker(worker))
    assert worker.ran


def test_recovery_does_not_reopen_a_manually_closed_human_wait(tmp_path):
    async def scenario():
        deps, graph, cid, config, state, pb = setup_case(tmp_path)
        await graph.ainvoke(state, config)
        assert (await graph.aget_state(config)).next == ('wait_human',)
        deps.store.close_case(cid, status='dismissed', now=deps.clock())
        platform = PlatformStore(tmp_path / 'platform.sqlite3')
        platform.initialize()
        worker = AgentWorker(platform, deps.store, deps, graph=graph)
        await worker.run_once()
        await worker.run_once()
        assert deps.store.get_case(cid)['status'] == 'dismissed'
        assert deps.store.recovery_cases('helio') == []
        assert (await graph.aget_state(config)).next == ('wait_human',)
        assert pb.calls == 1

    asyncio.run(scenario())


def test_worker_processes_cases_while_slack_connect_waits_and_cleans_up(tmp_path):
    from helio_security.agent_worker import run_worker
    async def scenario():
        deps, graph, cid, config, state, pb = setup_case(tmp_path)
        platform = PlatformStore(deps.store.path); platform.initialize()
        deps.platform_store = platform
        import time
        now = time.time()
        with platform.connection() as db:
            db.execute("INSERT INTO findings VALUES (?, ?, 'inactive_owner_agent_access', 'high', 'open', 'current', ?, ?, ?, ?, NULL, ?, 'inactive_owner_active_access')",
                ('finding-aaa','helio',now,now,now,now,'{"agent_id":"agent:sales-research","employee_status":"inactive","classification":"confidential","record_count":6}'))
        release = asyncio.Event()
        attempted = asyncio.Event()
        class Handler:
            connected = False
            closed = False
            client = None
            async def connect_async(self):
                attempted.set()
                await release.wait()
                self.connected = True
            async def is_connected(self):
                return self.connected
            async def close_async(self):
                self.closed = True
                self.connected = False
        handler = Handler()
        transport = SlackTransport(handler)
        worker = AgentWorker(platform, deps.store, deps, graph=graph, slack_transport=transport, interval=0.01)
        task = asyncio.create_task(run_worker(worker, handler, transport))
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            async def wait_until(check):
                while not check():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_until(lambda: platform.runtime('helio').get('agent_worker')), 1)
            assert pb.calls == 1
            assert deps.store.get_case(cid)['status'] == 'waiting_approver'
            assert platform.runtime('helio')['slack']['value']['status'] == 'disconnected'
            release.set()
            await asyncio.wait_for(wait_until(lambda: platform.runtime('helio')['slack']['value']['status']=='connected'), 1)
        finally:
            worker._stop_event.set()
            worker.wake()
            await asyncio.wait_for(task, 1)
        assert handler.closed
    asyncio.run(scenario())
