# Awasi — autonomous security prototype

An independent working prototype by **Dhruv Singh**, prepared for evaluation by
Awasi's founders. It demonstrates how a security product can connect company
context to investigation, human decisions, and verified remediation. This is not
an official Awasi release or a production deployment.

**Start here:** [what it does](#what-the-product-does) ·
[run the showcase](#1-run-the-browser-showcase-recommended) ·
[run the full stack](#3-run-the-full-docker-stack-optional) ·
[future scope](#future-scope-not-yet-built)

**Private evaluation repository, not open source.** Awasi's founders have limited
evaluation permission under the [Proprietary Evaluation License](LICENSE).
Repository access is by invitation; other use requires written permission.

## What the product does

The goal is to answer three questions: **who or what can access company data,
where that access creates risk, and what verified action will resolve it?**

The current implementation follows a complete case lifecycle:

1. **Observe:** collect employment, ownership, permissions, app visibility, and
   destination metadata from connected services.
2. **Connect:** build an access graph linking people, accounts, apps, agents,
   grants, data, and external destinations.
3. **Detect:** evaluate deterministic rules against the observed evidence.
4. **Investigate:** let a model inspect evidence through bounded, read-only tools
   and explain the situation, impact, and uncertainty.
5. **Resolve:** apply the configured containment and approval workflow, ask the
   right person in Slack or the showcase, verify the resulting state, and record
   the outcome. Supported actions have Undo paths.

The model investigates and writes explanations. Deterministic code controls
permissions, action selection, approvals, verification, and rollback. A model
failure cannot grant it additional authority or turn an uncertain action into a
verified success.

### What works today

The demo company, **Helio**, has 20 synthetic employees, a CRM, two internal apps,
two Workbench agents, and a local receiver representing an outside destination.
These are working local services, not live Salesforce, Google Workspace, or
other SaaS integrations.

| Scenario | What Awasi detects | What the workflow does |
| --- | --- | --- |
| Orphaned agent | Maya Chen becomes inactive, but her Sales Research Agent retains CRM access. | Pause access, ask Hannah whether to retire it, then require Casey's approval before revoking the grant. |
| Public internal app | Renewal Tracker becomes public while holding confidential customer access. | Ask Luca whether sharing is intentional; make it company-only and verify the public URL no longer opens. Observed anonymous access triggers immediate containment. |
| Unapproved destination | Lead Enrichment sends synthetic customer records to an unapproved receiver. | Pause the agent, ask the approver what to do, and verify connection removal and resumption if chosen. Records already sent cannot be recalled. |

The interfaces expose the access graph, risk paths, evidence, case history,
verified actions, and human decisions. Cases and checkpoints persist in the
Docker stack; the showcase instead gives each browser session fresh temporary data.

The repository also includes **optional CRM activity capture, signed webhook
delivery, durable ingestion, recovery, and processing**. That pipeline is separate
from the main demo's snapshot-based detection. Workbench is currently polled;
the whole product is not an instant webhook-driven monitoring system.

### Two ways to explore it

| | Browser showcase | Full Docker stack |
| --- | --- | --- |
| Best for | A guided product walkthrough | Inspecting the service architecture and persistent workflows |
| Access graph | In-memory projection of normalized service snapshots | Neo4j |
| Services and actions | Real local service handlers, policies, workflows, and playbooks | Separate Directory, CRM, Workbench, outside receiver, security API, and worker services |
| Human decisions | Browser controls; optional Slack | Slack |
| State | Isolated temporary session databases; reset from the UI | Persistent Docker volumes |
| Model | Recorded assessments by default in the quickstart; optional live Anthropic | Live Anthropic when configured; explicit fallback on failure |

Recorded assessments are labeled as recordings, not presented as fresh model
reasoning. The showcase's local graph does not demonstrate Neo4j operation.

## Install and run

Commands below target macOS/Linux or a compatible WSL shell. Use Python 3.11
for the environment used to generate the dependency locks, and Git to clone the
repository. Docker is needed only for the full stack; Node.js is needed only for
JavaScript checks. Installation and execution are subject to the [license](LICENSE).

### 1. Run the browser showcase (recommended)

Accept your GitHub repository invitation and authenticate Git on your machine
before cloning this private repository.

```sh
git clone https://github.com/singhdhruv2201/awasi.git
cd awasi
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
PYTHONPATH=.:src .venv/bin/python -m showcase.app --no-slack --recorded-model
```

Open **[http://127.0.0.1:3070](http://127.0.0.1:3070)** and click **Start baseline**.
This mode needs no API keys, sends no Slack messages, and makes no model requests.

For a short walkthrough:

1. Run **Orphaned agent**. Inspect the access path and verified pause. Act as Hannah
   to retire the agent, then as Casey to approve revocation.
2. Choose **New baseline**, then run **Public internal app**. Make the app
   company-only and confirm the restriction. Repeat with **Include anonymous
   access** enabled to see immediate containment.
3. Choose **New baseline**, then run **Unapproved destination**. Inspect the six
   synthetic records counted at the receiver. Remove the connection and resume
   the agent; the historical delivery count remains six.

Select graph nodes and open the case timeline to inspect evidence and action
verification. **New baseline** resets only that browser session. Stop the server
with `Ctrl+C`; its temporary session data is removed on shutdown.
See the [showcase guide](showcase/README.md) for the complete walkthrough.

### 2. Enable live reasoning and Slack (optional)

Use your own credentials in the ignored local `.env` file or shell environment.
Never commit or send that file with the repository.

For live model reasoning, configure `ANTHROPIC_API_KEY` and `AWASI_MODEL`. The
current demo configuration uses `claude-haiku-5-5`; choose a model available to
your Anthropic account and supported by the project's budget accounting. Then run:

```sh
PYTHONPATH=.:src .venv/bin/python -m showcase.app --no-slack --live-model
```

This makes paid requests. The showcase has a persistent estimated allowance of
**$0.50 and 40 requests** in `showcase/data/anthropic-ledger.json`. Resetting the
browser baseline does not reset this ledger. Estimates are not provider invoices;
do not delete the ledger to bypass its limit.

For real Slack messages and buttons, follow [Slack setup](docs/slack-setup.md).
Configure `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `AWASI_SLACK_CHANNEL`, and
`AWASI_SLACK_DEFAULT_USER`, then run:

```sh
PYTHONPATH=.:src .venv/bin/python -m showcase.app --live-model
```

The bot must be in the configured channel. In the solo demo, the configured Slack
user acts as the fictional owner and approver. Stop any other worker listening on
the same Slack app, or use a separate app, so button clicks reach the correct demo.
Use `--recorded-model` instead of `--live-model` to demonstrate Slack without paid
model calls. Starting without explicit flags can enable live calls when both model
and Slack credentials are present.

### 3. Run the full Docker stack (optional)

Install Docker with Compose; the isolated acceptance overlay requires Compose
2.24.4 or newer. From the cloned repository and Python environment above:

```sh
make setup
docker compose config --quiet
docker compose up --build -d --wait simulator crm workbench outside neo4j security-api dashboard
```

`make setup` generates local credentials in an ignored, owner-only `.env`,
preserving existing credentials. Open the dashboard at
**[http://localhost:3000](http://localhost:3000)**. The security API is on port 8002.
This command starts the services and observation pipeline, but not the case worker.
Allow the initial graph synchronization to finish before triggering scenarios.

Configure Slack and Anthropic as above, stop the showcase if it uses the same
Slack app, then explicitly start the worker:

```sh
docker compose up --build -d awasi-agent
make offboard             # Complete the owner and approver conversation
make publish-app          # Complete the public-sharing conversation
make connect-enrichment   # Complete the unapproved-destination conversation
```

Run one scenario at a time. `make security-sync` requests an observation;
`make security-findings`, `make security-cases`, and `make workbench-status`
show current state. `make outsider-view` simulates anonymous app access. Even an
investigator's anonymous probe records a view, so it is not a passive check.

The normal worker has **no dollar cap unless a live ledger is configured**.
For a separate budgeted environment, use `compose.acceptance.yaml` together with
`compose.yaml` and project name `awasi-acceptance`. That overlay mounts
`data/phase7_live_ledger.json` and defaults to a cumulative $0.50 / 40-request cap.
Use the same project and file arguments for every command targeting that stack;
ordinary `make` scenario commands target the normal stack.

`docker compose stop` preserves data. `make demo-reset` requires typing
`RESET DEMO` and removes the normal project's data volumes, including cases and
checkpoints. It preserves `.env` and host usage ledgers and leaves the model/Slack
worker stopped. It does not erase historical Slack messages.

To enable the separate CRM event pipeline, follow
[activity capture and delivery](docs/activity-delivery.md) and
[activity processing and recovery](docs/activity-processing.md).

### Validation

```sh
PYTHONPATH=.:src .venv/bin/python -m pytest -q showcase/tests
node --check showcase/static/showcase.js
make test
make test-integration   # Requires configured Neo4j on localhost:7687
make test-ui
```

Showcase tests exercise real service handlers with temporary data and fake model
and Slack clients. The repository's acceptance tests also cover authorization,
checkpoint recovery, Undo, and uncertain writes. They do not establish production
reliability. Live provider and Slack checks are separate from automated tests.
`make eval` is an explicit paid model evaluation; see
[model validation](docs/phase3-validation.md) and the
[recorded Phase 7 results](docs/awasi-v1-plan.md#phase-7-results) before running it.

## Future scope (not yet built)

The current result demonstrates three bounded security workflows. It is not yet
a general-purpose autonomous security platform. The next product work would be:

| Area | Remaining scope |
| --- | --- |
| Real integrations | Production connectors for CRM, identity, collaboration, and agent platforms; OAuth installation, scope discovery, rate limits, and provider-specific actions. |
| Broader detection | More access and data-exposure rules, cross-service correlations, and the proposed recurring 15-minute AI review of suspicious activity beyond deterministic rules. |
| Faster prevention | Workbench event delivery and a coherent event-driven graph; selective inline checks before high-risk operations. Today's polling can detect a send only after it happened. |
| Customer identity and isolation | Multi-tenant onboarding, SSO, named operators, production role mappings, and tenant-scoped storage and credentials. The current solo role-switching experience is for demonstration. |
| Production operation | Durable queues and a production database/checkpointer, multiple workers, deployment automation, backups, observability, and load/recovery testing. Current workflows use a single worker and SQLite. |
| Evaluation and trust | Broader model and adversarial evaluations, stronger evidence completeness checks, and measured reliability for connectors, human decisions, and remediation under failure. |
| Cleanup and hardening | Complete the planned removal of legacy optional CRM enforcement, revisit the simulator review backlog, and resolve dashboard availability issues observed under concurrent load. |

The current demo also has explicit limits: it cannot recover data already sent;
activity scanners and response checks must not be described as clean scans when
not configured; and uncertain actions require review. A previous full-suite run
under load was not clean, so this README makes no blanket claim that every
integration check currently passes.

The [v1 plan](docs/awasi-v1-plan.md) records implementation history and the remaining
cleanup phase; some earlier phase-status notes are historical. The
[review backlog](docs/fixes.md) should be reconciled against current code before
planning hardening work.

## Code and architecture guide

- [Showcase](showcase/README.md): browser walkthrough, model modes, and Slack setup.
- [Security backend](docs/backend.md), [graph](docs/graph.md), and
  [dashboard](docs/dashboard.md): observation, access paths, and case presentation.
- [CRM](docs/crm.md): source-side permissions and metadata.
- [Slack integration](docs/slack-setup.md): delivery, decisions, message authorship,
  and recovery behavior.
- [Engineering instructions](AGENTS.md): test-driven behavioral development.

## License and evaluation permission

Copyright © 2026 Dhruv Singh. Original repository material is provided under the
[Awasi Founders Evaluation License](LICENSE), a proprietary, non-open-source
license. Awasi's founders may run and make local modifications solely to evaluate
this prototype. Production use, incorporation into another product, redistribution,
and access for additional evaluators require separate written permission.

GitHub invitations control repository access; the license defines permitted use.
It does not override applicable third-party licenses or rights granted through
[GitHub's Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).
Contact [Dhruv Singh](https://github.com/singhdhruv2201) for access or additional
permission. Awasi names are used to identify the intended recipient; no affiliation,
endorsement, or trademark license is implied.
