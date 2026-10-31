# CRM activity contract — phases 0 and 1

Historical phase-0/1 contract and verification record. Phases 2–4 now extend this
with HTTP lifecycle evidence, schema v4, a signed receiver and background sender;
see [the current delivery guide](activity-delivery.md) for runtime behavior.

Contract version: 1. Scope: the owned, single-company Helio CRM. Phase 1 emits
operation events only; HTTP lifecycle capture, webhook transport, policy decisions,
and automatic remediation belong to later phases.

## Baseline and acceptance boundaries

The starting unit/API suite reports 246 passed and 16 skipped (Neo4j tests), with
one existing Starlette/httpx deprecation warning. Run the real Neo4j suite separately
when available. Tests must use temporary CRM databases and isolated graph namespaces.

Phase 1 is complete only after proving atomic operation/event/outbox persistence,
denial rollback, safe failures/redaction, v1/v2 migration, restart persistence,
concurrent ordering and unchanged authorization. Capture defaults off; delivery has
no worker or network endpoint in this phase. Phase 2 must add actual HTTP evidence.

Verification completed 2026-10-30:

- Baseline with real Neo4j: 262 passed.
- Added 67 tests across `test_crm_activity.py`, `test_crm_activity_api.py`, and
  `test_crm_activity_migrations.py`. Behavioral changes followed observed failing
  tests before implementation, followed by focused passing regression runs.
- Final `.venv/bin/python -m pytest -q`: 313 passed, 16 Neo4j-dependent tests skipped.
- Final `make test-integration`: **329 passed, no skips**, including real Neo4j,
  local HTTP services, and temporary SQLite databases. The existing Starlette/httpx
  deprecation warning remains; no dependencies were changed.
- `docker compose config --quiet`, documentation reference checks and
  `git diff --check` passed. No UI behavior changed, so no new UI tests were needed.
- Focused standard-library line tracing exercised all 72 executable source lines
  in the new activity module (excluding the synthetic line-0 module marker). This
  is line execution evidence, not a claim of exhaustive branch coverage.

Failure tests include partial writes followed by handled/unhandled errors, audit/
event/outbox insertion failures, a deferred foreign-key failure at commit, real
SQLite disk-full and lock-timeout conditions, concurrent stream-capacity attempts,
simultaneous revocations, interrupted migrations, corrupt schema/source identity,
restart and disable/re-enable behavior, and secret-bearing invalid inputs. The
partial-write test exposed and fixed a pre-existing savepoint/rollback gap.

Implementation and verification used isolated test state. Existing running Docker
services were not redeployed and capture was not enabled in the user's live demo.

## Coverage matrix

| Surface | Action | Phase 1 stream | Later evidence |
| --- | --- | --- | --- |
| GET `/customers`, GET `/customers/{id}` | `customers:read` | Security | HTTP request/prepared/completed response |
| PATCH `/customers/{id}` | `customers:write` | Security | Request validation and response lifecycle |
| POST `/grants/{id}/revoke` | `grants:revoke` | Security | Request validation and response lifecycle |
| GET `/accounts` | `accounts:read` | Operational on success; security on denial/failure | HTTP lifecycle |
| GET `/grants` | `grants:read` | Operational on success; security on denial/failure | HTTP lifecycle |
| GET `/inventory` | `inventory:read` | Operational on success; security on denial/failure | HTTP lifecycle |
| GET `/audit` | `audit:read` | Operational on success; security on denial/failure | HTTP lifecycle |
| Missing/unknown credentials reaching the store | Requested canonical operation | Security, actor unknown | HTTP authentication evidence |
| Store-level 404/409/422 and unexpected operation errors | Requested canonical operation | Security | Actual HTTP outcome, if any |
| Framework validation, unknown route/method, public `/`, `/health`, docs/OpenAPI | No store operation | Not captured yet | Phase 2 API/operational instrumentation |
| `control.py` commands and `agent.py` reads | Same HTTP/store actions above | Same as corresponding operation | Phase 2 HTTP lifecycle |
| Direct calls to public CRM store operations | Same canonical actions | Same operation events | No HTTP response is invented |
| Bootstrap/migration/configuration | Startup lifecycle | No synthetic business event | Operational deployment reporting |
| Webhook delivery and recovery reads | Not implemented | Future operational-only audit | Must never recursively enter exported stream |

No current background business job bypasses these store operations. Direct SQL,
host compromise, pre-application network rejection, directory CLI changes and client
receipt of bytes are outside this CRM operation boundary. Directory snapshots remain
the source of employee employment status.

## Event identity and fields

An immutable event has `schema_version: 1`, `event_type`, `company_id: "helio"`,
`source: "helio-crm"`, a persisted UUID `source_instance_id`, and `stream` (`security`
or `operational`). A monotonically increasing `sequence` belongs to that instance
and stream; gaps must not be interpreted as safety. Every event has UUID `event_id`,
UUID `request_id` and UTC `occurred_at`. Source instance survives restart/migration;
a deliberate new database gets a new instance. Restoring a database backup requires
an explicit future source-epoch recovery procedure before delivery resumes.

Phase 1 generates one request ID per store operation and reuses it in the existing
audit row. It is not yet an HTTP request ID. Phase 2 will supply trusted request
context for the complete lifecycle; clients cannot choose the identity or tenant.

The current `operation.completed` payload also contains:

- `actor_id`, `actor_kind`, `owner_employee_id`: from authenticated database state,
  or null for unknown authentication. Ownership does not imply delegation.
- `action`: one of the canonical actions in the matrix.
- `resource_id`: `customer-database`, `crm-metadata`, or `crm-audit`; `record_id` is
  a known database record ID or null. Never echo an unknown caller-supplied ID.
- `outcome`: `allowed`, `denied`, or `failed`; `status_code` is the operation's
  expected HTTP mapping, not proof of an emitted HTTP response.
- `reason_code`: `operation_completed`, `authentication_failed`, `access_denied`,
  `resource_not_found`, `version_conflict`, `invalid_operation`, or `operation_failed`.
- `result_record_count`: number of customer records produced by a successful read;
  null for other operations/failures. This is not a count of delivered records.
- `changed_fields`: actual changed allowlisted field names; `before_version` and
  `after_version` for a successful versioned mutation. Failed operations have no
  committed change/version evidence. Idempotent revocation has no changed fields.

Storage keeps `sequence`, `event_id`, `request_id`, and `occurred_at` as columns;
`payload` holds the remaining JSON fields. A future wire serializer combines them
without modifying evidence. `activity_events` contains security events and each
has exactly one `activity_outbox` row referencing its sequence. All outbox rows are
pending in this phase. `operational_events` has an independent sequence and no
outbox. There is no implicit notification or delivery acknowledgement.

Keep the old `/audit` shape/order/authorization. Sanitize free-form exception text
and unknown resource identifiers before audit persistence; only established safe
diagnostics belong there. Do not backfill invented modern events from old audits.

## Lifecycle and examples

| Stage | Meaning | Implementation |
| --- | --- | --- |
| `request.received` | Request reached bounded application parsing; identity may be unknown | Phase 2 |
| `operation.completed` | Business result/denial and its audit committed together | Phase 1 |
| `response.prepared` | Intended status, classification, actual result count/serialized bytes, scan status, release decision | Phase 2 hooks; detectors later |
| `response.completed` | Observed status/emitted bytes, duration, completion/disconnection | Phase 2; absent evidence remains unknown |

Example current payload (envelope IDs/time omitted):

```json
{
  "schema_version": 1,
  "event_type": "operation.completed",
  "company_id": "helio",
  "source": "helio-crm",
  "source_instance_id": "00000000-0000-4000-8000-000000000001",
  "stream": "security",
  "actor_id": "agent:sales-research",
  "actor_kind": "agent",
  "owner_employee_id": "emp-maya-chen",
  "action": "customers:read",
  "resource_id": "customer-database",
  "record_id": null,
  "outcome": "allowed",
  "status_code": 200,
  "reason_code": "operation_completed",
  "result_record_count": 6,
  "changed_fields": [],
  "before_version": null,
  "after_version": null
}
```

Other acceptance examples:

| Case | Required evidence |
| --- | --- |
| Agent tries to edit customer | `denied`, 403, `access_denied`, authenticated agent; no changed fields or customer content |
| Unknown credential | `denied`, 401, `authentication_failed`; actor fields null; no token or token hash |
| Stale customer version | `failed`, 409, `version_conflict`; no committed changes |
| Invalid direct-store update | `failed`, 422, `invalid_operation`; no raw submitted values |
| API rejects invalid JSON before store | No phase-1 event; future API lifecycle has safe validation code and 422, not an invented operation |
| Operation writes then raises | Rollback business writes; append `failed`, 500, `operation_failed` if audit storage remains usable; no exception text |
| Response disconnect after committed update | Preserve successful operation; future response event records interruption or remains unknown; never undo/invent database state |

Bodies, notes, emails, token hashes, credentials, exception messages, arbitrary
headers/URLs and unrecognized field names never enter new event payloads. Known
record/actor IDs originate from the trusted local database. This metadata-only
contract cannot diagnose content that was not inspected. Scan results remain
`not_run` until actual scanners are introduced; phase 1 emits no scan result.

## Atomicity, configuration, and retention

Use the existing transaction-level authorization boundary. A savepoint wraps the
operation so rejected/failed partial writes roll back before committing their audit
and event. Failure writing audit/event/outbox rolls back the whole transaction and
prevents returning success or data. Unexpected application errors are converted to
a safe 500 only after recording the failure where possible; unavailable storage
maps to a safe 503, with no claim that an event was saved. Process termination or
unrecoverable storage failure cannot guarantee a failure event.

`CRM_ACTIVITY_ENABLED=0` is the default; `1` opts a local demo into capture.
Programmatic stores accept an explicit boolean. Disabling capture preserves evidence
and continues the existing legacy audit; it does not delete pending rows or restore
access. This configuration switch is a deployment setting, not an API parameter.
Delivery cannot be enabled yet.

For the controlled phase-1 demo, retain events indefinitely and never silently
prune pending evidence. Cap each enabled stream at 10,000 rows by default (a positive
bounded constructor override supports tests). Reject further store operations with
safe 503 when their required stream cannot record them; no business side effects
may commit. No automatic cleanup endpoint exists. Disk-full behavior also fails
closed. Production retention, deletion, archival and alerting require later phases;
keep capture disabled on unattended deployments until those controls exist.

## Future transport contract (specified, not implemented)

Phase 3 will accept one bounded UTF-8 JSON event per request (initial maximum 64 KiB)
at `POST /webhooks/crm/activity`. Metadata strings and IDs must be bounded by their
runtime schema; unknown schema versions are rejected. The signing key maps to the
permitted company/source. Required headers are `X-Awasi-Key-Id`,
`X-Awasi-Delivery-Id`, `X-Awasi-Timestamp` (integer Unix seconds), and
`X-Awasi-Signature` (`v1=` plus lowercase HMAC-SHA256 hex). Sign the exact bytes:

```text
UTF8("awasi-v1\n" + key_id + "\n" + delivery_id + "\n" + timestamp + "\n") || raw_body
```

Header values cannot contain line breaks; delivery ID is a UUID. Verify exact bytes
and compare MACs in constant time before accepting parsed evidence. Initial replay
tolerance is 300 seconds. Retry with a freshly signed delivery timestamp while
preserving the event identity and occurrence time. HTTPS verification is required
outside an explicit isolated-development network. No user-supplied destination URL.

Return 202 after durable inbox/pending-work commit, 200 for an identical previously
stored event, 409 for conflicting identity/content, 401 for invalid authentication,
403 for authenticated source/company mismatch, 413 for excessive body size, 422 for
invalid event schema, and 503 when durable receipt is unavailable. Later sender
retries 429/5xx/transport failures; auth/schema errors are retained for correction.
Recovery uses separately scoped credentials and shares event deduplication. No
delivery status authorizes a CRM action or establishes that remediation succeeded.
