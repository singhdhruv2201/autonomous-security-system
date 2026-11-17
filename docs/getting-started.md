# Installation and operation

Commands below target macOS/Linux or a compatible WSL shell. Use Python 3.11
for the environment used to generate the dependency locks, and Git to clone the
repository. Docker is needed only for the full stack; Node.js is needed only for
JavaScript checks. Installation and execution are subject to the [license](../LICENSE).

### 1. Run the browser showcase (recommended)

Accept your GitHub repository invitation and authenticate Git on your machine
before cloning this private repository.

```sh
git clone https://github.com/singhdhruv2201/autonomous-security-system.git
cd autonomous-security-system
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
See the [showcase guide](../showcase/README.md) for the complete walkthrough.

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

For real Slack messages and buttons, follow [Slack setup](slack-setup.md).
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
[activity capture and delivery](activity-delivery.md) and
[activity processing and recovery](activity-processing.md).

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
[model validation](model-operations.md) and the
[recorded Phase 7 results](archive/awasi-v1-plan.md#phase-7-results) before running it.
