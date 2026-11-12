"""Tests for local trace span recording, redaction, and API exposure."""
import asyncio
from pathlib import Path
import time
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from helio_security.cases import CaseStore
from helio_security.tracing import CaseTracer, Tracer, sanitize_error, filter_attributes


def test_record_and_get_spans_in_case_store(tmp_path):
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-trace-1", route="investigating", now=time.time())

    span_data = {
        "id": "span-1",
        "case_id": cid,
        "parent_id": None,
        "kind": "model",
        "name": "claude-haiku-5-5",
        "started_at": 100.0,
        "ended_at": 101.5,
        "input_chars": 250,
        "output_chars": 80,
        "tokens_in": 60,
        "tokens_out": 20,
        "error": None,
        "attributes": {"model": "claude-haiku-5-5"},
    }
    store.record_span(span_data)

    spans = store.get_spans(cid)
    assert len(spans) == 1
    s = spans[0]
    assert s["id"] == "span-1"
    assert s["case_id"] == cid
    assert s["kind"] == "model"
    assert s["input_chars"] == 250
    assert s["tokens_in"] == 60
    assert s["tokens_out"] == 20
    assert s["attributes"] == {"model": "claude-haiku-5-5"}

    # get_case_full includes spans
    full = store.get_case_full(cid)
    assert "spans" in full
    assert len(full["spans"]) == 1
    assert full["spans"][0]["id"] == "span-1"


def test_sanitize_error_and_filter_attributes():
    err = RuntimeError("Failed with authorization Bearer xoxb-secret-123456789 and customer SSN 000-00-0000")
    sanitized = sanitize_error(err)
    assert "secret" not in sanitized
    assert "xoxb" not in sanitized
    assert "000-00" not in sanitized
    assert sanitized in ("runtime_error", "RuntimeError")

    raw_args = {
        "finding_id": "finding-123",
        "entity_id": "agent:sales-research",
        "secret_token": "xoxb-do-not-log",
        "customer_email": "alice@customer.com",
        "limit": 10,
    }
    filtered = filter_attributes(raw_args)
    assert filtered == {
        "finding_id": "finding-123",
        "entity_id": "agent:sales-research",
        "limit": 10,
    }
    assert "secret_token" not in filtered
    assert "customer_email" not in filtered


def test_tracer_records_spans_without_raw_secrets(tmp_path):
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-trace-2", route="investigating", now=time.time())

    tracer = Tracer(store, cid)
    with tracer.span("tool", "get_finding", attributes={"finding_id": "finding-trace-2", "secret": "shh"}):
        time.sleep(0.01)

    spans = store.get_spans(cid)
    assert len(spans) == 1
    assert spans[0]["name"] == "get_finding"
    assert spans[0]["kind"] == "tool"
    assert spans[0]["attributes"] == {"finding_id": "finding-trace-2"}
    assert "secret" not in spans[0]["attributes"]


def test_case_tracer_langchain_callback(tmp_path):
    import uuid
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-trace-3", route="investigating", now=time.time())

    callback = CaseTracer(store, cid)
    run_id = uuid.uuid4()

    # Simulate model start
    msg = HumanMessage(content="Investigate finding with sensitive prompt xoxb-secret")
    callback.on_chat_model_start(
        {"name": "ChatAnthropic"},
        [[msg]],
        run_id=run_id,
        invocation_params={"model_name": "claude-haiku-5-5"},
    )

    # Simulate model end
    ai_msg = AIMessage(
        content="Assessment summary",
        usage_metadata={"input_tokens": 150, "output_tokens": 40, "total_tokens": 190},
    )
    llm_result = LLMResult(generations=[[ChatGeneration(message=ai_msg)]])
    callback.on_llm_end(llm_result, run_id=run_id)

    spans = store.get_spans(cid)
    assert len(spans) == 1
    span = spans[0]
    assert span["kind"] == "model"
    assert span["tokens_in"] == 150
    assert span["tokens_out"] == 40
    assert span["input_chars"] == len(msg.content)
    assert span["output_chars"] == len(ai_msg.content)
    # Check that raw prompt text was NOT stored in attributes or anywhere in span row
    span_str = str(span)
    assert "xoxb-secret" not in span_str
    assert "sensitive prompt" not in span_str


def test_api_get_case_returns_spans(tmp_path):
    from fastapi.testclient import TestClient
    from helio_security.api import create_app
    from helio_security.settings import Settings

    settings = Settings(
        operator_token="o" * 32,
        viewer_token="v" * 32,
        observer_token="b" * 32,
        db_path=tmp_path / "security.sqlite3",
        company_id="helio",
        sync_interval=0,
    )
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-api-trace", route="investigating", now=time.time())
    store.record_span({
        "id": "span-api-1",
        "case_id": cid,
        "parent_id": None,
        "kind": "tool",
        "name": "get_finding",
        "started_at": 10.0,
        "ended_at": 10.5,
        "input_chars": 20,
        "output_chars": 100,
        "tokens_in": None,
        "tokens_out": None,
        "error": None,
        "attributes": {"finding_id": "finding-api-trace"},
    })

    class FakeGraph:
        company_id = "helio"
        def initialize(self): pass
        def status(self): return {"initialized": True, "stale": False}

    app = create_app(settings=settings, graph=FakeGraph(), store=store, start_worker=False)
    with TestClient(app) as client:
        resp = client.get(f"/cases/{cid}", headers={"Authorization": f"Bearer {'v'*32}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "spans" in data
        assert len(data["spans"]) == 1
        assert data["spans"][0]["id"] == "span-api-1"
        assert data["spans"][0]["name"] == "get_finding"


def test_get_spans_limit_and_offset(tmp_path):
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-trace-limit", route="investigating", now=time.time())

    for i in range(10):
        store.record_span({
            "id": f"span-{i}",
            "case_id": cid,
            "parent_id": None,
            "kind": "tool",
            "name": f"tool-{i}",
            "started_at": 100.0 + i,
            "ended_at": 101.0 + i,
            "input_chars": 10,
            "output_chars": 10,
            "tokens_in": None,
            "tokens_out": None,
            "error": None,
            "attributes": {},
        })

    # Test limit
    spans_5 = store.get_spans(cid, limit=5, offset=0)
    assert len(spans_5) == 5
    assert [s["id"] for s in spans_5] == [f"span-{i}" for i in range(5)]

    # Test offset
    spans_next_3 = store.get_spans(cid, limit=3, offset=5)
    assert len(spans_next_3) == 3
    assert [s["id"] for s in spans_next_3] == ["span-5", "span-6", "span-7"]


def test_tracer_safe_recording_on_store_failure(tmp_path):
    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-trace-fail", route="investigating", now=time.time())

    # Mock record_span to raise an unexpected error
    store.record_span = MagicMock(side_effect=RuntimeError("disk full"))

    tracer = Tracer(store, cid)
    # The span context manager must not raise when record_span fails
    with tracer.span("tool", "get_finding", attributes={"finding_id": "f1"}):
        val = 42

    assert val == 42


def test_action_dispatch_records_trace_spans(tmp_path):
    from types import SimpleNamespace
    from helio_security.actions import dispatch

    store = CaseStore(tmp_path / "security.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-act-trace", route="investigating", now=time.time())

    class FakePlaybook:
        async def read(self, *args, **kwargs):
            return {"principal_status": "active", "access_version": 1}
        async def run(self, *args, **kwargs):
            return {"status": "executed"}
        async def verify(self, *args, **kwargs):
            return True

    deps = SimpleNamespace(
        store=store,
        clock=time.time,
        playbooks={"crm.pause_principal": FakePlaybook()},
        crm_url="http://localhost:1",
        action_token="tok",
        observer_token="obs",
    )

    res = asyncio.run(dispatch(deps, cid, "crm.pause_principal", "agent:sales-research"))
    assert res["status"] == "executed"

    spans = store.get_spans(cid)
    span_names = [s["name"] for s in spans]
    assert "crm.pause_principal.read" in span_names
    assert "crm.pause_principal.run" in span_names


