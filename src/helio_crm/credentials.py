"""Generate local-only demo credentials. Never print their values."""

import os
import secrets
from pathlib import Path

ENV_KEYS = {"admin": "CRM_ADMIN_TOKEN", "maya": "CRM_MAYA_TOKEN", "agent": "CRM_AGENT_TOKEN",
            "observer": "CRM_OBSERVER_TOKEN", "recovery": "CRM_RECOVERY_TOKEN", "publisher": "CRM_IDENTITY_PUBLISHER_TOKEN"}


def validate_credentials(tokens):
    if not {"admin", "maya", "agent"} <= set(tokens) <= set(ENV_KEYS) or any(not isinstance(t, str) or len(t) < 32 for t in tokens.values()):
        raise ValueError("Distinct credentials of at least 32 characters are required; run make setup")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Each principal must have different credentials")


def from_environment():
    tokens = {key: os.getenv(variable, "") for key, variable in ENV_KEYS.items()
              if key not in {"observer", "recovery", "publisher"} or variable in os.environ}
    validate_credentials(tokens)
    return tokens


def generate_credentials(path: Path):
    if path.exists():
        pairs = [line.split("=", 1) for line in path.read_text().splitlines()
                 if "=" in line and not line.lstrip().startswith("#")]
        entries = dict(pairs)
        try:
            if len(entries) != len(pairs):
                raise ValueError("Duplicate environment keys")
            if not {"CRM_ADMIN_TOKEN", "CRM_MAYA_TOKEN", "CRM_AGENT_TOKEN"} <= set(entries):
                raise ValueError("Unexpected keys")
            validate_credentials({key: entries[env_key] for key, env_key in ENV_KEYS.items() if env_key in entries})
        except (ValueError, KeyError) as error:
            raise ValueError("Refusing to overwrite existing environment file; review it locally") from error
        path.chmod(0o600)
        for name in ("CRM_OBSERVER_TOKEN", "CRM_RECOVERY_TOKEN", "CRM_IDENTITY_PUBLISHER_TOKEN"):
            if name not in entries:
                separator = "" if path.read_text().endswith("\n") else "\n"
                with path.open("a") as output:
                    output.write(f"{separator}{name}={secrets.token_urlsafe(32)}\n")
        return
    # Exclusive creation prevents accidental rotation or overwriting other settings.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("".join(f"{name}={secrets.token_urlsafe(32)}\n" for name in ENV_KEYS.values()))


if __name__ == "__main__":
    generate_credentials(Path(".env"))
    print("Local demo credentials ready in .env (values hidden; do not commit).")
