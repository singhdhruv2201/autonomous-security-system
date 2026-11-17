# Known limitations and follow-up work

The prototype demonstrates bounded workflows, not production security coverage.
For the broader product roadmap, see [future scope](../README.md#future-scope-not-yet-built).

## Current boundaries

- Real SaaS connectors, multi-tenancy, SSO, inline prevention, and the proposed
  periodic AI anomaly review are not implemented in v1.
- The main stack polls source metadata. The showcase uses a per-session in-memory
  graph and temporary data, not Neo4j or durable service hosting.
- A single worker and SQLite checkpoints are suitable for the demo, not evidence
  of high availability or production throughput.
- Model text may be unavailable or require a bounded correction. Validated copy
  and investigation state are distinct from verified action results.
- Live Slack/model checks and automated fake-provider checks establish different
  things. A previous full-suite run under concurrent local load failed a dashboard
  graph/inventory availability assertion; startup and load reliability need further
  verification. This is not a claim of a consistently green full integration suite.

## Simulator review backlog

Originally recorded November 11, 2026; checked against the current source during
repository preparation on November 16, 2026. The three behaviors below remain
present. They do not block the narrow demo scenarios, but should be addressed
before extending destination support or relying on internal-view attribution.

Follow [AGENTS.md](../AGENTS.md): reproduce each behavior with a failing test, make
the smallest fix, and record relevant unit and integration results.

## FIX-01: Remove the test-specific destination-host exception

**Status:** Deferred / open.

**Location:** `src/helio_workbench/api.py`,
`validate_and_normalize_destination_url()`.

**Current behavior:** A declared destination host of `receiver` is accepted when
the URL points to `127.0.0.1` or `localhost`, despite the hosts not matching.
For example, `expected_host="receiver"` with
`http://127.0.0.1:8005/collect` passes validation. The exception exists in runtime
code rather than in test fixtures. A saved destination using this exception can
retain a label that differs from its actual receiver.

**Impact:** This weakens the host-identity invariant. The planned demo uses
`enrich-leads` and `partner-api`, so the exception is not needed for those flows.
Do not depend on `receiver` as an approved destination while this remains open.

**Proposed fix:** Remove the exception. Update local HTTP test fixtures to use
the hostname from their actual server URL. Keep validation consistent for
destination creation, updates, and pre-send checks of existing records.

**Acceptance checks:**

- [ ] Declaring `receiver` for a localhost URL is rejected.
- [ ] Matching localhost/IP destinations still work in local HTTP tests.
- [ ] A previously saved mismatched destination is blocked before sending data.
- [ ] The `enrich-leads` and `partner-api` demo flows continue to work.

## FIX-02: Attribute internal app views to the authenticated caller

**Status:** Deferred / open.

**Location:** `src/helio_workbench/api.py`, `user_view()`.

**Current behavior:** `GET /apps/{slug}` records `app.view` with
`actor=app_obj["owner_employee_id"]`. A user viewing another employee's app is
therefore logged as the app owner. Destination creation already uses
`resolve_caller_identity()`; internal app views do not yet use it.

**Impact:** Internal-view audit attribution is inaccurate. Anonymous public views
still use `anonymous`, so this does not block Phase 5's public-exposure signal.

**Proposed fix:** Use the authenticated caller identity for internal-view events,
reusing the existing resolver. Preserve the generic `user` identity for an
authenticated but unmapped caller; do not infer identity from asset ownership.

**Acceptance checks:**

- [ ] A mapped user viewing another employee's app is logged as the caller.
- [ ] An unmapped authenticated caller is logged as `user`, not as the owner.
- [ ] Public views remain attributed to `anonymous`.
- [ ] Internal views do not increment `anonymous_views_1h`.
- [ ] No credentials appear in event records.

## FIX-03: Correct IPv6 destination validation

**Status:** Deferred / open.

**Location:** `src/helio_workbench/api.py`, `is_valid_hostname()` and
`validate_and_normalize_destination_url()`.

**Current behavior:** A valid URL such as `http://[::1]:8005/collect` is rejected.
`urlsplit().hostname` returns the IPv6 address without brackets, while the
validator expects brackets when checking IPv6. Splitting an expected host at
the first colon also cannot correctly handle IPv6 literals.

**Impact:** IPv6 destinations are unsupported despite the validator's docstring.
The planned demo uses Docker service names and does not require IPv6.

**Proposed fix:** Validate parsed IP literals with `ipaddress`, define a canonical
stored host representation, and compare normalized addresses without splitting
IPv6 at the first colon. Preserve brackets where required in URL authorities.

**Acceptance checks:**

- [ ] Valid IPv6 URLs and ports are accepted; malformed literals are rejected.
- [ ] Equivalent IPv6 spellings compare consistently; different addresses fail
  host matching.
- [ ] Invalid ports and embedded credentials remain rejected.
- [ ] IPv4 and Docker service-name validation remains compatible.
- [ ] Exercise a real local IPv6 HTTP receiver where supported; report an
  explicit skip when the test environment cannot bind IPv6.

## Completion record

When closing an item, record its ID, date, changed behavior, test commands and
results, and commit reference if available. This repository-preparation update documents the remaining behavior; it does not
implement these fixes.
