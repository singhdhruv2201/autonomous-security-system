# Phases 5–6 implementation and acceptance status

Updated 2026-10-31. The original phase-5/6 implementation is already committed in
`22cc128` on top of `f966fdd`; the earlier handoff described the tree before that
commit. The local Docker environment is now upgraded and verified. This closeout
adds a reproducible latency benchmark, fixes per-event sender throttling and stale
dashboard wording, and records the tested load boundary. See the operator guide
[activity-processing.md](activity-processing.md) for commands and measured results.

## Implemented scope

From the [implementation phases](implementation-phases.md):

| Phase | Goal | Acceptance |
| --- | --- | --- |
| 5 — Recovery and event processing | Authorized cursor recovery, missing-event detection, durable processing checkpoints, deduplication across push/recovery | Missing and out-of-order events recover correctly. Restarts do not lose work or silently mark incomplete coverage complete. |
| 6 — Activity view and environment acceptance | Authenticated activity API, focused dashboard view, delivery/processing health | A CRM request appears in Awasi automatically. Denials and outage recovery work end to end. Delivery latency is measured. |

Design in one paragraph: push delivery (phases 2–4) stays as is. Awasi adds an
independent worker that polls a scoped CRM recovery feed every 5 seconds to find and
fill gaps, then normalizes inbox receipts into evidence with durable checkpoints.
Push and pull share one deduplication boundary (the inbox). The four lifecycle
events of a CRM request are grouped into one activity row. The dashboard shows gaps,
stale source checks, and unfinished processing explicitly instead of claiming
complete coverage.

## What was developed

### CRM side (`src/helio_crm/`)

- **Recovery credential and principal.** `credentials.py` adds `CRM_RECOVERY_TOKEN`;
  `make setup` appends it to an existing `.env` without rotating other secrets.
  `store.py` registers `service:activity-reader` with a single `events:read` grant on
  the new `crm-activity` resource, on fresh databases and on upgrade. Revoked grants
  are not revived.
- **Resource-scoped grant check.** `_perform` takes `grant_resource`, so `events:read`
  only counts when the grant is on `crm-activity`. Admins and other principals are
  denied.
- **Recovery feed** (`api.py`, `store.activity_feed`):
  - `GET /activity/status` returns the source instance ID, high watermark (from
    `sqlite_sequence`, so a fully pruned history still can't look empty), earliest
    retained sequence, capture state, queue counts, and capacity.
  - `GET /activity/events?after&through&source_instance_id&limit≤200` returns
    ascending pages with `next_after`/`has_more`. It returns 409 for a changed source,
    410 for expired history, and 422 for invalid cursors.
- **No recursive export.** `events:read` is an operational read (`activity.py`), and
  the new routes are classed as metadata (`lifecycle.py`). Recovery traffic is
  audited but isn't sent back into the delivery stream.
- **Inventory** includes the `crm-activity` resource when the reader exists, so the
  graph can observe the new grant.

### Awasi ingest side (`src/helio_security/`)

- **`activity_store.py` (new)** extends the phase-3 `Inbox` with additive tables:
  `activity_sources` (per company/source/instance received and processed cursors,
  watermark, and health snapshot), `activity_connectors` (current instance plus a
  persistent reset counter), `activity_worker_state` (connector and integrity
  errors), and `normalized_events`.
  - Contiguous checkpoints advance only through gap-free sequences. Out-of-order
    events are stored, but the cursors stay behind the gap.
  - `process_one` writes normalized evidence and advances the processed cursor in
    one `BEGIN IMMEDIATE` transaction. A crash leaves the work pending.
  - Existing phase-3 inboxes are migrated by deriving received cursors from rows that
    are already stored.
  - `health()` reports missing ranges, receipt and processing lag, the oldest pending
    age, staleness (>30 s), history expiry, resets, and integrity conflicts. It only
    reports `coverage_complete` when none of these apply.
  - `activity()` groups stages by `(source, source_instance_id, request_id)`, with a
    snapshot-stable `before` cursor and `actor_id`/`outcome` filters.
- **`activity_worker.py` (new)** contains `RecoveryClient` (5 s timeout, 2 MiB body
  cap, no redirects or proxies, plain HTTP only for local hosts) and `ActivityWorker`.
  The worker validates status and pages strictly and holds a fixed watermark per pass
  (at most 10 pages of 100). Any page anomaly is recorded as a failure without moving
  the checkpoint. It exposes `run_once()` for tests and is enabled by
  `AWASI_ACTIVITY_WORKER_ENABLED`.
- **`webhooks.py`** now creates an `ActivityStore` and calls `accepted`/`conflicted`
  hooks inside the receipt transaction. An identical duplicate is accepted, and a
  payload that differs for the same identity is recorded as an integrity conflict.
  `GET /activity` and `GET /activity/health` require the viewer or operator token,
  and that token must differ from webhook secrets. The worker starts and stops with
  the app lifespan.
- **`activity_client.py` (new)** adds the shared bounded filter contract
  (`activity_query`) and a read-only client that rejects responses for the wrong
  company.
- **`api.py` (security API)** proxies `/activity` and `/activity/health` when
  `AWASI_ACTIVITY_URL` is set. Otherwise it returns `enabled: false`. Overview now
  reports `activity_ingestion` truthfully.

### Dashboard (`src/helio_dashboard/`)

- `/api/activity` and `/api/activity/health` are proxied server-side, so the browser
  never sees credentials.
- **`static/activity.mjs` (new)** and the `app.mjs` changes add an **Activity** tab
  with source health metrics, one row per request, expandable lifecycle evidence,
  scanners shown as "Not run", outcome and actor filters, and "Load older".
  It refreshes every 5 s, but pauses while a row is expanded, a filter has focus, or
  older pages are loaded. Graph refreshes no longer repaint over the Activity tab.
  Styles were added to `styles.css`.

### Config and docs

- `compose.yaml` passes `CRM_RECOVERY_TOKEN`, the worker flag, `CRM_URL`, and the
  read tokens to `awasi-ingest`, and passes `AWASI_ACTIVITY_URL` to `security-api`.
- New [activity-processing.md](activity-processing.md). README, `crm.md`,
  `dashboard.md`, `activity-delivery.md`, `autonomous-security-plan.md`, and
  `implementation-phases.md` are updated to say phases 0–6 are implemented.

### Tests added

| File | Covers |
| --- | --- |
| `tests/test_activity_recovery.py` | Feed scoping, no recursive export, revocation and resource scope across restart, fixed-watermark paging, epoch change, expired history, invalid cursors, setup token append, inventory validity |
| `tests/test_activity_processing.py` | Out-of-order checkpoints, atomic processing with restart, phase-3 inbox migration, missing middle/final events over real HTTP, reset/stale/expired visibility, grouping/pagination/filters, connector failure reason, invalid pages not advancing checkpoints |
| `tests/test_activity_api.py` | Read auth, bounded filters, security API proxy company check, dashboard filter forwarding |
| `tests/test_activity_end_to_end.py` | A live CRM call and a denial reach the dashboard API without a manual sync, and recovery after an Awasi outage |
| `tests/test_activity_graph.py` (Neo4j) | The recovery grant is observed on its own `crm-activity` resource and creates no false findings |
| `tests/test_activity_benchmark.py` | Percentiles, complete evidence, missing/duplicate/invalid samples, bounded load, CLI outcomes and real HTTP/SQLite receipt probes |
| `tests/frontend/dashboard-activity.test.mjs` | Overview links to activity without claiming request capture is unimplemented |
| `tests/frontend/activity*.test.mjs` | Coverage state is never falsely complete, four stages appear as one row, escaping, outage display, pagination, and refresh preserving UI state |

## Verification status (2026-10-31)

| Gate | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q` | 423 passed, 17 skipped (Neo4j-only tests) |
| `make test-integration` | 440 passed, including real Neo4j, SQLite and HTTP |
| `make test-ui` | 15 passed |
| `docker compose --profile activity config --quiet` | Passed; recovery token and all activity flags configured |
| Live Docker/browser smoke | Allowed request and denial recovered as two rows / eight unique events after an ingest outage; filters, expanded evidence and older pages worked |
| Declared 20-request burst, concurrency 4 | Receipt p95 631.37 ms; durable acknowledgement p95 637.82 ms: target met |
| Larger 100-request burst, concurrency 4 | Receipt p95 1,896.54 ms; durable acknowledgement p95 3,090.10 ms: conservative bound missed |

The Starlette/httpx deprecation warning remains unrelated to this feature. Latency
results are real Docker measurements, not assertions inferred from unit-test timing.
The benchmark returns failure for missed targets and invalid/incomplete samples.

## Closeout checklist

1. **Environment upgraded.** `make setup` preserved credentials and added the missing
   recovery token. Capture, signed delivery, processing and the Activity proxy are
   enabled in ignored local configuration. All six services are healthy. Consistent
   CRM/inbox backups passed SQLite integrity checks before rebuilding this closeout;
   retained data, grants, source identity and Neo4j state were preserved.
2. **Latency measurement implemented and recorded.** `make benchmark-activity` runs
   bounded concurrent CRM reads and measures all source events against persisted
   inbox receipts and durable webhook acknowledgements. JSON reports contain the
   load, hardware/runtime, image identity, payload size, percentile method and result.
   The baseline missed the target because the sender paused after every event. A
   failing regression test reproduced this; the sender now drains due events before
   waiting. The exact before/after and larger-burst limitation are recorded in the
   [Acceptance evidence](activity-processing.md#acceptance-evidence).
3. **Browser smoke repeated on the deployment.** The dashboard showed unavailable
   activity with unknown coverage during the ingestion outage. CRM continued serving
   an allowed call and a denial; after restart the dashboard recovered both exactly
   once with four stages each. Denial filtering, expandable evidence, 25-to-50-row
   pagination and the overview's new Activity link were verified.
4. **Final review completed.** Recovery/authentication, durable checkpoints,
   deduplication, honest health states and sender retry/lease behavior were reviewed
   against the phase gates. Focused failing tests preceded behavioral changes; unit,
   integration, frontend and configuration checks passed. The already committed
   phase-5/6 implementation is retained; follow-up commits contain the closeout code,
   regression tests and current acceptance evidence rather than rewriting history.

No phase-7 policy registry, AI investigation, scanners, remediation or retention
features were added. The 100-request burst is **not** qualified for the conservative
two-second bound; the passing measurement applies only to the declared smaller load.

## Retained limitations and later work

- `process_one` processes the inbox strictly in order. If a stored receipt ever fails
  re-validation, it will be retried forever and block every receipt behind it.
  Receipts are validated on admission, so this should be rare, but no dead-letter
  path exists for normalization.
- `ActivityStore.failure()` uses `time.time()` instead of the worker's injected clock.
- Source resets and identity conflicts have no repair or acknowledge workflow by
  design. Coverage stays incomplete until someone investigates.
- The 10,000-event cap per CRM stream still applies, and there is no retention or
  pruning yet (phase 27).
- Next selectable phase after this: **7** (multiple-policy registry and finding
  lifecycle).
