"""Run the real Helio service handlers on a separate loopback listener."""
import asyncio
import os
import secrets
import socket
from threading import Thread

import httpx
from starlette.responses import Response
import uvicorn

from helio_crm.credentials import ENV_KEYS, WORKBENCH_ENV_KEYS
from helio_crm.policies import PolicyConfig
from helio_crm.store import CRMStore
from helio_simulator.directory import Directory
from helio_workbench.store import WorkbenchStore


def service_factories(root):
    # Workbench and Outside initialize their module-level default apps at import.
    # Point those constructors at this session too, before loading their factories.
    overrides = {
        "HELIO_DB_PATH": str(root / "directory.sqlite3"),
        "CRM_DB_PATH": str(root / "crm.sqlite3"),
        "WORKBENCH_DB_PATH": str(root / "workbench.sqlite3"),
        "OUTSIDE_DB_PATH": str(root / "outside.sqlite3"),
        "CRM_ACTIVITY_ENABLED": "0",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        from helio_crm.api import create_app as crm_app
        from helio_simulator.api import create_app as directory_app
        from helio_workbench.api import create_app as workbench_app
        from outside.api import create_app as outside_app
        return directory_app, crm_app, workbench_app, outside_app
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LoopbackServices:
    def __init__(self):
        self.routes = {}

    async def __aenter__(self):
        self.socket = socket.socket()
        try:
            self.socket.bind(("127.0.0.1", 0))
            self.socket.listen()
            self.base = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
            self.server = uvicorn.Server(uvicorn.Config(self, lifespan="off", access_log=False))
            self.thread = Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
            self.thread.start()
            for _ in range(200):
                if self.server.started:
                    return self
                if not self.thread.is_alive():
                    break
                await asyncio.sleep(.01)
            raise RuntimeError("Helio loopback services could not start")
        except BaseException:
            if hasattr(self, "thread"):
                await self.__aexit__(None, None, None)
            else:
                self.socket.close()
            raise

    async def __aexit__(self, *_):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join)
        self.socket.close()

    async def __call__(self, scope, receive, send):
        prefix, _, path = scope["path"][1:].partition("/")
        app = self.routes.get(prefix)
        if app and scope["type"] == "http":
            await app({**scope, "path": "/" + path, "root_path": ""}, receive, send)
        elif scope["type"] == "http":
            await Response(status_code=404)(scope, receive, send)
        else:
            await send({"type": "websocket.close"})


class HelioServices:
    def __init__(self, gateway, root):
        directory_app, crm_app, workbench_app, outside_app = service_factories(root)
        self.gateway = gateway
        self.prefixes = {name: secrets.token_urlsafe(24) for name in ("directory", "crm", "workbench", "outside")}
        self.urls = {name: f"{gateway.base}/{prefix}" for name, prefix in self.prefixes.items()}
        self.tokens = {key: secrets.token_urlsafe(32) for key in ENV_KEYS.keys() | WORKBENCH_ENV_KEYS.keys()}
        self.directory = Directory(root / "directory.sqlite3")
        self.directory.initialize()
        self.workbench = WorkbenchStore(root / "workbench.sqlite3")
        policy = PolicyConfig()
        crm_path = root / "crm.sqlite3"
        CRMStore(crm_path, policy_config=policy).initialize(self.directory.employees(), self.tokens)
        apps = {
            "directory": directory_app(self.directory.path),
            "crm": crm_app(crm_path, self.tokens, activity_enabled=False, policy_config=policy),
            "workbench": workbench_app(
                store=self.workbench, crm_url=self.urls["crm"],
                observer_token=self.tokens["observer"], action_token=self.tokens["actions"],
                user_token=self.tokens["maya"], token_actors={self.tokens["maya"]: "emp-maya-chen"},
                crm_tokens={
                    "service:renewal-tracker": self.tokens["renewal_tracker"],
                    "service:support-lookup": self.tokens["support_lookup"],
                    "agent:lead-enrichment": self.tokens["lead_enrichment"],
                    "agent:renewal-digest": self.tokens["renewal_digest"],
                },
            ),
            "outside": outside_app(root / "outside.sqlite3"),
        }
        for name, app in apps.items():
            gateway.routes[self.prefixes[name]] = app

    def close(self):
        for prefix in self.prefixes.values():
            self.gateway.routes.pop(prefix, None)

    def request(self, service, method, path, *, token=None, body=None):
        headers = {"Authorization": f"Bearer {self.tokens[token]}"} if token else {}
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.request(method, self.urls[service] + path, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
