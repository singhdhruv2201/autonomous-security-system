"""Generate local-only demo credentials. Never print their values."""

import os
import secrets
from pathlib import Path

ENV_KEYS = {
    "admin": "CRM_ADMIN_TOKEN",
    "maya": "CRM_MAYA_TOKEN",
    "agent": "CRM_AGENT_TOKEN",
    "observer": "CRM_OBSERVER_TOKEN",
    "recovery": "CRM_RECOVERY_TOKEN",
    "publisher": "CRM_IDENTITY_PUBLISHER_TOKEN",
    "actions": "CRM_ACTION_TOKEN",
}

WORKBENCH_ENV_KEYS = {
    "renewal_tracker": "CRM_RENEWAL_TRACKER_TOKEN",
    "support_lookup": "CRM_SUPPORT_LOOKUP_TOKEN",
    "lead_enrichment": "CRM_LEAD_ENRICHMENT_TOKEN",
    "renewal_digest": "CRM_RENEWAL_DIGEST_TOKEN",
}

OPTIONAL_ENV_KEYS = {
    "observer",
    "recovery",
    "publisher",
    "actions",
}


def validate_credentials(tokens):
    all_known = {**ENV_KEYS, **WORKBENCH_ENV_KEYS}
    if not {"admin", "maya", "agent"} <= set(tokens) <= set(all_known) or any(not isinstance(t, str) or len(t) < 32 for t in tokens.values()):
        raise ValueError("Distinct credentials of at least 32 characters are required; run make setup")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("Each principal must have different credentials")


def from_environment():
    tokens = {key: os.getenv(variable, "") for key, variable in ENV_KEYS.items()
              if key not in OPTIONAL_ENV_KEYS or variable in os.environ}
    for key, variable in WORKBENCH_ENV_KEYS.items():
        if variable in os.environ and os.environ[variable]:
            tokens[key] = os.environ[variable]
    validate_credentials(tokens)
    return tokens


def generate_credentials(path: Path, include_workbench: bool = False):
    if path.exists():
        pairs = [line.split("=", 1) for line in path.read_text().splitlines()
                 if "=" in line and not line.lstrip().startswith("#")]
        entries = dict(pairs)
        try:
            if len(entries) != len(pairs):
                raise ValueError("Duplicate environment keys")
            if not {"CRM_ADMIN_TOKEN", "CRM_MAYA_TOKEN", "CRM_AGENT_TOKEN"} <= set(entries):
                raise ValueError("Unexpected keys")
            all_known = {**ENV_KEYS, **WORKBENCH_ENV_KEYS}
            validate_credentials({key: entries[env_key] for key, env_key in all_known.items() if env_key in entries})
        except (ValueError, KeyError) as error:
            raise ValueError("Refusing to overwrite existing environment file; review it locally") from error
        path.chmod(0o600)
        optional_names = [
            "CRM_OBSERVER_TOKEN",
            "CRM_RECOVERY_TOKEN",
            "CRM_IDENTITY_PUBLISHER_TOKEN",
            "CRM_ACTION_TOKEN",
        ]
        if include_workbench:
            optional_names.extend(WORKBENCH_ENV_KEYS.values())
        for name in optional_names:
            if name not in entries:
                separator = "" if path.read_text().endswith("\n") else "\n"
                with path.open("a") as output:
                    output.write(f"{separator}{name}={secrets.token_urlsafe(32)}\n")
        return
    # Exclusive creation prevents accidental rotation or overwriting other settings.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        names = list(ENV_KEYS.values())
        if include_workbench:
            names.extend(WORKBENCH_ENV_KEYS.values())
        output.write("".join(f"{name}={secrets.token_urlsafe(32)}\n" for name in names))


if __name__ == "__main__":
    generate_credentials(Path(".env"))
    print("Local demo credentials ready in .env (values hidden; do not commit).")
