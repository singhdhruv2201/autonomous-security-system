# CRM activity capture and webhook delivery — phases 2–4

This guide records the phase-2/4 transport boundary. Phases 5–6 now add recovery,
processing and dashboard visibility; use the [current startup and recovery guide](activity-processing.md).

The CRM captures request, operation, prepared response and observed response
completion. A background sender delivers security events to an independent Awasi
inbox. Receipt means **durably stored, pending processing**. Phases 5–6 implement event
processing, gap recovery and the Activity view; new policies and remediation remain
later work. The existing snapshot-based findings continue independently.

## Enable the local environment

Back up existing CRM data before upgrading. Schema v4 migrates the phase-1 v3
outbox transactionally, preserving event IDs, source identity, credentials, customer
state and revoked grants. v1/v2 upgrades remain supported. Older binaries cannot
read v4; rollback needs a compatible build or the pre-upgrade backup.

```sh
make setup
CRM_ACTIVITY_ENABLED=1 CRM_WEBHOOK_ENABLED=1 docker compose --profile activity up --build -d --wait crm awasi-ingest
```

`make setup` adds a distinct random `CRM_WEBHOOK_SECRET` without rotating existing
credentials. Secrets stay in the ignored, owner-only `.env`; do not print expanded
Compose configuration. Both feature flags default to `0` and accept only `0`/`1`.
Delivery requires capture to be enabled. Starting the optional receiver requires a
signing secret of at least 32 characters. Reuse the enablement variables on subsequent
CRM recreations, or save both flags in the local `.env`.

The `activity` Compose profile adds `awasi-ingest` on localhost port 8003 with its
own `activity-inbox` volume. It has no Neo4j or model dependency. CRM uses the fixed
internal URL `http://awasi-ingest:8003/webhooks/crm/activity`; this development-only
HTTP exception is explicitly enabled by Compose. Outside this sandbox configure
`CRM_WEBHOOK_URL` with HTTPS and leave `CRM_WEBHOOK_ALLOW_LOCAL=0`. The URL must have
exact path `/webhooks/crm/activity`, with no credentials, query or fragment.
There is no caller-controlled destination registration. Redirects and environment
HTTP proxies are not followed. TLS verification uses the standard system trust store.

For local Python processes, the entry point is `helio_security.webhooks:app`, with
`AWASI_INBOX_DB_PATH` (default `data/inbox.sqlite3`), `CRM_WEBHOOK_KEY_ID` (default
`crm-1`) and `CRM_WEBHOOK_SECRET`. The receiver binds keys to company `helio` and
source `helio-crm`; this is a single-company demo, not tenant provisioning.

## Evidence and response semantics

Every captured HTTP request receives a server-generated UUID. Store operations and
legacy audits reuse it; a direct store/CLI operation still generates its own ID.
Client request-ID headers are never trusted. Routes are allowlisted templates;
unknown routes become `unmatched`. Query values, headers, raw paths, bodies,
customer notes, token hashes and exception details are excluded from these events.

| Stage | Recorded evidence |
| --- | --- |
| `request.received` | Trusted request ID, method and route template; actor initially unknown |
| `operation.completed` | Existing transactional actor, action, outcome, count and version metadata |
| `response.prepared` | Actual returned status, serialized body bytes, authorized read count where applicable, classification, scan result and release decision |
| `response.completed` | Status passed to the transport, bytes whose ASGI send completed, elapsed milliseconds, completed/interrupted |

Validation errors and unknown methods/routes produce HTTP evidence without an
invented operation. Successful public and metadata requests stay in the independent
operational stream; protected customer operations and failures enter the security
stream. Denied metadata responses enter the security stream even though their
initial request event is operational. Correlation spans both streams when needed.

Bodies are buffered up to 2 MiB before release; excess responses return a safe 503.
This boundary supports the current bounded JSON API, not new streaming/export
features. The response-check hook is installed but has no scanner: it reports
`scan_status=not_run`, never a claimed clean scan. A future configured hook can
return passed/allow or matched/deny; invalid results or exceptions prevent release.
No response body is persisted or sent to Awasi.

Operation success and response delivery are independent. A response audit failure
returns 503 before data release but cannot undo a previously committed operation.
A caller retrying a write must still use the CRM version precondition. A transport
disconnect records interruption when observable; a process crash or completion-log
failure can leave missing completion evidence. Emitted bytes do not prove that the
client consumed them. Error fallbacks caused by an unavailable audit store may
themselves have no completion record; they must not be counted as complete coverage.

## Authenticated receipt

One event per POST, maximum 64 KiB including its envelope. HMAC-SHA256 covers the
exact body and the version, key ID, fresh attempt UUID and integer Unix timestamp:

```text
awasi-v1\n{key_id}\n{delivery_id}\n{timestamp}\n{exact body bytes}
```

Headers are `X-Awasi-Key-Id`, `X-Awasi-Delivery-Id`, `X-Awasi-Timestamp` and
`X-Awasi-Signature` (`v1=` plus lowercase hexadecimal MAC). Timestamps must be within
300 seconds; clocks need to agree. Duplicate auth headers, duplicate JSON keys,
unknown fields and invalid bounded event schemas are rejected. Signatures use a
dedicated connector secret, not CRM bearer credentials. No secret or request body
is included in error responses.

| Status | Meaning |
| --- | --- |
| 202 | Event and pending state committed to SQLite |
| 200 | Identical canonical event already stored |
| 401 / 403 | Invalid signature/key/time/attempt identity, or authenticated company/source mismatch |
| 409 | Conflicting event identity or source sequence |
| 413 / 415 / 422 | Excess size, unsupported content encoding, or invalid schema |
| 503 | Storage unavailable; no durable acknowledgement |

Deduplication is scoped by company, source, source-instance UUID and event UUID;
sequence is independently unique within that source instance. Retries and key
rotation cannot create duplicate pending work. `/health` checks inbox readability,
not downstream processing or delivery completeness.

## Delivery, outages and replay

The CRM request never waits for the webhook. The sender claims one committed
event in a short transaction, sends outside the transaction, then records the result.
Leases last 60 seconds and are fenced by a random claim ID. A restarted sender
recovers expired claims without resetting attempt counts or changing event identity.
Each HTTP attempt gets a new signature timestamp and delivery ID.

Only 200/202 are successful. Connection errors, timeouts, 429 and 5xx retry with
exponential backoff and jitter. `Retry-After` is honored up to a 300-second bound.
HTTP timeout is five seconds; five attempts are allowed. Other statuses (including
redirects, auth failures, schema errors and conflicts) become `dead_letter` immediately.
Exhausted retries retain their source event and outbox row for operator replay.
The worker drains due events continuously and polls every 250 ms when idle or
after a storage exception. This is not a latency/throughput guarantee.

Delivery attempts use a separate local `delivery_audit` (latest 1,000 entries) and
never generate exported activity recursively. Queue state, attempts, next retry,
last safe status and error code are in `activity_outbox`. Inspect these using a
trusted local SQLite connection, or use the current Activity health API and dashboard
for sampled queue/capacity summaries.

After fixing the destination/key/storage problem, a CRM administrator may POST
`{}` to `/activity-deliveries/{event_sequence}/replay` using the existing CRM admin
bearer credential. Only dead letters can be replayed (404 missing, 409 other state,
403 non-admin). Replay resets attempts and makes the retained event pending;
it does not alter event identity or immediately guarantee receipt. The same
transaction audits the authenticated actor and target sequence. Successful replay
operations are operational evidence; denied replay attempts are security evidence.

For key rotation, first configure the receiver with the new
`CRM_WEBHOOK_KEY_ID`/`CRM_WEBHOOK_SECRET` and old
`CRM_WEBHOOK_PREVIOUS_KEY_ID`/`CRM_WEBHOOK_PREVIOUS_SECRET`, using distinct IDs.
Recreate the receiver, then recreate CRM with the new current key. Keep the previous
pair through the overlap, then remove it and recreate the receiver. The receiver
rejects old-key attempts after removal; already committed events remain deduplicated.

## Capacity and stopping

Each CRM event stream retains at most 10,000 rows by default. Delivered events
still count. There is **no automatic pruning** in these phases; this is a controlled
local demo, not an unattended production retention policy. Full required local
storage prevents further audited work with safe 503 responses. Four stages use
more capacity than the earlier operation-only implementation. The receiver inbox
also retains events indefinitely; disk monitoring and evidence lifecycle work remain
necessary before production use.

Set `CRM_WEBHOOK_ENABLED=0` and recreate CRM to stop delivery while retaining
capture. Set both flags to `0` to stop new capture as well. Existing events, inbox,
queue, credentials and grants remain. Stop the receiver with
`docker compose --profile activity stop awasi-ingest`. Do not use `down -v` as a
recovery action: it deletes retained data. The current phase-5 worker repairs gaps
and tracks separate received/processed checkpoints; see the recovery guide.

## Verification

Behavioral implementation followed failing focused tests before changes. Tests use
temporary real SQLite files, real local HTTP servers, controlled disconnects and
injected clocks. They cover request correlation and actor isolation, error paths,
capacity and response-check failures, duplicate/conflict races, key rotation,
storage failure, restarts, lost acknowledgements, receiver downtime, slow delivery,
redirect refusal, permanent errors, retry bounds, leases and authorized replay.

```sh
.venv/bin/python -m pytest -q
make test-integration
docker compose --profile activity config --quiet
```

The graph integration suite uses isolated `test-*` namespaces. No deployment or
live CRM reset is performed by these tests. No new detector or AI check is enabled.

Final verification: `make test-integration` passed **394 tests**, including the
real Neo4j checks and HTTP timeout regression. The normal suite previously passed
377 tests with 16 Neo4j skips, before that final timeout regression was added;
the added regression also passed independently. Compose validation and
`git diff --check` passed. The existing Starlette/httpx deprecation warning remains.

Instrumentation measurement on this development machine (Apple M2, arm64,
macOS 26.5.2, Python 3.11.6): 20 warmup calls followed by 200 sequential
`GET /customers` calls per mode, six records / 959 serialized bytes, concurrency
one, FastAPI TestClient, real temporary SQLite and sender disabled. Capture off
measured 1.47 ms median / 2.16 ms p95; capture on measured 5.46 ms median /
15.18 ms p95. This single local measurement isolates audit overhead; it is not
a network, concurrent-load or production latency guarantee. Current Docker receipt
measurements, the sender drainage fix and the larger-burst limit are recorded in
[phase-6 acceptance evidence](activity-processing.md#reproducible-receipt-latency-qualification).
