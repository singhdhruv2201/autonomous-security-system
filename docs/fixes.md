# Deferred fixes

Recorded after the Phase 4 follow-up review on 2026-11-11.

These issues are open but do not block the planned Phase 5 public-app and
unapproved-destination demo scenarios. Continue Phase 5 without expanding the
simulator hardening scope. Revisit these items before relying on broader
destination support or accurate internal-view attribution.

For each behavioral fix, follow `AGENTS.md`: reproduce the issue with a failing
test, implement the smallest correction, and run relevant unit and integration
tests. Mark an item complete only after recording the validation performed.

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
results, and commit reference if available. No implementation changes were made
when this backlog was created.
