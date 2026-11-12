"""Local trace span recording and LangChain callback handler.

Spans record operation metadata, character sizes, and token usage.
Raw prompts, completions, customer values, tokens, and authorization headers
are strictly excluded from trace storage.
"""
from contextlib import contextmanager
import json
import time
from typing import Any, Dict, List, Optional
import uuid

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .cases import CaseStore
from .redaction import safe_identifier

ALLOWLISTED_ATTRIBUTE_KEYS = {
    "finding_id",
    "entity_id",
    "actor_id",
    "target",
    "playbook",
    "limit",
    "model",
    "model_name",
    "status",
}


def filter_attributes(attributes: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Retain only safe, allowlisted identifier/configuration keys."""
    if not isinstance(attributes, dict):
        return {}
    filtered = {}
    for k, v in attributes.items():
        if k in ALLOWLISTED_ATTRIBUTE_KEYS:
            if isinstance(v, str) and safe_identifier(v, limit=2048):
                filtered[k] = v
            elif isinstance(v, (int, float, bool)):
                filtered[k] = v
    return filtered


def sanitize_error(err: Any) -> str:
    """Return a sanitized error category without raw error strings or secrets."""
    if err is None:
        return ""
    err_cls = err.__class__.__name__
    err_str = str(err).lower()
    if "timeout" in err_cls.lower() or "timeout" in err_str:
        return "timeout"
    if "rate" in err_str or "429" in err_str:
        return "rate_limit"
    if "connect" in err_str or "network" in err_str:
        return "connection_error"
    if "validation" in err_cls.lower():
        return "validation_error"
    # Fallback to normalized class name (no message text)
    if err_cls == "RuntimeError":
        return "runtime_error"
    return err_cls


class Tracer:
    """Context manager for explicit span recording (workflow, actions, manual ops)."""

    def __init__(self, store: CaseStore, case_id: str):
        self.store = store
        self.case_id = case_id
        self._span_stack: List[str] = []

    @contextmanager
    def span(
        self,
        kind: str,
        name: str,
        parent_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        span_id = str(uuid.uuid4())
        pid = parent_id or (self._span_stack[-1] if self._span_stack else None)
        self._span_stack.append(span_id)
        started_at = time.time()
        err_cat = None
        try:
            yield span_id
        except Exception as e:
            err_cat = sanitize_error(e)
            raise
        finally:
            ended_at = time.time()
            if self._span_stack and self._span_stack[-1] == span_id:
                self._span_stack.pop()
            try:
                self.store.record_span({
                    "id": span_id,
                    "case_id": self.case_id,
                    "parent_id": pid,
                    "kind": kind,
                    "name": name,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "input_chars": 0,
                    "output_chars": 0,
                    "tokens_in": None,
                    "tokens_out": None,
                    "error": err_cat,
                    "attributes": filter_attributes(attributes),
                })
            except Exception:
                pass


class CaseTracer(BaseCallbackHandler):
    """LangChain callback handler recording model and tool spans into CaseStore."""

    def __init__(self, store: CaseStore, case_id: str):
        super().__init__()
        self.store = store
        self.case_id = case_id
        self._runs: Dict[str, Dict[str, Any]] = {}

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        model_name = (
            kwargs.get("invocation_params", {}).get("model_name")
            or kwargs.get("invocation_params", {}).get("model")
            or (metadata or {}).get("model")
            or serialized.get("name", "chat_model")
        )
        input_chars = 0
        for batch in messages:
            for m in batch:
                content = getattr(m, "content", "")
                if isinstance(content, str):
                    input_chars += len(content)
                elif isinstance(content, list):
                    input_chars += len(str(content))

        self._runs[str(run_id)] = {
            "kind": "model",
            "name": str(model_name),
            "parent_id": str(parent_run_id) if parent_run_id else None,
            "started_at": time.time(),
            "input_chars": input_chars,
            "attributes": filter_attributes({"model": model_name}),
        }

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(str(run_id), None)
        if not run:
            return

        ended_at = time.time()
        output_chars = 0
        tokens_in = None
        tokens_out = None

        for batch in response.generations:
            for gen in batch:
                msg = getattr(gen, "message", None)
                if msg:
                    content = getattr(msg, "content", "")
                    output_chars += len(str(content))
                    usage = getattr(msg, "usage_metadata", None)
                    if isinstance(usage, dict):
                        tokens_in = usage.get("input_tokens")
                        tokens_out = usage.get("output_tokens")
                elif getattr(gen, "text", None):
                    output_chars += len(gen.text)

        # Fallback to response.llm_output
        if (tokens_in is None or tokens_out is None) and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage") or response.llm_output.get("usage")
            if isinstance(usage, dict):
                tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens")
                tokens_out = usage.get("completion_tokens") or usage.get("output_tokens")

        try:
            self.store.record_span({
                "id": str(run_id),
                "case_id": self.case_id,
                "parent_id": run["parent_id"],
                "kind": run["kind"],
                "name": run["name"],
                "started_at": run["started_at"],
                "ended_at": ended_at,
                "input_chars": run["input_chars"],
                "output_chars": output_chars,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": None,
                "attributes": run["attributes"],
            })
        except Exception:
            pass

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(str(run_id), None)
        if not run:
            return
        try:
            self.store.record_span({
                "id": str(run_id),
                "case_id": self.case_id,
                "parent_id": run["parent_id"],
                "kind": run["kind"],
                "name": run["name"],
                "started_at": run["started_at"],
                "ended_at": time.time(),
                "input_chars": run["input_chars"],
                "output_chars": 0,
                "tokens_in": None,
                "tokens_out": None,
                "error": sanitize_error(error),
                "attributes": run["attributes"],
            })
        except Exception:
            pass

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        tool_name = serialized.get("name", kwargs.get("name", "tool"))
        attrs = {}
        if isinstance(inputs, dict):
            attrs.update(filter_attributes(inputs))
        else:
            try:
                parsed = json.loads(input_str)
                if isinstance(parsed, dict):
                    attrs.update(filter_attributes(parsed))
            except Exception:
                pass

        self._runs[str(run_id)] = {
            "kind": "tool",
            "name": str(tool_name),
            "parent_id": str(parent_run_id) if parent_run_id else None,
            "started_at": time.time(),
            "input_chars": len(str(input_str)),
            "attributes": attrs,
        }

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(str(run_id), None)
        if not run:
            return
        output_str = str(output)
        try:
            self.store.record_span({
                "id": str(run_id),
                "case_id": self.case_id,
                "parent_id": run["parent_id"],
                "kind": run["kind"],
                "name": run["name"],
                "started_at": run["started_at"],
                "ended_at": time.time(),
                "input_chars": run["input_chars"],
                "output_chars": len(output_str),
                "tokens_in": None,
                "tokens_out": None,
                "error": None,
                "attributes": run["attributes"],
            })
        except Exception:
            pass

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID,
        parent_run_id: Optional[uuid.UUID] = None,
        **kwargs: Any,
    ) -> None:
        run = self._runs.pop(str(run_id), None)
        if not run:
            return
        try:
            self.store.record_span({
                "id": str(run_id),
                "case_id": self.case_id,
                "parent_id": run["parent_id"],
                "kind": run["kind"],
                "name": run["name"],
                "started_at": run["started_at"],
                "ended_at": time.time(),
                "input_chars": run["input_chars"],
                "output_chars": 0,
                "tokens_in": None,
                "tokens_out": None,
                "error": sanitize_error(error),
                "attributes": run["attributes"],
            })
        except Exception:
            pass
