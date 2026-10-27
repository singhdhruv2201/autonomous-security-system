"""Operator convenience commands; credentials are read locally and never printed."""

import argparse
import json
import os
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["overview", "findings", "connectors", "runs", "sync", "audit"])
    command = parser.parse_args().command
    path = "/sync-runs" if command in {"runs", "sync"} else "/" + command
    variable = "SECURITY_OPERATOR_TOKEN" if command in {"sync", "audit"} else "SECURITY_VIEWER_TOKEN"
    token = os.getenv(variable)
    if not token:
        print(json.dumps({"error": "Missing security API credential; run make setup"}))
        return 1
    request = urllib.request.Request(os.getenv("SECURITY_API_URL", "http://127.0.0.1:8002").rstrip("/") + path,
        method="POST" if command == "sync" else "GET", data=b"{}" if command == "sync" else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(json.dumps(json.load(response), indent=2))
        return 0
    except urllib.error.HTTPError as error:
        error.close()
        print(json.dumps({"error": f"http_{error.code}"}))
        return 2 if error.code in {401, 403, 409} else 1
    except (OSError, ValueError):
        print(json.dumps({"error": "Security API unavailable or returned an invalid response"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
