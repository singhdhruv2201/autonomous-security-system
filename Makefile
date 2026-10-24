.PHONY: setup up stop test agent offboard restore-employee accounts grants audit revoke-agent update-customer

setup:
	PYTHONPATH=src python3 -m helio_security.setup

up:
	docker compose up --build -d --wait

stop:
	docker compose stop

test:
	.venv/bin/python -m pytest -q

agent:
	docker compose run --rm --no-deps sales-agent

offboard:
	docker compose exec -T simulator python -m helio_simulator.directory_cli emp-maya-chen inactive

restore-employee:
	docker compose exec -T simulator python -m helio_simulator.directory_cli emp-maya-chen active

accounts grants audit:
	docker compose exec -T crm python -m helio_crm.control $@

revoke-agent:
	docker compose exec -T crm python -m helio_crm.control revoke-agent

update-customer:
	docker compose exec -T crm python -m helio_crm.control update-customer

.PHONY: graph-sync graph-status graph-risks test-integration
graph-sync:
	docker compose run --rm --no-deps graph-sync

graph-status:
	docker compose run --rm --no-deps graph-sync python -m helio_security.cli status

graph-risks:
	docker compose run --rm --no-deps graph-sync python -m helio_security.cli risks

test-integration:
	@.venv/bin/python -c 'import os; from pathlib import Path; import pytest; values=dict(line.split("=",1) for line in Path(".env").read_text().splitlines() if "=" in line and not line.lstrip().startswith("#")); os.environ["NEO4J_PASSWORD"]=values["NEO4J_PASSWORD"]; os.environ["NEO4J_TEST_URI"]="bolt://127.0.0.1:7687"; raise SystemExit(pytest.main(["-q"]))'
