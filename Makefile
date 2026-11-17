.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python

.PHONY: help showcase test-showcase check
help:
	@echo "Autonomous Security System"
	@echo "  make showcase         Start the recorded browser demo (no Slack/API spend)"
	@echo "  make setup            Generate local Docker credentials"
	@echo "  make test             Run backend tests"
	@echo "  make test-showcase    Run showcase tests and JavaScript syntax check"
	@echo "  make test-ui          Run dashboard JavaScript tests"
	@echo "  make check            Run all three check groups"
	@echo "  make test-integration Run backend tests with local Neo4j"
	@echo "  make stop             Stop the normal Docker stack, preserving data"
	@echo "  See docs/getting-started.md before enabling live workers or resetting data."

showcase:
	PYTHONPATH=.:src $(PYTHON) -m showcase.app --no-slack --recorded-model

test-showcase:
	PYTHONPATH=.:src $(PYTHON) -m pytest -q showcase/tests
	node --check showcase/static/showcase.js

check: test test-showcase test-ui

.PHONY: setup up stop test agent offboard restore-employee accounts grants audit revoke-agent update-customer \
	publish-app outsider-view connect-enrichment run-enrichment workbench-status outside-received

setup:
	PYTHONPATH=src python3 -m helio_security.setup
	PYTHONPATH=src python3 -m helio_workbench.setup

up:
	docker compose up --build -d --wait

stop:
	docker compose stop

test:
	$(PYTHON) -m pytest -q

.PHONY: test-ui
test-ui:
	node --test tests/frontend/*.test.mjs

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

.PHONY: test-integration
test-integration:
	@$(PYTHON) -c 'import os; from pathlib import Path; import pytest; values=dict(line.split("=",1) for line in Path(".env").read_text().splitlines() if "=" in line and not line.lstrip().startswith("#")); os.environ["NEO4J_PASSWORD"]=values["NEO4J_PASSWORD"]; os.environ["NEO4J_TEST_URI"]="bolt://127.0.0.1:7687"; raise SystemExit(pytest.main(["-q"]))'

.PHONY: security-overview security-findings security-connectors security-runs security-sync security-audit security-cases
security-overview security-findings security-connectors security-runs security-sync security-audit security-cases:
	docker compose exec -T security-api python -m helio_security.client $(patsubst security-%,%,$@)

publish-app:
	docker compose exec -T workbench python -m helio_workbench.control publish-app

outsider-view:
	docker compose exec -T -e WORKBENCH_URL=http://workbench:8004 outside python -m helio_workbench.control outsider-view

connect-enrichment:
	docker compose exec -T workbench python -m helio_workbench.control connect-enrichment
	docker compose exec -T workbench python -m helio_workbench.runner agent-lead-enrichment

run-enrichment:
	docker compose exec -T workbench python -m helio_workbench.runner agent-lead-enrichment

workbench-status:
	docker compose exec -T workbench python -m helio_workbench.control status

outside-received:
	docker compose exec -T outside python -m outside.cli

.PHONY: eval demo-reset
eval:
	PYTHONPATH=src $(PYTHON) -m helio_security.evaluation --live

demo-reset:
	PYTHONPATH=src $(PYTHON) -m helio_security.demo_reset
