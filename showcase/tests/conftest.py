"""Use real service handlers over ASGI when this executor cannot bind loopback sockets."""
from contextlib import nullcontext
import io
import socket
from types import SimpleNamespace
import urllib.error
import urllib.request

from fastapi.testclient import TestClient
import httpx
import pytest

from helio_security import connectors, playbooks, workbench_playbooks
from helio_workbench import api as workbench_api
from showcase import app, services


@pytest.fixture(autouse=True)
def service_transport(monkeypatch):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            pass
        else:
            return

    class ASGIServices(services.LoopbackServices):
        async def __aenter__(self):
            self.base = "http://127.0.0.1:13070"
            bridge = HttpBridge(self)
            for module in (playbooks, workbench_playbooks, services, workbench_api):
                monkeypatch.setattr(module, "httpx", bridge)
            monkeypatch.setattr(connectors, "urllib", SimpleNamespace(
                error=urllib.error, request=SimpleNamespace(
                    Request=urllib.request.Request, build_opener=lambda *_: self,
                ),
            ))
            return self

        async def __aexit__(self, *_):
            pass

        def open(self, request, **_):
            response = TestClient(self).get(request.full_url, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, response.status_code, "", {}, None)
            return io.BytesIO(response.content)

    class HttpBridge:
        def __init__(self, gateway):
            self.gateway = gateway

        def __getattr__(self, name):
            return getattr(httpx, name)

        def Client(self, **_):
            return nullcontext(TestClient(self.gateway))

        def AsyncClient(self, **options):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.gateway), **options)

        def get(self, url, *, timeout, **options):
            with self.Client() as client:
                return client.get(url, **options)

        def post(self, url, *, timeout, **options):
            with self.Client() as client:
                return client.post(url, **options)

    monkeypatch.setattr(app, "LoopbackServices", ASGIServices)
