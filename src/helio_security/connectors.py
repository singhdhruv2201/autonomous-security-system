"""Read-only HTTP connectors. Errors deliberately exclude remote bodies and secrets."""

import json
import urllib.error
import urllib.request

from .models import normalize

MAX_BYTES = 2 * 1024 * 1024


class ConnectorError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_snapshot(source, base_url, observer_token=None):
    if source not in {"directory", "crm", "workbench"}:
        raise ConnectorError("unknown_source")
    if source != "directory" and not observer_token:
        raise ConnectorError("missing_credential")
    path = "/snapshot" if source == "directory" else "/inventory"
    headers = {"Authorization": f"Bearer {observer_token}"} if source != "directory" else {}
    request = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ConnectorError("oversized")
        return normalize(source, json.loads(body))
    except urllib.error.HTTPError as error:
        error.close()
        raise ConnectorError(f"http_{error.code}") from None
    except (urllib.error.URLError, OSError):
        raise ConnectorError("unavailable") from None
    except (ValueError, TypeError):
        raise ConnectorError("invalid_payload") from None
