"""Read-only MCP server exposing bounded, redacted security observation tools."""
import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import httpx

try:
    from .redaction import scrub_text
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from helio_security.redaction import scrub_text

read_server = FastMCP("awasi-read")

# Allowed identifier characters: alphanumeric, hyphen, underscore, dot, colon (up to 2048 chars for base64 graph keys)
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.:]{1,2048}$")

MAX_RESPONSE_BYTES = 65536  # 64 KB ceiling

SENSITIVE_KEYS = {
    "token",
    "secret",
    "authorization",
    "password",
    "api_key",
    "key",
    "ssn",
    "customer_ssn",
    "credential",
    "customer_email",
    "customer_name",
    "access_token",
    "bot_token",
    "app_token",
    "secret_token",
}


def validate_id(identifier: str) -> str:
    """Validate that an identifier contains only allowed characters and is bounded."""
    if not isinstance(identifier, str) or not ID_PATTERN.match(identifier.strip()):
        raise ValueError(f"Invalid identifier: {identifier!r}")
    return identifier.strip()


def validate_limit(limit: int) -> int:
    """Validate limit is within [1, 20]."""
    if not isinstance(limit, int) or limit < 1 or limit > 20:
        raise ValueError(f"Invalid limit: {limit!r}. Must be between 1 and 20.")
    return limit


def _scrub_string(text: str) -> str:
    text = scrub_text(text)
    if len(text) > 1000:
        text = text[:997] + "..."
    return text


def redact_evidence(data: Any, max_items: int = 20) -> Any:
    """Recursively scrub sensitive credential and customer PII fields, and enforce bounds."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            lower_k = str(k).lower()
            if any(s in lower_k for s in SENSITIVE_KEYS):
                continue
            if lower_k == "email" or "email" in lower_k:
                continue
            if (k == "id" or k.endswith("_id")) and isinstance(v, str):
                cleaned[k] = scrub_text(v)[:2048]
            else:
                cleaned[k] = redact_evidence(v, max_items=max_items)
        return cleaned
    elif isinstance(data, (list, tuple)):
        return [redact_evidence(item, max_items=max_items) for item in list(data)[:max_items]]
    elif isinstance(data, str):
        return _scrub_string(data)
    return data


# These tools expose observation metadata, never customer records or free-form
# source notes. Text pattern scrubbing is an additional safeguard, not a schema.
EVIDENCE_FIELDS = frozenset("""
    id company_id finding_id agent_id grant_id resource_id employee_id entity_id
    actor_id owner_id owner_employee_id principal_id source_id target_id from_id to_id
    name employee_name kind actor_kind rule severity status assessment evidence_fresh
    principal_status employee_status grant_status grant_version version access_version
    classification data_classification record_count result_record_count scopes
    evidence events sources directory crm coverage_complete observed_at last_observed_at
    directory_observed_at crm_observed_at last_verified first_seen last_seen
    last_success last_attempt fresh present reason reason_code action outcome status_code
    request_id event_id event_type occurred_at received_at created_at updated_at
    items nodes edges start end properties source total limit offset truncated found
    unavailable error event_count lifecycle stream stages route duration_ms response_bytes
    prepared_record_count requested_limit response_check scan_status enabled
    completion release_decision serialized_bytes emitted_bytes snapshot next_before
""".split())


def project_evidence(data):
    truncated = False
    def project(value, depth=0):
        nonlocal truncated
        if depth > 8:
            truncated = True
            return None
        if isinstance(value, dict):
            return {key: project(item, depth + 1) for key, item in value.items()
                    if key in EVIDENCE_FIELDS}
        if isinstance(value, list):
            truncated |= len(value) > 20
            return [project(item, depth + 1) for item in value[:20]]
        if isinstance(value, str) and len(value) > 2048:
            truncated = True
            return _scrub_string(value)
        return value
    result = redact_evidence(project(data))
    if truncated:
        result["truncated"] = True
    return result


def _get_api_config():
    api_url = os.getenv("SECURITY_API_URL", "http://127.0.0.1:8002").rstrip("/")
    token = os.getenv("SECURITY_VIEWER_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return api_url, headers


async def _fetch_bounded(client: httpx.AsyncClient, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Fetch response with an explicit byte ceiling before fully buffering/parsing."""
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code == 404:
            return {"found": False, "error": "not_found"}
        if resp.status_code != 200:
            return {"unavailable": True, "error": f"http_{resp.status_code}"}
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                return {"unavailable": True, "error": "response_too_large"}
            body.extend(chunk)
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, RecursionError):
            return {"unavailable": True, "error": "invalid_json"}


@read_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_finding(finding_id: str) -> Dict[str, Any]:
    """Retrieve details and evidence for a specific finding by ID (read-only)."""
    vid = validate_id(finding_id)
    api_url, headers = _get_api_config()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            raw_data = await _fetch_bounded(client, f"{api_url}/findings/{quote(vid, safe=':')}", headers)
            if raw_data.get("error"):
                return raw_data
            return project_evidence(raw_data)
    except Exception as e:
        return {"unavailable": True, "error": e.__class__.__name__}


@read_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_entity(entity_id: str) -> Dict[str, Any]:
    """Retrieve an entity node from the company graph by ID (read-only)."""
    vid = validate_id(entity_id)
    api_url, headers = _get_api_config()
    company_id = os.getenv("HELIO_COMPANY_ID", "helio")

    candidate_ids = [vid]
    if ":" in vid or vid.startswith("emp-") or vid.startswith("grant-") or vid.startswith("resource-"):
        try:
            from helio_security.reader import entity_id as encode_entity_id
            if vid.startswith("agent:"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "Agent", vid], separators=(",", ":"))))
            elif vid.startswith("emp-"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "Employee", vid], separators=(",", ":"))))
            elif vid.startswith("grant-"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "AccessGrant", vid], separators=(",", ":"))))
            elif vid.startswith("resource-"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "DataResource", vid], separators=(",", ":"))))
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for cid in candidate_ids:
                data = await _fetch_bounded(client, f"{api_url}/entities/{quote(cid, safe=':')}", headers)
                if data.get("error") != "not_found":
                    return project_evidence(data)
            return {"found": False, "entity_id": vid, "error": "not_found"}
    except Exception as e:
        return {"unavailable": True, "error": e.__class__.__name__}


@read_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_neighbors(entity_id: str) -> Dict[str, Any]:
    """Retrieve graph relationships and neighbors for an entity (read-only)."""
    vid = validate_id(entity_id)
    api_url, headers = _get_api_config()
    company_id = os.getenv("HELIO_COMPANY_ID", "helio")

    candidate_ids = [vid]
    if ":" in vid or vid.startswith("emp-") or vid.startswith("grant-") or vid.startswith("resource-"):
        try:
            from helio_security.reader import entity_id as encode_entity_id
            if vid.startswith("agent:"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "Agent", vid], separators=(",", ":"))))
            elif vid.startswith("emp-"):
                candidate_ids.append(encode_entity_id(json.dumps([company_id, "Employee", vid], separators=(",", ":"))))
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for cid in candidate_ids:
                data = await _fetch_bounded(client, f"{api_url}/entities/{quote(cid, safe=':')}/relationships", headers)
                if data.get("error") != "not_found":
                    return project_evidence(data)
            return {"found": False, "entity_id": vid, "error": "not_found"}
    except Exception as e:
        return {"unavailable": True, "error": e.__class__.__name__}


@read_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_crm_activity(actor_id: str, limit: int = 10) -> Dict[str, Any]:
    """Retrieve recent CRM activity records for a principal (read-only)."""
    vid = validate_id(actor_id)
    vlim = validate_limit(limit)
    api_url, headers = _get_api_config()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            data = await _fetch_bounded(client, f"{api_url}/activity?actor_id={quote(vid, safe=':')}&limit={vlim}", headers)
            if data.get("error"):
                return data
            return project_evidence(data)
    except Exception as e:
        return {"unavailable": True, "error": e.__class__.__name__}


if __name__ == "__main__":
    import asyncio
    asyncio.run(read_server.run_stdio_async())
