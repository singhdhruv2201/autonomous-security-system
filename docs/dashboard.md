# Awasi dashboard

Open [localhost:3000](http://localhost:3000) after following the
[Docker startup guide](getting-started.md#3-run-the-full-docker-stack-optional).
Start the live case worker separately to avoid unintended model calls or messages.
No token needs to be pasted into the browser. The dashboard runs as a separate
service on the platform network, outside the simulated customer environment.

## Views

- **Dashboard:** current policy health, inventory counts, open findings, source
  freshness, and company monitoring. The green check means the configured check
  has no open findings with current source coverage; it is not a security certification.
- **Company graph:** live Neo4j inventory and relationships via the security API.
  Select a node for details, filter entity kinds, drag to pan, and use zoom controls.
  Connections stay inside the company; destinations and the public audience occupy
  the **OUTSIDE** lane. **Show in graph** highlights only relationships supported
  by the selected finding and the current graph. Missing or changed relationships
  are disclosed, never reconstructed as if they still exist.
- **Agents:** observed company agents, ownership, grants, and related flags.
  The deterministic observation worker is shown separately. Paused workbench agents
  and disabled CRM principals show their observed access state.
- **Apps:** application owners, resources, associated findings, and connection health.
- **Policies:** the three enabled detection rules, the six ordered decision rules,
  and all five playbooks with their reversible flags, supplied by the security API.
  This catalog describes supported behavior; case records show what actually ran.
- **Findings:** open/resolved filters, search, source evidence, suggested fix, and
  persisted finding history, risk path, case timeline, action status and recorded
  verification, and a collapsed trace list. Linked cases can be selected from the
  loaded history. The drawer updates while open without resetting scroll or trace
  expansion. Decisions and undo stay in Slack or the authorized API; there are no
  case mutation controls in the dashboard.
- **Activity:** opt-in correlated CRM requests, expandable lifecycle evidence,
  actor/outcome filters, cursor pagination, queue health and processing/gap indicators.
  Follow [activity setup and recovery](activity-processing.md) to enable this view.

Global search opens company entity details. **Refresh observations** queues a
real security API job and follows its completion. The view fetches updates every
15 seconds while visible; the v1 local backend observes company APIs every 15
seconds (the unset backend fallback is 60 seconds). Persistent findings are updated, not duplicated on every refresh.

Company monitoring currently displays **observed metadata state** and genuine
sync events. An employee's last-observed time is not the time they performed an
action. Request-level CRM evidence is in the separate **Activity** tab. Cases show
the persisted workflow used by Slack, including investigation, human decisions,
actions and verification. A verification timestamp records a completed check, not
a permanent guarantee; an undone action is marked as previously verified.
If the demo agent grant is already revoked,
zero open findings is expected; see the CRM walkthrough for the scenario.

## Architecture and access

```text
Browser :3000
  -> Dashboard gateway :3000 (platform network)
     -> Security API :8002
        -> Neo4j company graph + SQLite findings/cases/actions/traces/job history
        -> Directory + CRM + Workbench APIs (customer network)
```

The dashboard uses the existing viewer credential for a fixed set of read routes
and the operator credential only for queuing observations. Both stay server-side.
The browser receives metadata, never service tokens or customer record contents.
The dashboard has no CRM credentials, database passwords, or data volume mounts.
Refresh requires a matching Origin and a custom request header. Foreign Host
headers, arbitrary proxy paths, and extra refresh body fields are rejected.

Dashboard user authentication and RBAC are deferred. This is a trusted local
operator UI, bound to `127.0.0.1:3000`; anyone with access to that local port can
read company metadata and request observations. Do not deploy this gateway as a
public or multi-user service without adding authentication and authorization.

The initial UI reads up to 200 entities, 200 findings, 200 cases, and 500 graph
edges. Case details contain the recorded events and actions and up to 500 trace
entries. Limits and unavailable sources are disclosed; a case missing from the
loaded history is not proof that no case exists. Server-side search and pagination
are a future extension for larger companies. Only the latest 12 sync
runs are included in the monitoring feed.

## Development and verification

The UI is native HTML/CSS/JavaScript served by FastAPI. No Node packages or asset
build step are required at runtime. Static assets live in `src/helio_dashboard/static`.
After editing, rebuild with `docker compose up --build -d --wait dashboard` and
reload the browser. This also rebuilds the security API when its code changes.

```sh
make test                 # Python unit/API tests; Neo4j tests skip without test env
make test-ui              # Node built-in unit tests (Node 20+)
make test-integration     # Full Python suite including isolated real Neo4j tests
```

The integration tests use a separate company namespace, temporary databases, and
real HTTP servers. They verify the dashboard refresh and finding lifecycle through
the real security API, CRM/directory APIs, and Neo4j, without changing the demo.

## Phase 6 verification — 2026-11-12

- `make test-ui`: **34 passed**, including evidence paths, escaping, case selection,
  failures, late responses, refresh state, keyboard focus, and event ordering.
- `.venv/bin/python -m pytest -q`: **749 passed, 21 skipped** without Neo4j test settings.
- Full suite with an isolated Neo4j test namespace: **769 passed, 1 skipped**.
  The remaining skip is the optional paid model probe; one existing LangChain beta
  warning is emitted. No model credits were used.
- Real HTTP dashboard-to-security-API checks read durable workflow timelines and
  verified actions for all three scenarios. Browser checks used a disposable local
  preview with seeded directory/CRM/workbench graph data and synthetic case details;
  all three paths, the case drawer, trace expansion, and policy catalog were checked.
- Live Slack thread comparison remains part of Phase 7 acceptance. No real Slack
  messages were sent and the running demo data was not reset.

## Graph readability follow-up — 2026-11-12

Related records are ordered by their observed relationships instead of opaque IDs.
CRM accounts are listed inside each person’s details drawer instead of occupying
a separate graph column. Apps/agents, CRM identities and grants follow their
parent relationships. Shared parents/resources use centered branches, so not
every many-to-one link can be horizontal. Cards have opaque backgrounds, internal
labels, source/type subtitles and at least 72 graph units between rows. The canvas
grows vertically and scrolls instead of squeezing dense columns together. Polling
preserves the scroll position; Reset graph view returns to the starting position.

Workbench agents and CRM identities are distinct graph records joined by ACTS_AS.
They remain separate and now occupy separate columns; no records or relationships
were removed. Connection cards show the destination host instead of the composite ID.

TDD verification: `make test-ui` — **40 passed**; dashboard API and three-scenario
HTTP integration checks — **19 passed**. Browser verification used the running
dashboard and its 77 entities, including aligned accounts, distinct agent identities,
opaque cards, outside connections and entity details. The dashboard container was
rebuilt; AI/Slack workers were not started.

### Person account drawer follow-up

The graph omits AppAccount cards and their incident lines, retaining the underlying
records and relationships. The footer reports visible counts. Clicking an employee
shows their CRM accounts (application, role, principal status), with each account
opening its own full details. Matching uses identity edges or observed owner IDs,
never names. Missing/limited evidence is labeled explicitly.

`make test-ui`: **42 passed**, including multiple-account association, missing graph
edges, escaped role text, unrelated-account exclusion and the actual drawer flow.
