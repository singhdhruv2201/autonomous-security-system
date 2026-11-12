# Awasi v1 — Helio security demo

Helio is a synthetic company with twenty employees, a CRM, two internal apps,
two Workbench agents, and an outside receiver. Awasi observes their APIs, builds
a Neo4j access graph, investigates risks, and coordinates verified fixes in Slack.
This is a working local simulation, not an integration with Salesforce or another vendor.

The model explains evidence using read-only MCP tools. Deterministic rules and
workflows control containment, approvals, verification, and Undo. Model failure
uses an explicitly recorded fallback; it never gives the model permission to fix things.

## Setup

Requires Docker Compose (2.24.4+ for the optional acceptance overlay), Python 3.11+
and Node.js for frontend tests.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
make setup
docker compose up --build -d --wait simulator crm workbench outside neo4j security-api dashboard
```

Open [the dashboard](http://localhost:3000). The graph shows access paths and
outside exposure; click a person for their CRM accounts or a case for its timeline.
Only localhost ports are exposed. API explorers for customer services can be
published using `compose.override.yaml.example`; the security API is on port 8002.

`make setup` creates and preserves credentials in the ignored, owner-only `.env`.
Never commit or share it, or print expanded Compose configuration. Use
`docker compose config --quiet` for validation.

Configure these locally in `.env` before enabling live Slack/model calls:

- `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `AWASI_SLACK_CHANNEL`, and
  `AWASI_SLACK_DEFAULT_USER`. See [Slack setup](docs/slack-setup.md).
- `ANTHROPIC_API_KEY` and `AWASI_MODEL` (the current demo uses `claude-haiku-5-5`).

The default Slack user acts as the fictional owner, team lead, and approver in
this single-person demo. An unmapped Slack user cannot decide a case.
`make up` starts **all** default services, including the worker: configured keys
can cause real model requests and Slack messages. Start it deliberately:

```sh
docker compose up --build -d awasi-agent
```

Normal worker operation has no dollar cap unless `AWASI_LIVE_LEDGER` is configured.
Use the acceptance overlay below for the shared Phase 7 cap.

## Three demo scenarios

Start from fresh demo state and complete each Slack conversation before triggering
the next scenario. Observation runs every `SECURITY_SYNC_INTERVAL` seconds (the
local setup uses 15); `make security-sync` queues an immediate observation.

| Trigger | Expected Slack conversation | Verified result |
| --- | --- | --- |
| `make offboard` | Maya becomes inactive. Awasi pauses the Sales Research Agent and asks Hannah whether to retire it. Choose **Retire**, then Casey approves revocation. | Agent paused, grant revoked; subsequent access denied. |
| `make publish-app` | Renewal Tracker becomes public. If no anonymous views were observed, Luca is asked whether sharing was intentional. Choose **No, make it company-only**, then **Keep it company-only**. | App becomes company-only and its anonymous link returns 404. |
| `make connect-enrichment` | Lead Enrichment sends six synthetic records to an unapproved outside destination. Awasi pauses it and asks Casey to decide. Choose **Remove the connection and resume**. | Connection disabled, agent resumed, both verified. Previously sent records cannot be recalled. |

For public sharing, an observed anonymous view triggers immediate containment.
`make outsider-view` simulates that access. Even the investigator's anonymous probe
records a view; do not treat probes as passive when interpreting the timeline.
Owner silence for 15 minutes restricts the app. **Undo restriction** restores the
previous visibility and records a human override; the next case requires approver
consent. Approver silence does not silently grant approval. Uncertain mutation
outcomes go to `needs_review`, not `resolved`.

Controls should stay silent: company-only Support Lookup, Renewal Digest connected
to approved `partner-api`, and an agent whose owner is active. The Workbench is
polled; its evidence does not yet arrive by webhook. CRM activity capture and signed
webhook delivery are separate, opt-in evidence infrastructure.

Useful inspection commands:

```sh
make security-findings
make security-cases
make security-connectors
make workbench-status
make outside-received
make test-ui
```

## Acceptance tests and paid evaluation

```sh
make test               # Temporary databases; Neo4j checks skip without configuration
make test-integration   # Full suite with real Neo4j on localhost:7687
make test-ui            # Frontend behavior/layout tests
make eval               # Explicit paid opt-in: six recorded evidence assessments
```

`tests/test_v1_acceptance.py` uses real local HTTP services, Neo4j and MCP
subprocesses, a scripted investigator and fake Slack transport. It covers all
three scenarios, quiet controls, durable checkpoint reopen while waiting,
unauthorized Slack users, Undo and approver escalation, and ambiguous writes.
These tests do not spend API credits or send channel messages.

`make eval` evaluates the original model output for cause classification,
evidence IDs and owner eligibility, plus no fix recommendation on controls.
It uses six compact synthetic metadata fixtures, one request each, no provider
retries. It measures assessment quality, **not** MCP tool selection or live Slack.
Results are written to `data/phase7-evaluation.json`; an existing report prevents
an accidental paid rerun. To deliberately rerun after a change, provide a new
`--output` path to `PYTHONPATH=src .venv/bin/python -m helio_security.evaluation --live`.
Keep the same ledger path so the allowance does not reset.

Evaluation and the isolated live worker share `data/phase7_live_ledger.json`,
with a cumulative ceiling of **$0.50 and 40 requests** for Phase 7. Reservations
count against the allowance before dispatch; uncertain requests retain a
conservative charge. Usage is a local estimate, not the provider's invoice.
Existing historical ledgers are preserved. Do not delete a ledger to bypass its cap.
See [model operation](docs/phase3-validation.md) for fallback and usage details.

A separate clean Docker stack preserves the normal demo's volumes and history:

```sh
# Start services first. The worker is enabled separately after checking controls.
docker compose -p awasi-acceptance -f compose.yaml -f compose.acceptance.yaml up --build -d --wait simulator crm workbench outside neo4j security-api dashboard
docker compose -p awasi-acceptance -f compose.yaml -f compose.acceptance.yaml up --build -d awasi-agent
```

Its dashboard is [localhost:3001](http://localhost:3001), security API 18002,
Workbench 18004 and outside receiver 18005. The overlay mounts the host usage
ledger into the worker. Use the same Compose prefix for its commands (the ordinary
`make` triggers target the normal demo). For example:

```sh
docker compose -p awasi-acceptance -f compose.yaml -f compose.acceptance.yaml exec -T simulator python -m helio_simulator.directory_cli emp-maya-chen inactive
```

Stop the acceptance worker after the walkthrough to end live model activity.
Measured results and the exact manual completion status belong in the
[Phase 7 results](docs/awasi-v1-plan.md#phase-7-results).

## Stop or reset

`docker compose stop` preserves state. `make demo-reset` prompts you to type
`RESET DEMO`, then removes **all volumes in the normal Compose project** and
starts fresh unpaid services. This erases cases, checkpoints, CRM, directory,
Workbench, graph and optional activity history. `.env` and host usage ledgers
are retained. It leaves the Slack/model worker stopped. It does not reset the
separate acceptance project, and it does not delete historical Slack messages.

## Architecture and development

Runtime and development versions are locked in `requirements.lock` and
`requirements-dev.lock`. Behavioral changes use tests first, then focused
regressions and appropriate integration checks. Do not use live credentials in tests.

- [V1 plan](docs/awasi-v1-plan.md), [known follow-up fixes](docs/fixes.md)
- [Dashboard](docs/dashboard.md), [security backend](docs/backend.md), [graph](docs/graph.md)
- [CRM](docs/crm.md), [activity processing](docs/activity-processing.md), [signed delivery](docs/activity-delivery.md)

The directory owns employment status. CRM authentication, grants and principal
validity remain enforced. Optional CRM owner/volume/failure/content controls stay
off in this demo; removing their old implementation is Phase 8, outside this work.
