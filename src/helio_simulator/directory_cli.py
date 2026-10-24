"""Local operator changes; the directory HTTP API stays read-only."""

import argparse
import json
import os
from pathlib import Path

from .directory import Directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("employee_id")
    parser.add_argument("status", choices=["active", "inactive"])
    args = parser.parse_args()
    directory = Directory(Path(os.getenv("HELIO_DB_PATH", "data/helio.sqlite3")))
    directory.initialize()
    try:
        result = directory.set_employee_status(args.employee_id, args.status)
    except KeyError:
        raise SystemExit("Employee not found")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
