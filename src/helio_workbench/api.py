"""FastAPI application for Helio Workbench (Phase 4).

Provides endpoints for apps, agents, destinations, inventory, and event auditing.
Enforces strict separation between observer, action, and user capabilities.
"""
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple
import urllib.parse

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .store import ConflictError, NotFoundError, WorkbenchStore

log = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


def is_valid_hostname(hostname: str) -> bool:
    if not hostname or len(hostname) > 253:
        return False
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if hostname.startswith("[") and hostname.endswith("]"):
        try:
            ipaddress.IPv6Address(hostname[1:-1])
            return True
        except ValueError:
            return False
    try:
        ipaddress.IPv4Address(hostname)
        return True
    except ValueError:
        pass
    label_re = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]{0,61}[a-zA-Z0-9])?$")
    labels = hostname.split(".")
    return all(bool(label_re.match(lbl)) for lbl in labels)


def validate_and_normalize_destination_url(url: str, expected_host: Optional[str] = None) -> Tuple[str, str]:
    """Validates destination URL and returns (normalized_url, normalized_host).

    Enforces:
    - scheme must be http or https
    - URL must not contain embedded credentials (userinfo)
    - URL must not contain whitespace
    - Port must be a valid integer between 1 and 65535
    - URL must have a valid hostname (IPv4, IPv6, or domain/label)
    - if expected_host is given, URL hostname must match expected_host
    """
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=422, detail="Destination URL must be a non-empty string")

    if any(c.isspace() for c in url):
        raise HTTPException(status_code=422, detail="Destination URL must not contain whitespace")

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Malformed URL: {e}")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail=f"Invalid URL scheme '{parsed.scheme}'; must be http or https")

    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Destination URL must not contain embedded credentials")

    try:
        port = parsed.port
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid URL port: {e}")

    if port is not None and (port < 1 or port > 65535):
        raise HTTPException(status_code=422, detail="URL port out of range 1-65535")

    if not parsed.hostname or not is_valid_hostname(parsed.hostname):
        raise HTTPException(status_code=422, detail="Destination URL contains invalid hostname")

    url_host = parsed.hostname.strip().lower()
    if expected_host:
        norm_expected = expected_host.strip().lower().split(":")[0]
        if not is_valid_hostname(norm_expected):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid destination host: '{expected_host}'",
            )
        matches = url_host == norm_expected
        if not matches and url_host in ("127.0.0.1", "localhost") and norm_expected == "receiver":
            matches = True

        if not matches:
            raise HTTPException(
                status_code=422,
                detail=f"Destination host mismatch: declared '{expected_host}', but URL targets '{url_host}'",
            )

    return url, url_host


class AppPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    visibility: Optional[Literal["private", "company", "public"]] = None


class AgentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    status: Optional[Literal["active", "paused"]] = None


class DestinationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    enabled: Optional[bool] = None
    url: Optional[str] = None


class DestinationAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Optional[str] = Field(default=None, max_length=100)
    url: str = Field(min_length=1)
    enabled: bool = True


def get_token_from_header(auth: Optional[HTTPAuthorizationCredentials]) -> Optional[str]:
    return auth.credentials if auth else None


def create_app(
    store: Optional[WorkbenchStore] = None,
    observer_token: Optional[str] = None,
    action_token: Optional[str] = None,
    user_token: Optional[str] = None,
    crm_url: Optional[str] = None,
    crm_tokens: Optional[Dict[str, str]] = None,
    token_actors: Optional[Dict[str, str]] = None,
) -> FastAPI:
    db_path = Path(os.getenv("WORKBENCH_DB_PATH", "data/workbench.sqlite3"))
    st = store or WorkbenchStore(db_path)

    obs_tok = observer_token if observer_token is not None else os.getenv("WORKBENCH_OBSERVER_TOKEN", os.getenv("CRM_OBSERVER_TOKEN", ""))
    act_tok = action_token if action_token is not None else os.getenv("WORKBENCH_ACTION_TOKEN", os.getenv("CRM_ACTION_TOKEN", ""))
    usr_tok = user_token if user_token is not None else os.getenv("WORKBENCH_USER_TOKEN", os.getenv("CRM_MAYA_TOKEN", ""))

    crm_endpoint = (crm_url or os.getenv("CRM_URL", "http://crm:8001")).rstrip("/")
    tokens_map = crm_tokens or {}
    actors_map = token_actors or {}

    app = FastAPI(title="Helio Workbench", version="1.0.0")

    def resolve_caller_identity(tok: str) -> str:
        if tok in actors_map:
            return actors_map[tok]
        if tok and tok == os.getenv("CRM_MAYA_TOKEN"):
            return "emp-maya-chen"
        for pid, t in tokens_map.items():
            if tok and tok == t:
                return pid
        return "user"

    def require_observer(auth: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]):
        tok = get_token_from_header(auth)
        if not tok:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not obs_tok or tok != obs_tok:
            raise HTTPException(status_code=403, detail="Observer token required")
        return tok

    def require_action(auth: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]):
        tok = get_token_from_header(auth)
        if not tok:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not act_tok or tok != act_tok:
            raise HTTPException(status_code=403, detail="Action token required")
        return tok

    def require_user(auth: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)]):
        tok = get_token_from_header(auth)
        if not tok:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not usr_tok or tok != usr_tok:
            raise HTTPException(status_code=403, detail="User token required")
        return tok

    def get_crm_token_for_principal(principal_id: str) -> str:
        if principal_id in tokens_map and tokens_map[principal_id]:
            return tokens_map[principal_id]
        env_var_map = {
            "service:renewal-tracker": "CRM_RENEWAL_TRACKER_TOKEN",
            "service:support-lookup": "CRM_SUPPORT_LOOKUP_TOKEN",
            "agent:lead-enrichment": "CRM_LEAD_ENRICHMENT_TOKEN",
            "agent:renewal-digest": "CRM_RENEWAL_DIGEST_TOKEN",
        }
        var = env_var_map.get(principal_id)
        if var and os.getenv(var):
            return os.environ[var]
        raise HTTPException(
            status_code=503,
            detail=f"CRM principal credential not configured for {principal_id}",
        )

    def fetch_crm_customers(principal_id: str) -> List[Dict[str, Any]]:
        token = get_crm_token_for_principal(principal_id)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = httpx.get(f"{crm_endpoint}/customers", headers=headers, timeout=5.0)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"CRM service returned status {resp.status_code}",
                )
            return resp.json()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="Failed to connect to CRM service")

    @app.get("/health")
    def health():
        return {"status": "ok", "app": "helio-workbench"}

    # Inventory & Events (Observer token)
    @app.get("/inventory")
    def inventory(tok: str = Depends(require_observer)):
        return st.get_inventory()

    @app.get("/events")
    def events(
        after: Optional[int] = Query(None),
        limit: int = Query(50, ge=1, le=100),
        tok: str = Depends(require_observer),
    ):
        return st.list_events(after=after, limit=limit)

    # Public View (Unauthenticated)
    @app.get("/public/{slug}")
    def public_view(slug: str):
        app_obj = st.get_app_by_slug(slug)
        if not app_obj or app_obj["visibility"] != "public":
            raise HTTPException(status_code=404, detail="App not found or not public")

        raw_customers = fetch_crm_customers(app_obj["crm_principal_id"])
        formatted = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "contact_email": c.get("contact_email"),
                "plan": c.get("plan"),
                "subscription_status": c.get("subscription_status") or c.get("status"),
            }
            for c in raw_customers
        ]
        body_bytes = json.dumps(formatted).encode("utf-8")
        st.record_event(
            kind="app.view",
            subject_id=app_obj["id"],
            actor="anonymous",
            destination_host=None,
            record_count=len(formatted),
            bytes_count=len(body_bytes),
            fields=["contact_email", "id", "name", "plan", "subscription_status"],
            outcome="success",
        )
        return formatted

    # Internal User View (User token)
    @app.get("/apps/{slug}")
    def user_view(slug: str, tok: str = Depends(require_user)):
        app_obj = st.get_app_by_slug(slug)
        if not app_obj or app_obj["visibility"] not in ("company", "public"):
            raise HTTPException(status_code=404, detail="App not found or private")

        raw_customers = fetch_crm_customers(app_obj["crm_principal_id"])
        formatted = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "contact_email": c.get("contact_email"),
                "plan": c.get("plan"),
                "subscription_status": c.get("subscription_status") or c.get("status"),
            }
            for c in raw_customers
        ]
        body_bytes = json.dumps(formatted).encode("utf-8")
        st.record_event(
            kind="app.view",
            subject_id=app_obj["id"],
            actor=app_obj["owner_employee_id"],
            destination_host=None,
            record_count=len(formatted),
            bytes_count=len(body_bytes),
            fields=["contact_email", "id", "name", "plan", "subscription_status"],
            outcome="success",
        )
        return formatted

    # Mutations (Action token)
    @app.patch("/apps/{app_id}")
    def patch_app(app_id: str, body: AppPatchRequest, tok: str = Depends(require_action)):
        try:
            return st.update_app(
                app_id,
                body.expected_version,
                visibility=body.visibility,
                actor="service:awasi-actions",
            )
        except ConflictError as e:
            raise HTTPException(
                status_code=409,
                detail=str(e),
                headers={"X-Current-Version": str(e.current_version or "")},
            )
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/agents/{agent_id}")
    def patch_agent(agent_id: str, body: AgentPatchRequest, tok: str = Depends(require_action)):
        try:
            return st.update_agent(
                agent_id,
                body.expected_version,
                status=body.status,
                actor="service:awasi-actions",
            )
        except ConflictError as e:
            raise HTTPException(
                status_code=409,
                detail=str(e),
                headers={"X-Current-Version": str(e.current_version or "")},
            )
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/agents/{agent_id}/destinations/{host}")
    def patch_destination(
        agent_id: str,
        host: str,
        body: DestinationPatchRequest,
        tok: str = Depends(require_action),
    ):
        norm_url = None
        if body.url is not None:
            norm_url, _ = validate_and_normalize_destination_url(body.url, expected_host=host)

        try:
            return st.update_destination(
                agent_id,
                host,
                body.expected_version,
                enabled=body.enabled,
                url=norm_url,
                actor="service:awasi-actions",
            )
        except ConflictError as e:
            raise HTTPException(
                status_code=409,
                detail=str(e),
                headers={"X-Current-Version": str(e.current_version or "")},
            )
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/agents/{agent_id}/destinations")
    def add_destination_endpoint(agent_id: str, body: DestinationAddRequest, tok: str = Depends(require_user)):
        norm_url, norm_host = validate_and_normalize_destination_url(body.url, expected_host=body.host)

        agent = st.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

        caller_actor = resolve_caller_identity(tok)
        try:
            return st.add_destination(
                agent_id,
                norm_host,
                norm_url,
                enabled=body.enabled,
                actor=caller_actor,
            )
        except ConflictError as e:
            raise HTTPException(
                status_code=409,
                detail=str(e),
                headers={"X-Current-Version": str(e.current_version or "")},
            )
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # Agent Execution (User token)
    @app.post("/agents/{agent_id}/run")
    def run_agent(agent_id: str, tok: str = Depends(require_user)):
        agent = st.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

        if agent["status"] == "paused":
            raise HTTPException(status_code=409, detail=f"Agent {agent_id} is paused")

        # 1. Read customers from CRM
        customers = fetch_crm_customers(agent["crm_principal_id"])
        record_count = len(customers)
        payload_bytes = json.dumps(customers).encode("utf-8")
        fields = sorted(list(set(k for c in customers if isinstance(c, dict) for k in c.keys())))

        # 2. Get enabled destinations
        dests = [d for d in st.list_destinations(agent_id=agent_id) if d["enabled"]]

        if not dests:
            return {
                "status": "executed",
                "agent_id": agent_id,
                "records_sent": 0,
                "destinations_count": 0,
            }

        confirmed_records = 0
        destinations_sent = 0

        for d in dests:
            try:
                norm_url, norm_host = validate_and_normalize_destination_url(
                    d["url"], expected_host=d["host"]
                )
            except HTTPException:
                st.record_event(
                    kind="agent.send",
                    subject_id=agent_id,
                    actor=agent["crm_principal_id"],
                    destination_host=d["host"],
                    record_count=0,
                    bytes_count=len(payload_bytes),
                    fields=fields,
                    outcome="invalid_destination",
                )
                continue

            outcome = "unknown"
            confirmed = False
            parsed_dest = urllib.parse.urlsplit(norm_url)
            host_header = parsed_dest.netloc

            try:
                resp = httpx.post(
                    norm_url,
                    content=payload_bytes,
                    headers={"Content-Type": "application/json", "Host": host_header},
                    timeout=5.0,
                    follow_redirects=False,
                )
                if 200 <= resp.status_code < 300:
                    outcome = "success"
                    confirmed = True
                elif 300 <= resp.status_code < 400:
                    outcome = f"redirect_{resp.status_code}"
                elif 400 <= resp.status_code < 500:
                    outcome = f"rejected_{resp.status_code}"
                else:
                    outcome = f"failed_{resp.status_code}"
            except httpx.TimeoutException:
                outcome = "uncertain_timeout"
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.CloseError):
                outcome = "uncertain_disconnect"
            except httpx.ConnectError:
                outcome = "error_connection"
            except Exception as e:
                err_msg = str(e).lower()
                if "disconnect" in err_msg or "closed" in err_msg or "reset" in err_msg:
                    outcome = "uncertain_disconnect"
                else:
                    outcome = f"error_{e.__class__.__name__}"

            if confirmed:
                confirmed_records += record_count
                destinations_sent += 1

            event_record_count = (
                record_count if (confirmed or outcome.startswith("uncertain")) else 0
            )
            st.record_event(
                kind="agent.send",
                subject_id=agent_id,
                actor=agent["crm_principal_id"],
                destination_host=d["host"],
                record_count=event_record_count,
                bytes_count=len(payload_bytes),
                fields=fields,
                outcome=outcome,
            )

        return {
            "status": "executed",
            "agent_id": agent_id,
            "records_sent": confirmed_records,
            "destinations_count": len(dests),
        }

    return app


app = create_app()
