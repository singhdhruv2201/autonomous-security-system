# Helio company simulator

A fictional company with a persistent directory (five teams, twenty employees)
and a working Sales CRM. The CRM has its own database, enforced bearer-token
permissions, six synthetic customers, an audit trail, and a Sales Research Agent
that runs in a separate container and makes actual HTTP requests.

The separate security backend observes the directory and CRM through their APIs,
builds a Neo4j company graph, and persists security findings. Its authenticated
API runs on port 8002 with a background observation worker. The Awasi dashboard
on port 3000 shows company security, the graph, agents, apps, policies, and findings.
AI investigation, Slack, and automated remediation remain later milestones.
The CRM also has opt-in request/response activity capture and signed background
webhook delivery to a durable Awasi inbox (phases 0–4). See
[activity startup, delivery and replay](docs/activity-delivery.md). Inbox processing,
gap recovery and activity dashboard visibility remain later phases.
See [dashboard guide](docs/dashboard.md), [security API and workflow](docs/backend.md) and [graph walkthrough](docs/graph.md).

## Start with Docker

```sh
make setup
make up
make security-overview
```

Open **http://localhost:3000** for the Awasi dashboard.
Use **http://localhost:8000/docs** for the directory or
**http://localhost:8001/docs** for the CRM API explorer, or
**http://localhost:8002/docs** for the security platform API.
`make setup` generates four CRM credentials, two security API tokens, a webhook signing secret, and a Neo4j password in a local `.env` file
with owner-only permissions. It preserves existing credentials on repeat runs.
The file is excluded from Git and Docker build contexts; there is no committed
credential template. Never share it or the output of `docker compose config`
without `--quiet`, which can expand credentials.

Data is seeded on first startup into separate `company-data`, `crm-data`, and
`security-graph-data` volumes, plus `security-state` for findings and job history.
Restarting preserves state. All published ports are bound to localhost;
this development environment is not intended for public exposure.

Neo4j Browser is available at **http://localhost:7474/browser/**. Connect to
`bolt://localhost:7687` as `neo4j`, using `NEO4J_PASSWORD` from your local `.env`.
The security worker refreshes observations every 60 seconds. Use `make security-sync`
to queue an additional refresh, `make security-runs` to track it, and
`make security-findings` to view persisted risks. The older `make graph-sync`,
`make graph-status`, and `make graph-risks` commands remain graph-only diagnostics.

## CRM demo

```sh
make agent              # Reads six synthetic customers and prints a summary
make update-customer    # Maya updates a customer note using her write grant
make offboard           # Mark Maya inactive in the company directory
make agent              # Still succeeds: the agent has a separate credential
make grants             # Inspect ownership, scopes, status, and versions
make revoke-agent       # Administrator revokes the agent's access grant
make agent              # Expected failure: HTTP 403, exit code 2 from agent
make audit              # Inspect recorded reads, writes, denials, and revocation
```

The `make` command may also exit 2 when a recipe fails; the agent itself exits 2
for access denial. It exits 1 for configuration/network errors.
The walkthrough intentionally leaves Maya inactive and the grant revoked.
See [CRM details and reset instructions](docs/crm.md) for the access model,
API contract, and how to repeat the scenario with fresh CRM data.

```sh
docker compose stop       # Stop while preserving company state
docker compose up -d      # Start again
```

For an intentional full reset of this simulator's data:

```sh
docker compose down -v
docker compose up -d --wait
```

## Company structure

| Team ID | Team | Employees | Lead |
| --- | --- | ---: | --- |
| engineering | Engineering | 8 | Jordan Lee |
| sales | Sales | 4 | Hannah Reed |
| support | Customer Support | 3 | Owen Clark |
| operations | Operations | 3 | Amara James |
| leadership | Leadership | 2 | Alex Rivera |

All employees start active. Names and addresses are synthetic; emails use the
reserved `helio.example` domain. Maya Chen (`emp-maya-chen`) is a sales account
executive and the offboarding scenario's subject. A local operator command changes
employment status; the directory's HTTP endpoints remain read-only.

An employee has a stable ID, name, email, team ID, job title, employment status,
and team-lead flag. Team membership has a database foreign key, and a team cannot
have two designated leads. Lead designation describes the organization; it does
not grant security-platform approval permissions.

## API

| Method | Path | Result |
| --- | --- | --- |
| GET | `/company` | Company identity and current counts |
| GET | `/teams` | Teams, employee counts, and lead IDs |
| GET | `/teams/sales` | Sales team details and members |
| GET | `/employees` | All employees |
| GET | `/employees?team_id=sales&status=active` | Filtered directory |
| GET | `/employees/emp-maya-chen` | One employee |
| GET | `/health` | Database connectivity check |
| GET | `/snapshot` | Complete teams/employees snapshot from one transaction |

Unknown employees or teams return 404, and unsupported status filters return 422.
Team counts include both active and inactive employees. IDs stay stable across
restarts and fixture resets, ready for future application ownership and graph
relationships. There are no write endpoints in this milestone.

```sh
curl http://localhost:8000/company
curl http://localhost:8000/teams/sales
```

## Local development and tests

Python 3.11+ is required. Docker provides Python without a host installation.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pytest -q
make test-integration  # Complete suite including real Neo4j; requires make up
PYTHONPATH=src .venv/bin/python -m uvicorn helio_simulator.api:app --reload --host 127.0.0.1
```

Stop the Docker service before starting a local server on the same port. Local
runs default to `data/helio.sqlite3`, independent of the Docker volume. Override
the path by exporting `HELIO_DB_PATH`. Tests use temporary databases.

Runtime dependencies are pinned in `requirements.lock`; development dependencies
are pinned in `requirements-dev.lock`. To deliberately refresh these locks:

```sh
.venv/bin/python -m pip install 'pip-tools>=7,<8'
.venv/bin/pip-compile --strip-extras --output-file=requirements.lock pyproject.toml
.venv/bin/pip-compile --strip-extras --extra=dev --constraint=requirements.lock --output-file=requirements-dev.lock pyproject.toml
```

## Implementation boundaries

- `src/helio_simulator/seed.py`: reproducible team and employee fixtures.
- `src/helio_simulator/directory.py`: SQLite schema, one-time seed, and queries.
- `src/helio_simulator/api.py`: FastAPI app factory, response models, and routes.
- `tests/test_directory.py`: membership, filters, restart persistence, constraints,
  and read-only API checks.

The directory is the authority for employment status in the company graph. The CRM is our
own small business application, not a connection to Salesforce or another vendor.
The graph can identify an inactive employee's agent with an active grant to
confidential data. Revocation still requires an operator in the CRM. Automated
investigation and approval belong to a later security-platform milestone.
