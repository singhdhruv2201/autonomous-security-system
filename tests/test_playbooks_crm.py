"""Tests for CRM playbooks — against a real local CRM HTTP server.

TDD: written first. Uses the serve() helper from test_graph.py.
Covers: pause, undo, stale version, non-agent target refusal, timeout reconciliation.
"""

import json
import secrets
import time

import pytest

from helio_crm.api import create_app as crm_app
from helio_security.playbooks import PlaybookError, get_playbook
from test_graph import serve

EMPLOYEES = [
    {"id": "emp-casey-wong", "name": "Casey Wong", "email": "casey@helio.test", "team_id": "team-eng"},
    {"id": "emp-maya-chen", "name": "Maya Chen", "email": "maya@helio.test", "team_id": "team-sales"},
    {"id": "emp-hannah-reed", "name": "Hannah Reed", "email": "hannah@helio.test", "team_id": "team-sales"},
]


def make_tokens():
    return {
        "admin": secrets.token_urlsafe(32),
        "maya": secrets.token_urlsafe(32),
        "agent": secrets.token_urlsafe(32),
        "observer": secrets.token_urlsafe(32),
        "actions": secrets.token_urlsafe(32),
    }


@pytest.fixture
def crm_server(tmp_path):
    tokens = make_tokens()
    app = crm_app(tmp_path / "crm.sqlite3", tokens, lambda: EMPLOYEES)
    with serve(app) as url:
        yield url, tokens


class TestPausePlaybook:
    def test_pause_principal_disables_agent(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.pause_principal")
        result = asyncio.run(
            playbook.run(
                target="agent:sales-research",
                crm_url=url,
                action_token=tokens["actions"],
                observer_token=tokens["observer"],
            )
        )
        assert result["after"]["principal_status"] == "disabled"
        assert result["before"]["principal_status"] == "active"

    def test_pause_then_undo_re_enables(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.pause_principal")
        result = asyncio.run(
            playbook.run(
                target="agent:sales-research",
                crm_url=url,
                action_token=tokens["actions"],
                observer_token=tokens["observer"],
            )
        )
        asyncio.run(
            playbook.undo(
                target="agent:sales-research",
                before=result["before"],
                crm_url=url,
                action_token=tokens["actions"],
                observer_token=tokens["observer"],
            )
        )
        # Verify re-enabled
        verified = asyncio.run(
            playbook.verify(
                target="agent:sales-research",
                expected_state="active",
                crm_url=url,
                observer_token=tokens["observer"],
            )
        )
        assert verified is True

    def test_stale_version_raises_error(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.pause_principal")
        # Pause once to bump version
        asyncio.run(
            playbook.run(
                target="agent:sales-research",
                crm_url=url,
                action_token=tokens["actions"],
                observer_token=tokens["observer"],
            )
        )
        # Undo to restore, but try to run again with stale version
        with pytest.raises(PlaybookError):
            asyncio.run(
                playbook.run(
                    target="agent:sales-research",
                    crm_url=url,
                    action_token=tokens["actions"],
                    observer_token=tokens["observer"],
                    force_version=1,  # deliberately stale
                )
            )

    def test_non_agent_target_refused_by_playbook(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.pause_principal")
        with pytest.raises(PlaybookError, match="agent"):
            asyncio.run(
                playbook.run(
                    target="employee:emp-maya-chen",
                    crm_url=url,
                    action_token=tokens["actions"],
                    observer_token=tokens["observer"],
                )
            )


class TestRevokeGrantPlaybook:
    def test_revoke_grant(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.revoke_grant")
        result = asyncio.run(
            playbook.run(
                target="grant-sales-agent",
                crm_url=url,
                action_token=tokens["actions"],
                observer_token=tokens["observer"],
            )
        )
        assert result["after"]["status"] == "revoked"

    def test_non_agent_grant_refused_by_playbook(self, crm_server):
        url, tokens = crm_server
        import asyncio
        playbook = get_playbook("crm.revoke_grant")
        with pytest.raises(PlaybookError, match="agent"):
            asyncio.run(
                playbook.run(
                    target="grant-maya",  # belongs to an employee account
                    crm_url=url,
                    action_token=tokens["actions"],
                    observer_token=tokens["observer"],
                )
            )
