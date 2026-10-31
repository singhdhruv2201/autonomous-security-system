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
| Activity Reader | `CRM_RECOVERY_TOKEN` | Read the scoped activity recovery feed |
| Identity Publisher | `CRM_IDENTITY_PUBLISHER_TOKEN` | Publish directory context only; no customer reads or grant changes |

Administration does not implicitly permit reading customer records. The other
nineteen employee accounts have no demo credentials or grants yet.

Tokens are generated locally and only SHA-256 hashes are stored in the CRM
database. Use Bearer authentication for protected endpoints. The Swagger UI's
Authorize button accepts one of the locally generated token values if interactive
inspection is needed. The make targets use credentials without printing them.
The CRM process receives bootstrap tokens in its environment; this is
local demo provisioning, not a production secret-management implementation.
Recovery and identity publication use their own additional credentials.

Grant `grant-sales-agent` permits `customers:read` on `customer-database` through
`helio-crm`. `grant-maya` permits both `customers:read` and `customers:write`.
Permissions apply to the six-record resource as a whole, not individual rows.
The CRM checks current grants inside the same SQLite transaction as each
operation. Revoking a grant prevents subsequent requests; it cannot recall data
the agent has already fetched.

## Employment and credential lifecycle

The directory remains the authority for employment status. With `CRM_OWNER_MODE=enforce`,
a dedicated publisher sends a complete, versioned directory projection every 60 seconds.
Customer reads and writes require active owner/employee status and context younger
than 120 seconds. Missing, expired, incomplete, or replayed context never authorizes
access. The publisher retries a failed update after 5 seconds independently of Neo4j.
Observation mode records the decision while retaining base authorization. Off mode
retains the original offboarding demonstration. Employment changes do not revoke
credentials or grants; those remain separate explicit administrative actions.

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
| POST | `/identity-projection` | Publisher only; complete company/source/version/timestamps and employee statuses |
| PATCH | `/principals/{id}/access` | Admin only; expected access version, principal status, credential status, expiry |

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
rejected by FastAPI are captured by the opt-in request/response activity layer. Audit reads
are recorded after collecting their result, so their own record appears on the
next read. The audit table is a local append-only application convention, not
tamper-proof storage.

## Durable activity and delivery (phases 0–6)

Schema v4 includes `activity_events`, `activity_outbox`, `operational_events` and
`delivery_audit`, extending v3 with durable delivery state and leases.
Existing v1/v2/v3 databases migrate in one transaction while preserving customer
records, credential hashes, revoked grants, and legacy audits. The source-instance
UUID survives restart; incomplete/corrupt activity schemas fail startup. Older
binaries do not support schema v4. Back up the database before deploying a new
build; rollback requires a compatible build or restoring that backup, not resetting
schema metadata by hand.

Capture defaults off. To opt a controlled local Docker demo in after building:

```sh
CRM_ACTIVITY_ENABLED=1 docker compose up --build -d --no-deps crm
```

The Compose setting accepts only `0` or `1`. Local Uvicorn uses the same environment
variable; tests/programmatic callers can explicitly set
`CRMStore(path, activity_enabled=True)` or `create_app(..., activity_enabled=True)`.
This setting affects future operations and does not replay old audit history.
No deployment or capture setting is changed merely by editing the source.

Each protected store operation emits metadata about its outcome. Customer operations,
grant revocations, and denials/failures enter the security stream with a pending
outbox entry; successful metadata/audit reads enter only the operational stream.
Both streams correlate with legacy audit request IDs and the HTTP lifecycle.
HTTP requests also record received, prepared response and completion metadata.
See the [current capture and delivery guide](activity-delivery.md) and historical
[phase-0/1 activity contract](crm-activity-contract.md).

Business changes, legacy audit, activity event and pending delivery commit together.
Rejected partial writes roll back before the denial is recorded. An unusable
audit/event/outbox store returns a generic 503 and prevents success/data release.
Unexpected application errors produce a safe 500 and failure evidence when storage
is still usable. Unknown requested IDs and exception text are sanitized; payloads
never include customer bodies, notes, bearer tokens or token hashes.

The controlled-demo limit is 10,000 retained events per stream. A full required
operation stream prevents the business commit; a later response-audit failure
returns 503 but preserves any already committed operation. A background sender is
available, but there is no cleanup job or automatic pruning yet; leave capture off
for unattended use. To stop capture, recreate CRM with both `CRM_ACTIVITY_ENABLED=0`
and `CRM_WEBHOOK_ENABLED=0`; existing events and pending
deliveries remain. This does not change grants or disable the original audit log.
The database is application-append-only, not tamper-proof.

Framework-rejected requests, public routes, response sizes and observable interruptions
are covered. Response checks report `not_run` when scanning is disabled or outside
the supported customer-data boundary. Optional signed
webhooks persist events in a separate Awasi inbox; replay is admin-only. A dedicated
`events:read` principal can read the scoped recovery feed, and Awasi processes and
displays correlated requests. See [recovery and activity visibility](activity-processing.md)
for current enablement, grants, watermarks and coverage semantics. The delivery guide
still describes rotation, replay and the limits of completion evidence.

Focused checks:

```sh
.venv/bin/python -m pytest -q tests/test_crm_activity.py tests/test_crm_activity_api.py tests/test_crm_activity_migrations.py tests/test_crm_lifecycle.py tests/test_webhooks.py tests/test_webhook_worker.py
```

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


## Deterministic policies (phases 7–13)

The CRM evaluates policies inside its existing SQLite transaction. It makes no
LLM, graph, or network calls on this path. Authorization matches both the canonical
resource and `helio-crm` application. Existing admin and field restrictions always
apply, including when optional policies are off or observing. Rejected writes roll
back before the durable denial audit is committed; rejected responses release no
customer bytes. Policy decisions include rule/version, mode, boundary, result and
safe reason codes, linked to the request and exported through the signed webhook.

Optional settings default to `off` for compatibility. This workstation has all four
set to `enforce` in its ignored `.env`:

| Setting | Behavior |
| --- | --- |
| `CRM_OWNER_MODE` | `off`, `observe`, or `enforce`; active employee/owner with fresh identity context |
| `CRM_VOLUME_MODE` | Shared per-principal rolling budgets across list/get customer routes |
| `CRM_FAILURE_MODE` | Repeated authentication/authorization failure detection and temporary cooldown |
| `CRM_CONTENT_MODE` | Bounded note/response scanning before write commit or data release |
| `CRM_REQUEST_LIMIT` | Default 100 attempted authorized customer reads per rolling 60 seconds |
| `CRM_RECORD_LIMIT` | Default 100 prepared disclosures per rolling 60 seconds; repeated reads count again |
| `CRM_BULK_LIMIT` | Default blocks a single result of 50 or more records |

Apply changes with `make up`. The dashboard obtains actual modes from authenticated
CRM inventory, marks stale configuration unknown, and renders policy-specific
findings. `make setup` adds the publisher credential while preserving every existing
credential. Additive migrations preserve grants, revocations, activity identities,
legacy audit consumers and inactive-owner finding IDs/history. Back up SQLite before
upgrading; downgrading the executable across schema changes is not supported.

The shared transactional rolling-event store serializes competing requests. Record
reservations remain charged after a disconnect or lost response; delivery evidence
separately records attempted, prepared, reserved and completed-send counts. A
completed send does not prove that the caller consumed the data. Events include up
to 50 enumerated record IDs, never customer bodies. Clock rollback fails enforced
budgets closed. Credential expiry uses a durable clock watermark.

Failure detection defaults to five failures in 60 seconds and a 60-second cooldown.
Only credential/permission failures count; conflicts, missing records, owner-policy
blocks, and upstream errors do not. Authenticated requests share the principal
bucket across routes. Unknown credentials use an isolated fingerprint bucket, never
a caller-supplied username. Rotating unknown credentials is outside this detector's
coverage; this is not a network-wide brute-force limiter. Requests during cooldown
do not extend it and share one incident per cooldown. Temporary throttles return
429; they do not permanently disable
accounts. Webhook replay cannot increment CRM counters.

The initial detector set covers AWS access-key IDs, Slack token prefixes, Stripe
live-key prefixes, bearer tokens, explicitly labeled refresh tokens, and US SSN
patterns. Secret matches can block; SSN matches remain observation-only, including
in enforce mode. Matching is heuristic and cannot establish whether a credential is
live. No provider validation is performed. Unicode normalization and explicit
pattern/size/time bounds limit scanning: defaults are 65,536 UTF-8 bytes and 10 ms.
Oversize or incomplete scans are unknown and return 503 under enforcement. Only
category names enter evidence. Email, attachments, OCR, MCP and arbitrary prompt
content are outside this phase's scanner boundary.

Administrative access updates use `expected_version` and one or more of
`principal_status` (`active`/`disabled`), `credential_status` (`active`/`revoked`),
and `expires_at` (Unix time or null). `/accounts` and `/inventory` expose current
`access_version`, statuses and expiry without token hashes. Revoked static credentials
cannot be restored by this endpoint. Re-enabling a principal never revives a revoked
grant or credential. Disabled/expired administration credentials cannot use role-based
routes. Keep a separate valid administrator available for recovery; no emergency
HTTP bypass exists. Recovery with a separate administrator is tested in an isolated
fixture. Credential replacement/rotation is a later operation.

AI investigation and automatic revocation are still disabled. Historical activity
incidents remain open until a later incident-management workflow is implemented;
missing events or elapsed time never silently resolve them. The existing graph
posture rule retains its explicit resolve/reopen behavior.

### Acceptance evidence — 2026-10-31

- `make test-integration`: **500 passed**, including the real Neo4j tests, local
  HTTP services and isolated SQLite databases. `make test-ui`: **19 passed**.
  The ordinary pytest run passed 480 tests with 17 Neo4j skips before three final
  delivery/observe regressions were added; those three passed separately and are
  included in the final 500-test integration run. The existing Starlette/httpx
  deprecation warning remains.
- Covered exact app/resource permissions, cross-rule isolation and legacy finding
  migration, projection freshness/replay/outage, lifecycle recovery/restart, shared
  concurrent budgets and 49/50/100/101 boundaries, cooldown identity/replay,
  observe-mode behavior, redaction and blocked writes/releases. Failed response
  audit and disconnected delivery retain reserved disclosures without claiming a
  completed send. Detector fixtures include benign and synthetic positive inputs,
  Unicode, oversize, timeout and adversarial-length cases.
- All six Docker services are healthy with the four optional CRM modes enforcing.
  The graph posture rule observes; SSN matches remain observation-only. Browser
  checks verified actual modes and redacted findings, including ISO timestamps.
  A synthetic Stripe-pattern write returned 403, left the customer unchanged,
  and produced a durable visible finding in **0.872 seconds** after the final
  rebuild. Five invalid-token attempts were followed by 429; the valid agent
  continued to receive 200. These are synthetic test incidents, retained as history.
- Live directory offboarding blocked the owned agent after **46.359 seconds**;
  restoring the employee allowed access after **60.568 seconds**. The final
  employee status is active. Publication runs every 60 seconds, retries failures
  after five seconds, and projection expiry is 120 seconds. These are measured
  samples, not an instantaneous-offboarding guarantee.
- An isolated real-HTTP comparison used 200 sequential GET requests per mode,
  ten warm-ups, a 161-byte single-customer response, activity enabled and higher
  fixture budgets to avoid deliberate throttles. On an Apple M4 Pro, 48 GiB,
  macOS 26.6.2, Python 3.11.14, p95 was **5.303 ms** with optional policies off
  and **5.377 ms** enforcing: a **0.074 ms** difference. Runs were sequential;
  this host measurement is not a Docker throughput qualification or production SLO.
- Consistent SQLite backups were taken before rollout. Post-rollout checks confirmed
  existing customers, credential hashes and grants were preserved, SQLite integrity
  was `ok`, and the activity received/processed/source cursors matched with no gaps,
  pending deliveries or dead letters. Docker Desktop encountered a backend Go panic
  during one rebuild; an ordinary restart recovered it without a factory reset or
  volume deletion. The final tests and live smoke ran after recovery.

Local logs, benchmark details, screenshots and backups are in the ignored
`work/phase7-13-20261031/` directory. The existing 10,000-retained-event cap per
stream still applies; this work does not add phase-27 retention or qualify an
unattended production deployment.
