# Working CRM and Sales Research Agent

## Architecture

```text
Company directory (:8000) ---- bootstrap identities ----> CRM (:8001)
       |                                                   |
  company-data volume                                crm-data volume
  teams / employees                              customers / grants / audit
                                                           ^
                                                           | HTTP + bearer token
                                                Sales Research Agent
                                                separate, read-only container
                                                no database volume or admin token
```

This is a real API application using synthetic business data. The agent is a
deterministic Python program, not an LLM: it fetches customers and computes an
account summary from the response. An LLM is unnecessary to demonstrate the
authentication and revocation boundary.

## Identities and permissions

The CRM imports the directory's twenty employee identities on first startup,
retaining their stable employee IDs. It also creates an agent principal owned by
Maya and an administration principal owned by Casey, the CTO. Only three demo
principals originally received credentials; the graph milestone adds a fourth,
read-only observer:

| Principal | Credential environment variable | Permission |
| --- | --- | --- |
| Maya Chen | `CRM_MAYA_TOKEN` | Read customers; update notes and subscription status |
| Sales Research Agent | `CRM_AGENT_TOKEN` | Read customers only |
| CRM Administration | `CRM_ADMIN_TOKEN` | Inspect accounts/grants/audit and revoke grants |
| Security Observer | `CRM_OBSERVER_TOKEN` | Read accounts, grants, and inventory metadata only |

Administration does not implicitly permit reading customer records. The other
nineteen employee accounts have no demo credentials or grants yet.

Tokens are generated locally and only SHA-256 hashes are stored in the CRM
database. Use Bearer authentication for protected endpoints. The Swagger UI's
Authorize button accepts one of the locally generated token values if interactive
inspection is needed. The make targets use credentials without printing them.
The CRM process receives all four bootstrap tokens in its environment; this is
local demo provisioning, not a production secret-management implementation.

Grant `grant-sales-agent` permits `customers:read` on `customer-database` through
`helio-crm`. `grant-maya` permits both `customers:read` and `customers:write`.
Permissions apply to the six-record resource as a whole, not individual rows.
The CRM checks current grants inside the same SQLite transaction as each
operation. Revoking a grant prevents subsequent requests; it cannot recall data
the agent has already fetched.

## Intentional lifecycle gap

The directory is the authority for employment status. The CRM initially imports
identity information, not employment status, and has no automatic offboarding
connector. Consequently directory offboarding does not disable either Maya's
CRM credential or her separately credentialed agent. This is the explicit
integration gap that the graph now detects. Remediation is still a manual CRM action.

The directory API remains read-only. Trusted local operators can run:

```sh
make offboard
make restore-employee
```

After bootstrap, CRM restarts do not require a fresh directory snapshot and do
not restore revoked grants. Changing local tokens without migrating stored hashes
causes startup to fail rather than silently rotating or reviving credentials.

## API contract

| Method | Path | Access / behavior |
| --- | --- | --- |
| GET | `/customers` | Requires active read grant |
| GET | `/customers/{id}` | Requires active read grant; 404 if absent |
| PATCH | `/customers/{id}` | Requires write grant; accepts `expected_version`, `notes`, `subscription_status` |
| GET | `/accounts` | Admin or observer; employee accounts, agent, and service principals |
| GET | `/grants` | Admin or observer; ownership, resource, scopes, status, version |
| GET | `/inventory` | Admin or observer; atomic app, resource, account, and grant metadata snapshot |
| POST | `/grants/{id}/revoke` | Admin; body contains `expected_version` |
| GET | `/audit?limit=100` | Admin; newest first, maximum 500 records |
| GET | `/health` | Unauthenticated readiness check; no customer data |

Updates accept only notes (up to 500 characters) and subscription status
(`active`, `trial`, `cancelled`). Extra fields and invalid values are rejected.
Customer versions increment on updates. Stale updates or revocations return 409.
Revoking an already-revoked grant succeeds without another version increment.
Unknown or missing credentials return 401; known credentials without the
necessary permission return 403.

The observer's grant (`grant-security-observer`) targets `crm-metadata`, distinct
from the `customer-database` resource. It cannot read customer records, edit them,
read audit records, or revoke grants. Revoking the observer's grant prevents
subsequent metadata reads. The additive schema-v2 migration preserves all
existing credentials, customer data, and revocations; restarting never restores
a revoked observer grant.

Activity records contain UTC time, request ID, principal, operation, resource,
outcome, and HTTP status. They contain no bearer tokens or customer response
bodies. Authentication/authorization denials and valid operations that encounter
missing resources or stale versions are audited. Request parsing/validation errors
rejected by FastAPI before reaching the store are not yet audited. Audit reads
are recorded after collecting their result, so their own record appears on the
next read. The audit table is a local append-only application convention, not
tamper-proof storage.

## Repeat the scenario

To restore employment status without changing CRM grants:

```sh
make restore-employee
```

To intentionally discard **only CRM demo data and audit history**, keep `.env`
and the company directory volume, then run:

```sh
docker compose stop crm
docker compose rm -f crm
docker volume rm helio-simulator_crm-data
docker compose up -d --wait crm
make restore-employee
```

This recreates the six customers and original grants. It is an explicit local
test reset, not a remediation API. Do not reset data you want to preserve.

## Tests

```sh
make test
```

Tests use isolated temporary databases and randomly generated test credentials.
They exercise API and database enforcement, stale writes, read-only agent limits,
revocation, restart persistence, audit redaction, and directory offboarding.
The Docker walkthrough additionally exercises the actual network boundary.
