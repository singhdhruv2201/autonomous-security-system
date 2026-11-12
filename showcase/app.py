"""Local-only web entry point for the Awasi product showcase."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import secrets
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from helio_security.slack_app import SlackActions

from .services import LoopbackServices
from .session import COMPANY, ModelConfig, SCENARIOS, Session
from .slack import has_actions, listen_for_actions, notifier_factory

STATIC = Path(__file__).parent / "static"
COOKIE = "awasi_showcase"
log = logging.getLogger(__name__)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    choice: str = Field(min_length=1, max_length=50)


def same_origin(request: Request):
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("x-awasi-showcase") != "1" or (origin and origin != expected):
        raise HTTPException(403, "A same-origin showcase request is required.")


def create_app(model=None, *, slack=None, slack_client=None, slack_socket=None):
    sessions = {}
    make_notifier = notifier_factory(slack, slack_client, model_name=model.name if model else None)

    @asynccontextmanager
    async def lifespan(app):
        async with LoopbackServices() as gateway:
            app.state.gateway = gateway
            try:
                async with listen_for_actions(slack, slack_action, slack_socket):
                    yield
            finally:
                for session in sessions.values():
                    session.close()
                sessions.clear()

    app = FastAPI(title="Awasi Showcase", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        return response

    def current(request: Request):
        session = sessions.get(request.cookies.get(COOKIE))
        if not session:
            raise HTTPException(404, "Start a baseline environment first.")
        return session

    def ready(session):
        if session.busy:
            raise HTTPException(409, "An operation is already running in this environment.")

    async def step(session, worker, label, detail):
        session.busy = True
        try:
            if await worker() is not False:
                await asyncio.to_thread(session.observe, label, detail)
        except Exception:
            log.error("Showcase operation failed")
            session.log("error", "The operation did not finish. Review recorded evidence or start a fresh baseline.")
            raise HTTPException(503, "The operation did not finish. Review the timeline or start a fresh baseline.") from None
        finally:
            session.busy = False
            session.published = session.view()
        return session.published

    async def slack_action(body, case_id, choice):
        # Messages from the regular worker or a reset baseline have no local case.
        target = next(((key, session) for key, session in sessions.items()
                       if session.cases.get_case(case_id)), None)
        if not target:
            return
        key, session = target
        while session.busy:
            await session.idle.wait()
        if sessions.get(key) is not session:
            return

        async def apply():
            received = False

            def wake():
                nonlocal received
                received = True

            actions = SlackActions(session.cases, session.notifier, slack, wake=wake)
            await actions.handle(lambda: None, body)  # Socket Mode already acknowledged the envelope.
            if received:
                await session.run_worker()
            return received

        try:
            await step(session, apply, "Decision", choice)
        except HTTPException:
            log.warning("Showcase could not finish the Slack decision")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config():
        from dataclasses import asdict
        from helio_security.policies import policy

        return {
            "scenarios": {key: asdict(scenario) for key, scenario in SCENARIOS.items()},
            "policies": {scenario.rule: policy(scenario.rule).view() for scenario in SCENARIOS.values()},
            "model": model.name if model else None,
            "slack_enabled": make_notifier is not None,
            "slack_interactive": has_actions(slack),
        }

    @app.get("/api/session")
    async def get_session(session: Session = Depends(current)):
        return {**session.published, "busy": session.busy}

    @app.post("/api/session", status_code=201, dependencies=[Depends(same_origin)])
    async def start(request: Request, response: Response):
        old_key = request.cookies.get(COOKIE)
        old = sessions.get(old_key)
        if old:
            ready(old)
            old.busy = True

        key = secrets.token_urlsafe(32)
        session = None
        try:
            session = Session(request.app.state.gateway, model, make_notifier=make_notifier)
            sessions[key] = session
            await asyncio.to_thread(session.initialize)
            if not all(session.projection.sources.get(source, {}).get("status") == "ok"
                       for source in ("directory", "crm", "workbench")):
                raise RuntimeError("Incomplete baseline")
            session.published = session.view()
        except BaseException as error:
            if session:
                sessions.pop(key, None)
                session.close()
            if old:
                old.busy = False
            if isinstance(error, asyncio.CancelledError):
                raise
            log.error("Showcase baseline could not be initialized")
            raise HTTPException(503, "The baseline environment could not start.") from None

        if old:
            sessions.pop(old_key)
            old.busy = False
            old.close()
        response.set_cookie(COOKIE, key, httponly=True, samesite="strict")
        return session.published

    @app.post("/api/scenarios/{scenario_id}", dependencies=[Depends(same_origin)])
    async def trigger(scenario_id: str, session: Session = Depends(current)):
        scenario = SCENARIOS.get(scenario_id)
        if not scenario:
            raise HTTPException(404, "Unknown scenario.")
        ready(session)
        if any(SCENARIOS[item].group == scenario.group for item in session.started):
            raise HTTPException(409, "Start a fresh baseline to replay this scenario.")
        session.started.add(scenario_id)

        async def run():
            await asyncio.to_thread(session.mutate, scenario_id)
            await asyncio.to_thread(session.observe, "Triggered", scenario.title)
            session.published = session.view()
            await session.run_worker()

        return await step(session, run, "Workflow", scenario.title)

    @app.post("/api/cases/{case_id}/decision", dependencies=[Depends(same_origin)])
    async def decide(case_id: str, decision: Decision, session: Session = Depends(current)):
        ready(session)
        if not session.cases.get_case(case_id):
            raise HTTPException(404, "Case not found in this environment.")
        try:
            session.cases.submit_decision(
                COMPANY, case_id, key=decision.key, choice=decision.choice,
                employee_id="operator", source="showcase", now=time.time(),
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return await step(session, session.run_worker, "Decision", decision.choice)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3070)
    parser.add_argument("--no-slack", action="store_true", help="Keep showcase notifications in the browser.")
    model_options = parser.add_mutually_exclusive_group()
    model_options.add_argument("--live-model", action="store_true", help="Require live Anthropic configuration.")
    model_options.add_argument("--recorded-model", action="store_true", help="Use recorded Anthropic assessments.")
    args = parser.parse_args()
    # Repo clients use the standard proxy environment. Keep private service requests local.
    no_proxy = ",".join(filter(None, [os.getenv("no_proxy") or os.getenv("NO_PROXY"), "127.0.0.1", "localhost"]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = no_proxy

    from dotenv import dotenv_values
    from helio_security.slack_app import SlackConfig

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env", interpolate=False)
    settings = {**values, **os.environ}
    slack = None if args.no_slack else SlackConfig.from_environment(settings)

    key = os.getenv("ANTHROPIC_API_KEY") or values.get("ANTHROPIC_API_KEY")
    if args.live_model and not key:
        parser.error("--live-model requires ANTHROPIC_API_KEY")
    model = None
    if args.live_model or (not args.recorded_model and key and slack and slack.bot_token and slack.channel):
        from helio_security.live_ledger import LiveLedger

        name = os.getenv("AWASI_MODEL") or values.get("AWASI_MODEL") or "claude-haiku-5-5"
        ledger = LiveLedger(Path(__file__).parent / "data" / "anthropic-ledger.json",
                            max_spend_usd=.50, max_requests=40)
        model = ModelConfig(name, key, ledger)

    uvicorn.run(create_app(model, slack=slack), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
