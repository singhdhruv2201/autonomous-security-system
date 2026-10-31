"""Authenticated observation API for the future dashboard and investigation agent."""

import hmac
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import BaseModel, ConfigDict

from .graph import GraphStore
from .platform_store import PlatformStore
from .reader import GraphReader
from .settings import Settings
from .worker import ObservationWorker
from .rules import RULE_ID
from .activity_client import ActivityClient, ActivityUnavailable, activity_query

bearer = HTTPBearer(auto_error=False)
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=100000)]
Kind = Literal["Employee", "Team", "App", "Agent", "AppAccount", "ServiceAccount", "DataResource", "AccessGrant"]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_app(settings=None, *, graph=None, store=None, reader=None, start_worker=True, activity_client=None):
    @asynccontextmanager
    async def lifespan(app):
        configured = settings or Settings.from_environment()
        owned_driver = None
        worker = None
        try:
            platform_graph = graph
            if platform_graph is None:
                owned_driver = GraphDatabase.driver(configured.graph_uri,
                    auth=(configured.graph_user, configured.graph_password), connection_timeout=5,
                    connection_acquisition_timeout=10, max_transaction_retry_time=10)
                platform_graph = GraphStore(owned_driver, configured.company_id)
            if platform_graph.company_id != configured.company_id:
                raise ValueError("Graph scope does not match service company")
            platform_graph.initialize()
            platform_store = store or PlatformStore(configured.db_path)
            platform_store.initialize()
            platform_reader = reader or GraphReader(platform_graph)
            if start_worker:
                worker = ObservationWorker(platform_store, platform_graph, platform_reader,
                    configured.directory_url, configured.crm_url, configured.observer_token,
                    interval=configured.sync_interval)
                worker.start()
            app.state.services = SimpleNamespace(settings=configured, graph=platform_graph,
                store=platform_store, reader=platform_reader, worker=worker)
            activity_url = os.getenv('AWASI_ACTIVITY_URL','')
            app.state.activity_client = activity_client or (ActivityClient(activity_url,configured.viewer_token) if activity_url else None)
            yield
        finally:
            if worker:
                worker.stop()
            if owned_driver:
                owned_driver.close()

    app = FastAPI(title="Helio Security Platform", version="0.3.0", lifespan=lifespan,
        description="Observation, inventory, connector health and persistent findings. Read-only toward company systems.")

    async def unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "Security data is temporarily unavailable"})

    for error in (Neo4jError, DriverError, sqlite3.Error, OSError, ActivityUnavailable):
        app.add_exception_handler(error, unavailable)

    def principal(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        configured = request.app.state.services.settings
        supplied = credentials.credentials.encode() if credentials else b""
        for role, token in (("operator", configured.operator_token), ("viewer", configured.viewer_token)):
            if hmac.compare_digest(supplied, token.encode()):
                return {"role": role, "company_id": configured.company_id}
        raise HTTPException(401, "Valid security API credential required", headers={"WWW-Authenticate": "Bearer"})

    def operator(identity=Depends(principal)):
        if identity["role"] != "operator":
            raise HTTPException(403, "Operator role required")
        return identity

    def services(request: Request):
        return request.app.state.services

    def found(value):
        if value is None:
            raise HTTPException(404, "Resource not found")
        return value

    def coverage(service):
        status = service.graph.status()
        return {key: status[key] for key in ("company_id", "sources", "coverage_complete")}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["System"])
    def health(service=Depends(services)):
        service.graph.driver.verify_connectivity()
        with service.store.connection() as db:
            db.execute("SELECT count(*) FROM runs").fetchone()
        if service.worker and not service.worker.alive:
            raise HTTPException(503, "Observation worker unavailable")
        return {"status": "ok"}

    @app.get("/overview", tags=["Observation"])
    def overview(identity=Depends(principal), service=Depends(services)):
        company = identity["company_id"]
        return {**service.graph.status(),
                "observation": {"interval_seconds": service.settings.sync_interval, "agentic": False,
                                "activity_ingestion": app.state.activity_client is not None, "remediation_enabled": False},
                "finding_counts": {state: service.store.findings(company, status=state, limit=1)["total"]
                                   for state in ("open", "resolved")},
                "latest_sync_run": next(iter(service.store.runs(company, limit=1)["items"]), None)}

    @app.get("/policies", tags=["Observation"])
    def policies(identity=Depends(principal), service=Depends(services)):
        return {"company_id": identity["company_id"], "items": [{"id": RULE_ID,
            "name": "Inactive owner with agent access", "enabled": True, "severity": "high",
            "description": "Detect an agent that retains customer access after its owner becomes inactive.",
            "sources": ["directory", "crm"], "mode": "Observe and flag",
            "conditions": ["Owner is inactive", "Agent has an active grant", "Grant permits customer read or write",
                           "Target resource is confidential"],
            "suggested_fix": "Review ownership and revoke the affected CRM grant if access is no longer required.",
            "coverage": "Requires fresh directory and CRM evidence. Missing evidence never resolves a finding."}]}

    @app.get('/activity', tags=['Activity'])
    def list_activity(identity=Depends(principal), query=Depends(activity_query)):
        client = app.state.activity_client
        if client is None:
            return {'company_id':identity['company_id'],'enabled':False,'items':[],'total':0,'next_before':None,'snapshot':0}
        return client.get('/activity',identity['company_id'],query)

    @app.get('/activity/health', tags=['Activity'])
    def activity_health(identity=Depends(principal)):
        client = app.state.activity_client
        if client is None:
            return {'company_id':identity['company_id'],'enabled':False,'sources':[],'coverage_complete':False,'worker_running':False}
        return client.get('/activity/health',identity['company_id'])

    @app.get("/connectors", tags=["Observation"])
    def connectors(identity=Depends(principal), service=Depends(services)):
        return coverage(service)

    @app.get("/entities", tags=["Inventory"])
    def entities(kind: Kind | None = None, limit: Limit = 50, offset: Offset = 0, present: bool = True,
                 identity=Depends(principal), service=Depends(services)):
        return {**coverage(service), **service.reader.entities(kind, limit, offset, present)}

    @app.get("/entities/{entity_id}", tags=["Inventory"])
    def entity(entity_id: str, identity=Depends(principal), service=Depends(services)):
        return found(service.reader.entity(entity_id))

    @app.get("/entities/{entity_id}/relationships", tags=["Inventory"])
    def relationships(entity_id: str, limit: Limit = 100, identity=Depends(principal), service=Depends(services)):
        return {**coverage(service), **found(service.reader.relationships(entity_id, limit))}

    @app.get("/graph", tags=["Inventory"])
    def company_graph(kind: Kind | None = None, limit: Limit = 100,
                      edge_limit: Annotated[int, Query(ge=1, le=500)] = 200,
                      identity=Depends(principal), service=Depends(services)):
        return {**coverage(service), **service.reader.graph(kind, limit, edge_limit)}

    @app.get("/findings", tags=["Findings"])
    def findings(status: Literal["open", "resolved"] | None = None, limit: Limit = 50, offset: Offset = 0,
                 identity=Depends(principal), service=Depends(services)):
        source_status = coverage(service)
        page = service.store.findings(identity["company_id"], status=status, limit=limit, offset=offset)
        if not source_status["coverage_complete"]:
            for item in page["items"]:
                item.update(assessment="unknown", evidence_fresh=False)
        return {**source_status, **page}

    @app.get("/findings/{finding_id}", tags=["Findings"])
    def finding(finding_id: str, identity=Depends(principal), service=Depends(services)):
        item = found(service.store.finding(identity["company_id"], finding_id))
        source_status = coverage(service)
        if not source_status["coverage_complete"]:
            item.update(assessment="unknown", evidence_fresh=False)
        return {**source_status, **item}

    @app.post("/sync-runs", status_code=202, tags=["Observation"])
    def request_sync(response: Response, body: SyncRequest = Body(default=SyncRequest()),
                     identity=Depends(operator), service=Depends(services)):
        run, created = service.store.enqueue(identity["company_id"], identity["role"], "manual", time.time())
        if not created:
            raise HTTPException(409, {"code": "sync_in_progress", "run_id": run["id"]})
        response.headers["Location"] = "/sync-runs/" + run["id"]
        return run

    @app.get("/sync-runs", tags=["Observation"])
    def runs(limit: Limit = 50, offset: Offset = 0, identity=Depends(principal), service=Depends(services)):
        return service.store.runs(identity["company_id"], limit, offset)

    @app.get("/sync-runs/{run_id}", tags=["Observation"])
    def run(run_id: str, identity=Depends(principal), service=Depends(services)):
        return found(service.store.run(identity["company_id"], run_id))

    @app.get("/audit", tags=["Administration"])
    def audit(limit: Limit = 50, offset: Offset = 0, identity=Depends(operator), service=Depends(services)):
        return service.store.audit(identity["company_id"], limit, offset)

    return app


app = create_app()
