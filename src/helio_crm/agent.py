"""A deterministic sales agent using the same HTTP boundary as an external client."""

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter


def build_summary(customers):
    return {"customer_count": len(customers),
            "by_status": dict(Counter(c["subscription_status"] for c in customers)),
            "accounts": [f"{c['name']} ({c['plan']}, {c['subscription_status']})" for c in customers]}


def run(base_url, token):
    request = urllib.request.Request(base_url.rstrip("/") + "/customers",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return build_summary(json.load(response))


def main():
    token = os.environ.get("CRM_AGENT_TOKEN")
    if not token:
        print("Agent credential is missing", file=sys.stderr)
        return 1
    try:
        result = run(os.getenv("CRM_URL", "http://127.0.0.1:8001"), token)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            print(f"Sales Research Agent blocked by CRM (HTTP {error.code}).", file=sys.stderr)
            return 2
        print(f"CRM request failed (HTTP {error.code}).", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError):
        print("CRM unavailable; no customer summary generated.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
