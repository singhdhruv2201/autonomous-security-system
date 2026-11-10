# Awasi dashboard

Open [localhost:3000](http://localhost:3000) after `make setup` and `make up`.
No token needs to be pasted into the browser. The dashboard runs as a separate
service on the platform network, outside the simulated customer environment.

## Views

- **Dashboard:** current policy health, inventory counts, open findings, source
  freshness, and company monitoring. The green check means the configured check
  has no open findings with current source coverage; it is not a security certification.
- **Company graph:** live Neo4j inventory and relationships via the security API.
  Select a node for details, filter entity kinds, drag to pan, and use zoom controls.
- **Agents:** observed company agents, ownership, grants, and related flags.
  The deterministic observation worker is shown separately. Agent execution and
  remediation telemetry are not available yet.
- **Apps:** application owners, resources, associated findings, and connection health.
- **Policies:** the actual enabled access rule and its four conditions. Read-only.
- **Findings:** open/resolved filters, search, source evidence, suggested fix, and
  persisted finding history. No approval or remediation execution controls yet.
- **Activity:** opt-in correlated CRM requests, expandable lifecycle evidence,
  actor/outcome filters, cursor pagination, queue health and processing/gap indicators.
  Follow [activity setup and recovery](activity-processing.md) to enable this view.

Global search opens company entity details. **Refresh observations** queues a
real security API job and follows its completion. The view fetches updates every
15 seconds while visible; the v1 local backend observes company APIs every 15
seconds (the unset backend fallback is 60 seconds). Persistent findings are updated, not duplicated on every refresh.

Company monitoring currently displays **observed metadata state** and genuine
sync events. An employee's last-observed time is not the time they performed an
action. Request-level CRM evidence is in the separate **Activity** tab; Slack, AI
investigation and approved fixes remain future milestones. If the demo agent grant is already revoked,
zero open findings is expected; see the CRM walkthrough for the scenario.

## Architecture and access

```text
Browser :3000
  -> Dashboard gateway :3000 (platform network)
     -> Security API :8002
        -> Neo4j company graph + SQLite findings/job history
        -> Directory + CRM APIs (customer network)
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

The initial UI reads up to 200 entities, 200 findings, and 500 graph edges, enough
for the current small demo (54 entities, or 57 with the recovery reader). Truncation is indicated; server-side search and
pagination are a future extension for larger companies. Only the latest 12 sync
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
