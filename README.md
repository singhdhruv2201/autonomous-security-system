# Helio company simulator

First milestone: a persistent, fictional company directory. Five teams, twenty
employees, and read-only APIs. Applications, agents, permission grants, Neo4j,
the security backend, dashboard, and Slack are later milestones.

## Start with Docker

```sh
docker compose up --build -d --wait
```

Open **http://localhost:8000/docs** to explore the API. The root URL also redirects
to these interactive docs. Data is seeded on first startup and stored in the
`company-data` Docker volume. Restarting does not reset employees or duplicate
records. The container port is bound to localhost only; this unauthenticated
synthetic-data service is not intended for public exposure.

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
executive and the future offboarding scenario's subject. No offboarding behavior
has been added yet.

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

The directory will be one source for the eventual company graph. It is not the
security platform, and it does not pretend that external vendor applications are
already connected.
