"""Workbench control CLI for local operator actions and demonstration triggers."""
import argparse
import json
import os
import urllib.error
import urllib.request


def request(method, path, token=None, body=None):
    base = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8004")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["publish-app", "restrict-app", "connect-enrichment", "status", "inventory", "outsider-view"],
    )
    args = parser.parse_args()

    action_token = os.environ.get("WORKBENCH_ACTION_TOKEN") or os.environ.get("CRM_ACTION_TOKEN", "")
    observer_token = os.environ.get("WORKBENCH_OBSERVER_TOKEN") or os.environ.get("CRM_OBSERVER_TOKEN", "")

    if args.command == "publish-app":
        inv = request("GET", "/inventory", observer_token)
        app = next(a for a in inv["apps"] if a["id"] == "app-renewal-tracker")
        result = request(
            "PATCH",
            f"/apps/{app['id']}",
            action_token,
            {"expected_version": app["version"], "visibility": "public"},
        )
    elif args.command == "restrict-app":
        inv = request("GET", "/inventory", observer_token)
        app = next(a for a in inv["apps"] if a["id"] == "app-renewal-tracker")
        result = request(
            "PATCH",
            f"/apps/{app['id']}",
            action_token,
            {"expected_version": app["version"], "visibility": "company"},
        )
    elif args.command == "connect-enrichment":
        user_token = os.environ.get("WORKBENCH_USER_TOKEN") or os.environ.get("CRM_MAYA_TOKEN", "")
        result = request(
            "POST",
            "/agents/agent-lead-enrichment/destinations",
            user_token,
            {"host": "enrich-leads", "url": "http://enrich-leads:8005/collect", "enabled": True},
        )
    elif args.command == "outsider-view":
        base = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8004")
        req = urllib.request.Request(base + "/public/renewals")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.load(response)
    elif args.command in ("status", "inventory"):
        result = request("GET", "/inventory", observer_token)
    else:
        result = {}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Workbench request failed (HTTP {error.code}): {detail}")
