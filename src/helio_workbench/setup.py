"""Generate distinct random workbench credentials without rotating existing environment."""

from pathlib import Path
import secrets

WORKBENCH_CREDENTIALS = (
    "CRM_RENEWAL_TRACKER_TOKEN",
    "CRM_SUPPORT_LOOKUP_TOKEN",
    "CRM_LEAD_ENRICHMENT_TOKEN",
    "CRM_RENEWAL_DIGEST_TOKEN",
)

LEGACY_DEFAULTS = {
    "CRM_RENEWAL_TRACKER_TOKEN": "token-crm-renewal-tracker-local-workbench",
    "CRM_SUPPORT_LOOKUP_TOKEN": "token-crm-support-lookup-local-workbench",
    "CRM_LEAD_ENRICHMENT_TOKEN": "token-crm-lead-enrichment-local-workbench",
    "CRM_RENEWAL_DIGEST_TOKEN": "token-crm-renewal-digest-local-workbench",
}


def setup(path: Path = Path(".env"), migrate_legacy: bool = False) -> None:
    if not path.exists():
        from helio_crm.credentials import generate_credentials

        generate_credentials(path)

    content = path.read_text()
    entries = dict(
        line.split("=", 1)
        for line in content.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )

    has_legacy = any(
        entries.get(k) == LEGACY_DEFAULTS.get(k) for k in WORKBENCH_CREDENTIALS
    )
    if has_legacy and not migrate_legacy:
        raise ValueError(
            "Configured credentials contain legacy defaults. "
            "Run explicit migration ('python -m helio_workbench.setup migrate') to rotate."
        )

    additions = {}
    lines = content.splitlines()
    replaced = False

    for key in WORKBENCH_CREDENTIALS:
        val = entries.get(key)
        if not val or (migrate_legacy and val == LEGACY_DEFAULTS.get(key)):
            new_val = secrets.token_urlsafe(32)
            if key in entries:
                lines = [
                    f"{key}={new_val}" if l.startswith(f"{key}=") else l
                    for l in lines
                ]
                replaced = True
            else:
                additions[key] = new_val

    if replaced:
        content = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
        path.write_text(content)

    if additions:
        separator = "" if content.endswith("\n") else "\n"
        with path.open("a") as output:
            output.write(
                separator + "".join(f"{k}={v}\n" for k, v in additions.items())
            )

    path.chmod(0o600)


def migrate(path: Path = Path(".env")) -> None:
    """Explicitly migrate legacy default credentials to fresh random tokens."""
    setup(path, migrate_legacy=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        env_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".env")
        migrate(env_file)
        print("Migrated legacy workbench credentials in", env_file)
    else:
        target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env")
        setup(target)
        print("Workbench credentials ready in .env (values hidden).")
