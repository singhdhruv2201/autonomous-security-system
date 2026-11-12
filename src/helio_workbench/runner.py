"""Workbench runner CLI for triggering agent executions."""
import argparse
import json
import os
import urllib.error
import urllib.request


def request(method, path, token, body=None):
    base = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8004")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        base + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_id", nargs="?", default="agent-lead-enrichment", help="Agent ID to run")
    args = parser.parse_args()

    user_token = (
        os.environ.get("WORKBENCH_USER_TOKEN")
        or os.environ.get("CRM_MAYA_TOKEN", "")
    )

    result = request("POST", f"/agents/{args.agent_id}/run", user_token)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Workbench agent run failed (HTTP {error.code}): {detail}")
