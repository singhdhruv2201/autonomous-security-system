"""Upgrade local credentials without changing any existing identity."""

import secrets
from pathlib import Path

from helio_crm.credentials import generate_credentials


def setup(path):
    generate_credentials(path)
    content = path.read_text()
    entries = dict(line.split("=", 1) for line in content.splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
    if "NEO4J_PASSWORD" in entries and len(entries["NEO4J_PASSWORD"]) < 8:
        raise ValueError("Existing Neo4j password is invalid; review .env locally")
    additions = {}
    for name in ("NEO4J_PASSWORD", "SECURITY_OPERATOR_TOKEN", "SECURITY_VIEWER_TOKEN", "CRM_WEBHOOK_SECRET"):
        if name not in entries:
            additions[name] = secrets.token_urlsafe(32)
        elif name != "NEO4J_PASSWORD" and len(entries[name]) < 32:
            raise ValueError("Existing security API token is invalid; review .env locally")
    credentials = [value for key, value in {**entries, **additions}.items()
                   if key.endswith(("_TOKEN", "_SECRET")) or key == "NEO4J_PASSWORD"]
    if len(set(credentials)) != len(credentials):
        raise ValueError("Service credentials must be distinct; review .env locally")
    if additions:
        with path.open("a") as output:
            separator = "" if content.endswith("\n") else "\n"
            output.write(separator + "".join(f"{name}={value}\n" for name, value in additions.items()))


if __name__ == "__main__":
    setup(Path(".env"))
    print("Local CRM, Neo4j and security API credentials ready; values hidden.")
