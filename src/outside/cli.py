"""Outside service CLI for inspecting received collections."""
import json
import os
import urllib.request


def main():
    base = os.environ.get("OUTSIDE_URL", "http://localhost:8005")
    with urllib.request.urlopen(base + "/received", timeout=5) as response:
        result = json.load(response)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
