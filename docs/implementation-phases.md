# Awasi implementation phases

Status: phases 0–6 implemented; phases 7 onward remain proposed. Phase 0–1's
[activity contract](crm-activity-contract.md) and the
[phase 2–4 delivery guide](activity-delivery.md) and
[phase 5–6 recovery/visibility guide](activity-processing.md) record implemented scope and checks.
This is the authoritative execution breakdown for the
[architecture plan](autonomous-security-plan.md); it replaces that plan's earlier
0/1a/1b/1c phase numbering.

## How to select work

Select one phase per implementation request. A phase is an acceptance boundary,
not a requirement to fit everything into one commit. Use small changes inside it:
failing test, smallest passing implementation, then regression review. Stop before
starting the next phase unless it was also selected. If a missing prerequisite is
discovered, report the dependency and amend scope explicitly rather than quietly
pulling the later phase into the current work.

The first product milestone is **phases 0–6: complete CRM activity arrives in Awasi
and is visible with delivery health**. Phases 7–13 add policies in separate families.
Phases 14–17 add investigation and one controlled remediation capability. Phases
18–28 are individually selectable extensions or deployment requirements, not one
large final implementation task.

Do not assume numbering makes every phase depend on every earlier phase. Dependencies
below are the required ones. AI investigation does not require email, OAuth, or MCP;
an owned-CRM deployment does not need every optional integration.

## Completion rules for every implementation phase

- Run the existing relevant baseline before editing; identify unrelated failures.
  Each behavioral change starts with a focused failing test for the intended behavior.
- Prove state changes, refusals, and externally visible outcomes. Use temporary real
  SQLite databases for persistence; real local HTTP servers for transport work; real
  Neo4j for graph boundaries. Mocks are useful for controlled errors, not the only
  evidence that a service boundary works.
- Use injected clocks, explicit worker `run_once` operations, barriers, and controlled
  transport faults. Avoid minute-long waits, arbitrary sleeps, external CRM accounts,
  or a paid model in the ordinary unit suite. Keep a small real network/restart suite
  alongside deterministic unit tests.
- Preserve credentials, revoked grants, old audit consumers, existing finding IDs,
  and existing data across migrations. Use isolated fixtures; never reset running
  demo data to manufacture test cases. Migration tests must exercise existing schemas,
  not only empty databases.
- Each phase includes its own failure behavior, operator-visible errors, and safe
  configuration defaults. A feature is not complete merely because its happy path
  works. Avoid introducing enabled behavior that relies on an unimplemented phase.
- Disabling a feature must not delete its evidence or revive credentials/grants.
  Record how to stop its worker/rollout. Prefer forward-compatible migrations; do
  not promise that an older executable can read a newer schema without testing it.
- Run `.venv/bin/python -m pytest -q` at phase completion. If code crosses graph
  boundaries, also run `make test-integration` with services available. Run
  `make test-ui` for UI changes. Report failures and skips precisely; a skipped
  required integration check means the acceptance gate is not fully verified.
- Performance targets require benchmarks with declared hardware, payloads and
  concurrency. Unit-test success is not proof of throughput or latency. Repeat broad
  tests only after relevant changes or unresolved failures justify it.

## Environment: phases 0–6

### Phase 0 — Lock the activity contract and acceptance fixtures

**Depends on:** none.

**Scope:** inventory existing routes/store/CLI operations and test coverage. Define
the four event stages, trusted fields, redaction, sequence/epoch and request IDs,
signature encoding, response metrics, exported versus operational events, and HTTP
error contracts. Record retention/backpressure defaults and a threat/coverage matrix.
Prepare representative redacted example events and a concrete test checklist.

**Exclude:** schema migrations, new routes, runtime validators, policy decisions,
workers, dashboard changes, and new infrastructure.

**Done when:** every existing operation maps to expected evidence and there are
unambiguous examples for success, denial, validation error, server failure, and
interrupted response. Inspect/run the current baseline and document any skips.
This is a contract/documentation phase; do not invent unit tests for prose. Runtime
schema validation receives failing tests in the implementation phase that adds it.

### Phase 1 — Durable CRM operation events and outgoing queue

**Depends on:** 0.

**Scope:** additive event/outbox migrations; event IDs and source ordering; emit
`operation.completed` from existing store operations with trusted actor/outcome.
Keep supported CLI/store calls covered and preserve the existing `/audit` contract.
Commit business changes and event/outbox rows together; rollback rejected partial
writes while retaining their denial evidence. Define independent operational audit
records. Delivery is disabled until phase 4.

**Exclude:** HTTP lifecycle middleware, webhook network calls, Awasi changes, new policies.

**Gate:** actual SQLite tests for reads/writes/denials, atomic commit/rollback,
old-schema migration, audit redaction, source ordering, and restart persistence.
Existing CRM authorization/revocation tests still pass. Enable capture only in
controlled tests/demo until delivery and queue-capacity handling are in place.

### Phase 2 — Complete API request and response capture

**Depends on:** 1.

**Scope:** generate one trusted request ID and join store events to it. Capture
`request.received`, `response.prepared`, and `response.completed`, including 401,
403, 404, 405, 409, 422, 500, public routes, and interrupted responses. Distinguish
actual record counts, serialized/emitted bytes, intended/observed status, and committed
operations. Install a bounded response-check hook reporting `not_run` initially.
Redact before persistence. Requests with no store operation still have API evidence.

**Exclude:** secret detectors, new access policies, content blocking, exports/streaming
features, and any wait for Awasi before responding.

**Gate:** API/SQLite tests for the route matrix, no raw credentials or rejected-body
echoes in events, one request across lifecycle stages, and unchanged CRM responses.
Use a controlled ASGI send failure/disconnect and real HTTP success/error cases;
never infer successful client receipt from a prepared response or invent a completion
event after a crash. Measure instrumentation overhead separately from later policies.

### Phase 3 — Authenticated Awasi webhook receiver

**Depends on:** 0; phase 2 fixtures are recommended for the integration examples.

**Scope:** `POST /webhooks/crm/activity`, size/schema limits, HMAC verification,
timestamp tolerance, configured key-to-company/source mapping, key rotation, and
atomic durable inbox/pending-work records. Identical duplicates are harmless;
conflicting payloads for the same identity are integrity failures. Acknowledge only
after storage. Keep admission independent of graph/model availability; limit any
startup refactor to enabling that boundary and preserve existing graph-route behavior.

**Exclude:** CRM sender, policy evaluation, remediation, graph writes, activity UI.

**Gate:** real HTTP/SQLite tests for valid/tampered payloads, wrong tenant/source/key,
expired timestamp, rotation, size limits, duplicate/conflict, storage failure and
restart. A durable acknowledgement survives restart even when downstream processing
is unavailable. The receiver can be tested with a signed fixture without phase 4.

### Phase 4 — CRM webhook sender and retry delivery

**Depends on:** 1–3.

**Scope:** a separately scheduled sender with durable claims, a fixed receiver URL,
per-attempt signatures, timeout/backoff/jitter handling, bounded retries, dead-letter
retention, and authorized operator replay. Add minimal setup/Compose wiring for
separate secrets and an explicit local-network exception to production HTTPS.
No network call runs inside a CRM operation transaction. Delivery logs must not
create more exported security events.

**Exclude:** cursor recovery, policy processing, automatic remediation, arbitrary
user-controlled webhook destinations, a dashboard redesign.

**Gate:** real CRM-to-Awasi HTTP tests prove automatic delivery; duplicate-safe retry
after lost acknowledgement; sender restart; 429/5xx/timeouts and permanent errors;
rotation; no redirects; durable exhausted retries; and no recursive export. Awasi
downtime does not block business requests while local audit/queue capacity is healthy.
Include queue-limit failure behavior now, not only in production hardening.

### Phase 5 — Gap recovery and resumable event processing

**Depends on:** 4.

**Scope:** scoped CRM `events:read` recovery API, source high-water marks, ascending
cursor pagination, an independent Awasi worker, contiguous ingestion checkpoints,
out-of-order tracking, durable processing state, source-reset detection, and recovery
after outages. Push and recovery use the same deduplication boundary. Maintain
separate received and processed progress. The worker normalizes evidence; it need
not yet evaluate new rules or update graph topology.

**Exclude:** new detections, AI, data-changing remediation, business CRM features.

**Gate:** local HTTP/SQLite tests for a missing middle event and missing final event,
duplicates across push/pull, worker restart, processing crash, fixed-watermark paging,
epoch changes, expired source history and unauthorized recovery access. A gap cannot
silently become complete coverage. Existing snapshot reconciliation remains intact.

### Phase 6 — Activity visibility and environment acceptance

**Depends on:** 5.

**Scope:** company-scoped paginated `GET /activity`; a focused dashboard activity
view with request/response stages and safe filters; delivery/processing lag, gaps,
queue health and dead letters. Expose unsupported/not-run checks honestly. Measure
the proposed receipt latency target under a declared test load. Document startup,
replay, credential rotation, and degraded monitoring behavior.

**Exclude:** policy editor, alert automation, approval UX, AI features, new scanners.

**Gate:** API authorization/pagination tests, frontend model tests, browser/API smoke
verification, and one end-to-end CRM call that appears in Awasi without manual sync.
Repeat with a denial and an Awasi outage/recovery. Confirm four lifecycle events do
not display as four separate business requests. Verify source-to-receipt latency
and identify any missed target explicitly. The 2026-10-31
[acceptance evidence](activity-processing.md#acceptance-evidence) records a qualified
20-request burst and the missed conservative bound at a 100-request burst.

**First usable milestone:** CRM activity is reliably observable. This does not yet
mean all policy checks or automatic remedies are enabled.

## Policies: phases 7–13

### Phase 7 — Multiple-policy registry and finding lifecycle

**Depends on:** 5; 6 for final dashboard coverage verification.

**Scope:** versioned registry, rule-specific capabilities/modes, generalized finding
storage and evidence, isolated rule watermarks, and distinct posture versus activity
incident lifecycles. Migrate the existing inactive-owner rule without changing its
meaning, identity, or history. Render rule-specific titles and evidence in the
existing dashboard. Synthetic candidates can exercise new lifecycle shapes in tests.

**Exclude:** new blocking behavior, a general rule-authoring UI, new detectors.

**Gate:** SQLite migration and API tests, existing real Neo4j lifecycle integration,
cross-rule reconciliation isolation, stale/missing evidence handling, and frontend
rendering. An older activity incident must not become unknown solely because it is
15 minutes old. An unimplemented policy cannot appear enabled and healthy.

### Phase 8 — Inline enforcement boundary and resource permissions

**Depends on:** 2 and 7.

**Scope:** deterministic evaluator/decision contracts, explicit rule precedence,
canonical action/resource mapping, and request/pre-commit/pre-release hooks. Correct
scope checks to match the actual resource/app; retain existing admin and field-write
requirements. Persist decisions linked to activity. Start with existing supported
permissions as the first real rules.

**Exclude:** owner projection, new counters, secret scans, row ownership, remote
decision calls, or automatic revocation.

**Gate:** failing cross-resource access regression first; then real CRM tests for
allow/deny precedence, no rejected side effects or data release, correct error
mapping, durable denial records, and read-only observer restrictions. Test both
observe/enforce modes without letting observe mode disable base authorization.

### Phase 9 — Fresh owner status and inactive-owner enforcement

**Depends on:** 8 and existing directory observation.

**Scope:** minimal versioned identity projection, dedicated authenticated publisher,
source timestamps/expiry and replay checks, inactive-owner agent rule and inactive
employee checks. Preserve observation-only mode explicitly. Publish validated
directory evidence without querying Neo4j on each CRM request.

**Exclude:** disabling/rotating credentials, row/team permissions, automated revocation.

**Gate:** directory -> projection -> CRM integration; active/inactive status, startup
without context, stale/incomplete/replayed/wrong-company context, restart without
freshness reset, independent agent/service semantics, and later request denial.
Measure propagation lag; do not promise instantaneous directory offboarding.

### Phase 10 — Principal and credential lifecycle enforcement

**Depends on:** 8; use 9 when combining employment and credential status.

**Scope:** explicit active/disabled principal and revoked/expired credential state,
an intentionally narrow authorized administrative transition, and enforcement on
all protected routes, including role-based admin operations. Add lifecycle evidence
and restart-safe migrations. Support existing static credentials; do not introduce
an OAuth/session system in this phase.

**Exclude:** automatic disabling, bulk actions, shared-key rotation, production SSO.

**Gate:** current grant revocation behavior remains intact; principal disable and
credential expiry prevent admin as well as customer access; unauthorized transitions
fail; updates use concurrency/version checks; restart cannot revive access. Exercise
the operator recovery procedure in an isolated fixture, not a blanket bypass.

### Phase 11 — Record/request budgets and bulk-result controls

**Depends on:** 8 and phase 5 event processing.

**Scope:** one shared transactional rolling-budget mechanism; configured request and
returned-record limits; an actual-result bulk threshold on existing list/get routes;
record enumeration evidence; explicit attempted/prepared/released stages. Use the
same mechanism for related policies rather than independent racing counters.

**Exclude:** new export/pagination endpoints, Redis, AI anomaly baselines.

**Gate:** 49/50 and 100/101 boundaries, exact window expiry, simultaneous requests,
shared budget across list/get, token/principal scoping, repeated disclosures, restart,
clock anomalies, and failed/disconnected delivery accounting. Prove no bytes are
released after a blocking decision. Use fixtures larger than the six demo records.

### Phase 12 — Repeated authentication and permission failures

**Depends on:** 5, 7, 8, and the rolling-window mechanism in 11.

**Scope:** classify failed authentication versus authorization versus ordinary tool
errors; create bounded incidents and temporary cooldown/throttle behavior using
trusted actor/source keys. Process events immediately without waiting for AI.

**Exclude:** permanent account disable, country detection, lockouts keyed only by a
caller-supplied username, generic treatment of every 4xx/5xx as an attack.

**Gate:** threshold/cooldown boundaries, distributed repeated attempts for the same
authenticated actor, event replay without double counting, no account lockout by
spoofed username, and 409/upstream failures not counted as bad credentials.

### Phase 13 — Bounded content scanning before writes and release

**Depends on:** 8; phases 2 and 7 provide response evidence and findings.

**Scope:** a bounded scanner for current CRM notes and bounded serialized responses,
using a small explicit detector list. High-confidence secret detectors can block
under enabled policy; PII is initially observation-only unless an explicit contextual
rule authorizes blocking. Define normalization/size/time limits and safe redacted
evidence. Reuse the scanner at both relevant boundaries.

**Exclude:** OCR, arbitrary attachments, all possible secret formats, prompt/MCP
scanning without an adapter, provider calls to validate secrets, email sending.

**Gate:** benign fixtures and known test values, real-looking synthetic positives,
boundary lengths, pathological pattern inputs, timeout/oversize as unknown, no secret
in logs/errors, and proof that blocked content is neither written nor returned.
Measure overhead and review false positives before enabling heuristic enforcement.

**Policy milestone:** phases 7–13 cover today's CRM surfaces. Each family is enabled
only after its own gate. Full principal lifecycle (10), rate controls (11), and scans
(13) can be selected independently after their stated prerequisites.

## Investigation and remediation: phases 14–17

### Phase 14 — Durable 15-minute review jobs, without a model

**Depends on:** 5 and 7. Does not require all blocking policies.

**Scope:** separate scheduler/job kind, interval claims, catch-up, ingestion
watermarks, late arrivals, overlapping context, bounded historical aggregates and
complete chunked review inputs. Persist coverage and retryable review state. Use a
deterministic test analyzer to prove orchestration without model costs.

**Exclude:** LLM calls, autonomous conclusions, remediation, a new distributed queue.

**Gate:** injected-clock interval boundaries, downtime catch-up, duplicate scheduler
runs, late evidence, crash/restart, dataset larger than one chunk, and independence
from observation/delivery workers. Test analyzer output is never shown as live AI.

### Phase 15 — Evidence-backed AI investigation

**Depends on:** 14.

**Scope:** one model adapter, bounded redacted context, a small read-only tool set,
structured output validation and evidence-ID checks, model/prompt provenance, incident
deduplication and cost limits. Establish a labeled benign/attack evaluation set and
quality thresholds before enabling it for live reviews.

**Exclude:** policy editing, arbitrary SQL/shell/network access, action credentials,
automatic remedies, requiring live model calls in ordinary unit tests.

**Gate:** deterministic adapter/tool/schema tests plus a separately reported live
model evaluation on held-out fixtures. Test malicious logs, invented citations,
cross-company evidence, partial results, timeout, budget exhaustion and quiet periods.
Report measured quality/cost; do not claim AI detection quality from mocked outputs.

### Phase 16 — One approved remediation: revoke one agent grant

**Depends on:** 7 and 9; phase 15 is optional because a deterministic finding can
start the same workflow. Phase 25 is required before production human approvals.

**Scope:** only grant revocation for the inactive-owner finding. Add a restricted
executor credential/action route, proposal/approval record with target/version/expiry,
durable action journal, idempotency, fresh precondition checks, and authoritative
verification. Use the existing local operator identity only for the local demo and
label that limitation. Retain the existing manual CRM administrative workflow.

**Exclude:** general workflow builder, deleting data, disabling humans, rotating
shared credentials, multiple playbooks, automatic approval, Slack/email integration.

**Gate:** real HTTP CRM action and database state; no mutation by observer/model;
stale/wrong-target approval; repeated execution; crash after action but before result;
failed verification; and a controlled agent request denied after revocation. Use
isolated test credentials for behavioral verification, never expand the executor's
rights to impersonate production users merely for a probe.

### Phase 17 — Preauthorized execution of that same playbook

**Depends on:** 16.

**Scope:** explicit configured authorization for that single deterministic playbook,
fresh evidence, excluded protected accounts, blast-radius/action budgets, pause
switch, and fully audited execution. Reuse phase 16's executor and verifier.

**Exclude:** automatic action on AI-only suspicion, additional remedy types, broad
administrator access, automatic restoration of revoked grants.

**Gate:** preauthorization scope/expiry, fresh-condition recheck, concurrent duplicate
findings, protected target, action limit, stop switch, downstream failure and replay.
Observe/propose first, then enable only the narrowly authorized playbook.

## Optional application surfaces: phases 18–24

Each row is a separate selectable phase, implemented only if that capability is
needed. The referenced foundational phases remain prerequisites. For external
providers, verify that provider's current contracts before implementation and add
a controlled integration/sandbox test; mocks alone cannot establish coverage.

| Phase | Scope and dependencies | Exclusions and acceptance gate |
| --- | --- | --- |
| 18. Checked export and pagination | After 8/11/13: one CRM export workflow, bounded authorized paging, artifact checks and download-time permission revalidation | No external CRM migration. Test all pages, cross-page budgets, revoked access between creation/download, expired download, and response limits. Split paging and export into reviewable changes within this capability. |
| 19. Record/team access | After 8/9: authoritative row ownership/team fields, migration and read/write filtering | No organization-wide role redesign. Test cross-team access, explicit delegation, reassignment/version races, and filtering before counts/pagination. Existing rows need an explicit migration policy. |
| 20. One email integration | After 8/13: draft/send checks and verified business-recipient registry for one adapter | No generic messaging platform. Test To/Cc/Bcc, normalized domains, changed drafts, permissions and secret scan immediately before send; verify actual adapter boundary in a controlled environment. |
| 21. Business webhook egress | After 8/13: one outbound CRM business-event adapter with destination/data policy | Distinct from CRM-to-Awasi activity delivery. Test exact destination rules, redirects, connection-time address validation, disallowed network targets, retries and changed payloads. |
| 22. Login geography | After 5/7/12: one IdP event adapter, trusted location context and narrowly defined geofence/travel detections | No invented login data from CRM bearer requests. Test human versus service identity, VPN/clock uncertainty, new-country versus travel distinction, and alert/step-up behavior supported by that IdP. |
| 23. OAuth scope governance | After 8/10 and an identified authorization provider: approved scope set and one consent/grant lifecycle adapter | No universal OAuth broker. Test expansion approval bound to client/principal/resource/scopes, stale consent, audience/issuer checks and provider revocation behavior. |
| 24. MCP tool enforcement | After 8/13 and 23 if its transport uses OAuth: one MCP server/agent integration with trusted tool manifest and argument/result policies | No host sandbox implementation hidden inside this phase. Test unlisted tools, changed arguments, delegated identities and alternate-route bypass; document host tools/egress outside the boundary. |

## Deployment requirements: phases 25–28

These are not prerequisites for the isolated local demo. Select the appropriate
ones before the associated production capability; do not label a deployment ready
for multiple companies or replicas while its prerequisites remain incomplete.

| Phase | Bounded scope | Dependencies and gate |
| --- | --- | --- |
| 25. Individual operator identity | One SSO identity provider, viewer/operator/approver roles, per-person action audit and approval identity | Existing API/approval contracts. Test denied role transitions, session expiry and approval provenance. No multi-tenant migration or identity-provider marketplace. Required before production human approvals. |
| 26. Tenant isolation | Explicit tenant configuration and isolation across events, signing keys, findings, graph queries, jobs and actions | 25 plus the enabled product surfaces. Complete a cross-tenant negative-test matrix and per-tenant migration/provisioning tests before a second production tenant. No horizontal scaling or new providers. |
| 27. Evidence lifecycle | Defined retention, restricted evidence access, protected secrets, archival/deletion jobs and independently controlled audit storage for the chosen deployment | 5/7 and production identity; include 26 if multi-tenant. Test expiry without silent cursor gaps, access restrictions, rotation and backup/restore of retained evidence. No distributed-worker rewrite. |
| 28. Capacity and recovery qualification | Benchmark the chosen deployment envelope; exercise outages, disk/queue exhaustion and restore; define measurable SLOs and runbooks | All enabled capabilities. A single-replica release can remain single-replica if it meets its declared envelope. If multiple replicas are required, create a separate reviewed storage/lease/counter migration plan before implementation; do not hide that migration in a load-test phase. |

Production qualification includes the transport/egress configuration already designed
in the relevant phases. This roadmap does not postpone basic authentication,
redaction, retry safety or atomicity until production.

## Suggested first selection

Phases **0–6 are complete**; implementation details are recorded in the
[activity contract](crm-activity-contract.md), [delivery guide](activity-delivery.md)
and [recovery/visibility guide](activity-processing.md).
The next selectable work is **phase 7**, the multiple-policy registry and finding
lifecycle. Review the activity evidence and local delivery measurements before
selecting additional policies. This status does not authorize later phases.

Good review checkpoints are: after 2 (complete CRM-side evidence), after 4 (live
delivery), after 6 (reliable visible environment), after each policy phase, and after
16 (one verified, explicitly approved remedy). No estimate of days is implied;
implementation evidence will determine whether a phase needs more than one change.
