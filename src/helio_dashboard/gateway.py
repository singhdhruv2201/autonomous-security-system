"""Fixed, bounded security API requests. No browser-supplied upstream URLs."""

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from helio_security.connectors import NoRedirect


class UpstreamError(Exception):
    pass


class Gateway:
    def __init__(self, base_url, viewer_token, operator_token):
        self.base_url = base_url.rstrip("/")
        self.viewer_token, self.operator_token = viewer_token, operator_token

    @classmethod
    def from_environment(cls):
        viewer, operator = os.getenv("SECURITY_VIEWER_TOKEN", ""), os.getenv("SECURITY_OPERATOR_TOKEN", "")
        if min(len(viewer), len(operator)) < 32 or viewer == operator:
            raise ValueError("Dashboard credentials missing or invalid; run make setup")
        return cls(os.getenv("SECURITY_API_URL", "http://127.0.0.1:8002"), viewer, operator)

    def request(self, path, method="GET"):
        token = self.operator_token if method == "POST" else self.viewer_token
        request = urllib.request.Request(self.base_url + path, method=method,
            data=b"{}" if method == "POST" else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise UpstreamError("oversized")
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise UpstreamError("invalid_payload")
                return response.status, result
        except urllib.error.HTTPError as error:
            with error:
                if error.code == 409:
                    try:
                        payload = json.loads(error.read(4096))
                        run_id = str(uuid.UUID(payload["detail"]["run_id"]))
                        return 409, {"detail": {"code": "sync_in_progress", "run_id": run_id}}
                    except (KeyError, ValueError, TypeError):
                        pass
                raise UpstreamError(f"upstream_{error.code}") from None
        except (OSError, urllib.error.URLError):
            raise UpstreamError("unavailable") from None
        except (ValueError, TypeError):
            raise UpstreamError("invalid_payload") from None

    def state(self):
        paths = {"overview": "/overview", "inventory": "/entities?limit=200",
                 "graph": "/graph?limit=200&edge_limit=500", "findings": "/findings?limit=200",
                 "cases": "/cases?limit=200", "connectors": "/connectors", "runs": "/sync-runs?limit=12", "policies": "/policies"}
        result = {"errors": {}, "fetched_at": time.time()}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {name: pool.submit(self.request, path) for name, path in paths.items()}
            for name, future in futures.items():
                try:
                    result[name] = future.result()[1]
                except UpstreamError as error:
                    result[name] = None
                    result["errors"][name] = str(error)
        return result
