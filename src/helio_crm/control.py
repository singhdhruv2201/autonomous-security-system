"""Local operator helper. Uses real HTTP APIs without printing credentials."""

import argparse
import json
import os
import urllib.error
import urllib.request


def request(method, path, token, body=None):
    base = os.environ.get("CRM_URL", "http://127.0.0.1:8001")
    req = urllib.request.Request(base + path, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["accounts", "grants", "audit", "revoke-agent", "update-customer"])
    args = parser.parse_args()
    admin = os.environ["CRM_ADMIN_TOKEN"]
    if args.command == "revoke-agent":
        grant = next(g for g in request("GET", "/grants", admin) if g["id"] == "grant-sales-agent")
        result = request("POST", "/grants/grant-sales-agent/revoke", admin,
                         {"expected_version": grant["version"]})
    elif args.command == "update-customer":
        maya = os.environ["CRM_MAYA_TOKEN"]
        customer = request("GET", "/customers/cust-northstar", maya)
        result = request("PATCH", "/customers/cust-northstar", maya,
                         {"expected_version": customer["version"], "notes": "Renewal scheduled by Maya"})
    else:
        result = request("GET", "/" + args.command, admin)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"CRM request failed (HTTP {error.code})")
