"""Upgrade local credentials without changing any existing identity."""

import secrets
from pathlib import Path

from helio_crm.credentials import generate_credentials


def setup(path):
    generate_credentials(path)
    content = path.read_text()
    entries = dict(line.split("=", 1) for line in content.splitlines() if "=" in line)
    if "NEO4J_PASSWORD" not in entries:
        with path.open("a") as output:
            separator = "" if content.endswith("\n") else "\n"
            output.write(f"{separator}NEO4J_PASSWORD={secrets.token_urlsafe(32)}\n")
    elif len(entries["NEO4J_PASSWORD"]) < 8:
        raise ValueError("Existing Neo4j password is invalid; review .env locally")


if __name__ == "__main__":
    setup(Path(".env"))
    print("Local CRM and Neo4j credentials ready; values hidden.")
