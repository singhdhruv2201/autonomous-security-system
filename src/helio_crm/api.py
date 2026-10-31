"""A separate application boundary: its own data, authentication and grants."""

import json
import os
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .credentials import from_environment
from .store import CRMError, CRMStore

bearer = HTTPBearer(auto_error=False)
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


class CustomerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    notes: str | None = Field(default=None, max_length=500)
    subscription_status: Literal["active", "trial", "cancelled"] | None = None


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


def load_directory():
    base = os.environ.get("HELIO_DIRECTORY_URL", "http://127.0.0.1:8000")
    with urllib.request.urlopen(base.rstrip("/") + "/employees", timeout=5) as response:
        return json.load(response)


def create_app(db_path=None, tokens=None, employees_loader=None, *, activity_enabled=None):
    if activity_enabled is None:
        configured = os.getenv("CRM_ACTIVITY_ENABLED", "0")
        if configured not in {"0", "1"}:
            raise ValueError("CRM_ACTIVITY_ENABLED must be 0 or 1")
        activity_enabled = configured == "1"
    store = CRMStore(db_path or Path(os.getenv("CRM_DB_PATH", "data/crm.sqlite3")),
                     activity_enabled=activity_enabled)

    @asynccontextmanager
    async def lifespan(app):
        credentials = tokens if tokens is not None else from_environment()
        employees = [] if store.is_initialized() else (employees_loader or load_directory)()
        store.initialize(employees, credentials)
        from .webhook_worker import DeliveryWorker
        worker = DeliveryWorker.from_environment(store)
        if worker:
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.stop()

    app = FastAPI(title="Helio CRM", version="0.2.0", lifespan=lifespan,
                  description="Working local CRM with synthetic customers and enforced bearer-token grants.")
    from .lifecycle import ActivityMiddleware
    app.add_middleware(ActivityMiddleware, store=store)

    @app.exception_handler(CRMError)
    async def handle_error(request: Request, error: CRMError):
        headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail}, headers=headers)

    def token(credentials):
        return credentials.credentials if credentials else None

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["System"])
    def health():
        with store.connection() as db:
            db.execute("SELECT COUNT(*) FROM customers").fetchone()
        return {"status": "ok", "app": "helio-crm"}

    @app.get("/customers", tags=["Customers"])
    def customers(credentials: Credentials):
        return store.list_customers(token(credentials))

    @app.get("/customers/{customer_id}", tags=["Customers"])
    def customer(customer_id: str, credentials: Credentials):
        return store.get_customer(token(credentials), customer_id)

    @app.patch("/customers/{customer_id}", tags=["Customers"])
    def update_customer(customer_id: str, body: CustomerUpdate, credentials: Credentials):
        return store.update_customer(token(credentials), customer_id, body.expected_version,
                                     body.model_dump(exclude={"expected_version"}, exclude_unset=True))

    @app.get("/accounts", tags=["Administration"])
    def accounts(credentials: Credentials):
        return store.list_accounts(token(credentials))

    @app.get("/grants", tags=["Administration"])
    def grants(credentials: Credentials):
        return store.list_grants(token(credentials))

    @app.post("/grants/{grant_id}/revoke", tags=["Administration"])
    def revoke(grant_id: str, body: RevokeRequest, credentials: Credentials):
        return store.revoke_grant(token(credentials), grant_id, body.expected_version)

    @app.get("/inventory", tags=["Security metadata"])
    def inventory(credentials: Credentials):
        return store.inventory(token(credentials))

    @app.get("/audit", tags=["Administration"])
    def audit(credentials: Credentials, limit: int = Query(default=100, ge=1, le=500)):
        return store.audit(token(credentials), limit)

    @app.post('/activity-deliveries/{sequence}/replay', tags=['Administration'])
    def replay(sequence: Annotated[int, Field(ge=1, le=2**63-1)], body: dict, credentials: Credentials):
        if body or sequence < 1:
            raise CRMError(422, 'Invalid replay request')
        return store.replay_delivery(token(credentials), sequence)

    return app


app = create_app()
