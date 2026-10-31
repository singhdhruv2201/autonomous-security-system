"""Local UI gateway. Browser receives data, never service bearer credentials."""

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID
from urllib.parse import urlencode

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .gateway import Gateway, UpstreamError
from helio_security.activity_client import activity_query

ASSETS = Path(__file__).parent / "static"


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_app(gateway=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.gateway = gateway or Gateway.from_environment()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "dashboard", "testserver"])

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy":
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(UpstreamError)
    async def unavailable(request, error):
        return JSONResponse({"detail": "Security API unavailable"}, status_code=503)

    def same_origin(request: Request):
        if (request.headers.get("origin") != str(request.base_url).rstrip("/")
                or request.headers.get("x-awasi-request") != "dashboard"):
            raise HTTPException(403, "Use the dashboard to request a refresh")

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/state")
    def state(request: Request):
        result = request.app.state.gateway.state()
        return JSONResponse(result, status_code=200 if result["overview"] is not None else 503)

    @app.post("/api/sync-runs", dependencies=[Depends(same_origin)])
    def refresh(request: Request, body: EmptyRequest = Body(default=EmptyRequest())):
        status, payload = request.app.state.gateway.request("/sync-runs", method="POST")
        return JSONResponse(payload, status_code=status)

    @app.get("/api/sync-runs/{run_id}")
    def run(run_id: UUID, request: Request):
        return request.app.state.gateway.request("/sync-runs/" + str(run_id))[1]

    @app.get("/api/findings/{finding_id}")
    def finding(finding_id: UUID, request: Request):
        return request.app.state.gateway.request("/findings/" + str(finding_id))[1]

    @app.get('/api/activity')
    def activity(request: Request, query=Depends(activity_query)):
        return request.app.state.gateway.request('/activity?'+urlencode(query))[1]

    @app.get('/api/activity/health')
    def activity_health(request: Request):
        return request.app.state.gateway.request('/activity/health')[1]

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app


app = create_app()
