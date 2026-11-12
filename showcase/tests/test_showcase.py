import pytest
from fastapi.testclient import TestClient

from showcase.app import create_app

HEADERS = {"X-Awasi-Showcase": "1"}


@pytest.fixture
def client():
    with TestClient(create_app()) as browser:
        yield browser


def start(client):
    response = client.post("/api/session", headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def trigger(client, scenario):
    response = client.post(f"/api/scenarios/{scenario}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def answer(client, case, choice):
    response = client.post(
        f"/api/cases/{case['id']}/decision", headers=HEADERS,
        json={"key": case["question"]["key"], "choice": choice},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_baseline_is_seeded_and_reset_is_private(client):
    assert client.get("/api/session").status_code == 404
    assert client.post("/api/session").status_code == 403
    assert client.post("/api/session", headers={**HEADERS, "Origin": "http://other.example"}).status_code == 403
    baseline = start(client)
    assert baseline["inventory"]["employees"] == 20
    assert baseline["metrics"]["open_findings"] == 0
    assert baseline["cases"] == []
    assert baseline["scenarios"]["public"]["evidence"]["visibility"] == "company"
    assert all(source["status"] == "ok" for source in baseline["sources"].values())

    original_cookie = client.cookies.get("awasi_showcase")
    start(client)
    trigger(client, "public")
    new_cookie = client.cookies.get("awasi_showcase")
    assert original_cookie != new_cookie
    client.cookies.clear()
    client.cookies.set("awasi_showcase", original_cookie)
    assert client.get("/api/session").status_code == 404
    client.cookies.clear()
    client.cookies.set("awasi_showcase", new_cookie)

    reset = start(client)
    assert reset["id"] != baseline["id"]
    assert reset["metrics"] == baseline["metrics"]
    assert reset["scenarios"]["public"]["evidence"]["visibility"] == "company"
    assert reset["cases"] == []
    assert len(reset["samples"]) == 1


@pytest.mark.parametrize(
    ("scenario", "rule", "choices", "check", "value", "action_count"),
    [
        ("offboarding", "inactive_owner_agent_access", ["retire", "approve"], "grant_status", "revoked", 2),
        ("public", "public_app_confidential_data", ["restrict", "keep_restricted"], "visibility", "company", 1),
        ("public_view", "public_app_confidential_data", ["keep_restricted"], "visibility", "company", 1),
        ("external", "agent_unapproved_destination", ["remove_connection"], "enabled", False, 3),
    ],
)
def test_real_policy_workflow_and_remediation(client, scenario, rule, choices, check, value, action_count):
    start(client)
    state = trigger(client, scenario)
    assert len(state["findings"]) == 1
    assert state["findings"][0]["rule"] == rule
    assert state["scenarios"][scenario]["recording"]["model"]
    assert state["scenarios"][scenario]["graph"]["edges"]
    case, = state["cases"]
    assert case["investigation"]["source"] == "fallback"
    assert case["question"]["choices"][0] == choices[0]
    assert client.post(f"/api/scenarios/{scenario}", headers=HEADERS).status_code == 409
    if scenario == "external":
        assert state["metrics"]["records_delivered"] == 6
        assert state["scenarios"][scenario]["evidence"]["agent_status"] == "paused"
    if scenario == "public_view":
        assert state["metrics"]["anonymous_views"] == 1
        assert state["scenarios"][scenario]["evidence"]["visibility"] == "company"
    if scenario == "public":
        assert not case["actions"]
    response = client.post(f"/api/cases/{case['id']}/decision", headers=HEADERS,
                           json={"key": case["question"]["key"], "choice": "invalid"})
    assert response.status_code == 409
    for choice in choices:
        state = answer(client, case, choice)
        case, = state["cases"]
    assert case["status"] == "resolved"
    assert len(case["actions"]) == action_count
    assert all(action["verified_at"] for action in case["actions"])
    assert any(event["type"] == "verification" for event in case["events"])
    assert state["metrics"]["open_findings"] == 0
    assert state["scenarios"][scenario]["evidence"][check] == value
    assert "contact@northstar.example" not in str(state)
    if scenario == "external":
        assert state["metrics"]["records_delivered"] == 6
        assert state["scenarios"][scenario]["evidence"]["agent_status"] == "active"


def test_separate_browsers_and_stale_answers_cannot_cross_sessions(client):
    start(client)
    state = trigger(client, "offboarding")
    cookie_a = client.cookies.get("awasi_showcase")
    case, = state["cases"]
    client.cookies.clear()
    other = start(client)
    assert other["id"] != state["id"]
    assert other["scenarios"]["offboarding"]["evidence"]["employee_status"] == "active"
    assert client.post(f"/api/cases/{case['id']}/decision", headers=HEADERS,
                       json={"key": case["question"]["key"], "choice": "retire"}).status_code == 404
    client.cookies.clear()
    client.cookies.set("awasi_showcase", cookie_a)
    next_state = answer(client, case, "retire")
    assert next_state["cases"][0]["question"]["key"] != case["question"]["key"]
    assert client.post(f"/api/cases/{case['id']}/decision", headers=HEADERS,
                       json={"key": case["question"]["key"], "choice": "retire"}).status_code == 409


def test_import_and_service_initialization_ignore_existing_databases(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    checkout = Path(__file__).resolve().parents[2]
    live = tmp_path / "existing.sqlite3"
    live.write_bytes(b"existing data sentinel")
    child = subprocess.run(
        [
            sys.executable, "-c",
            "from pathlib import Path; import sys; from showcase.services import HelioServices; "
            "from types import SimpleNamespace; "
            "root = Path(sys.argv[1]); "
            "services = HelioServices(SimpleNamespace(base='http://127.0.0.1:12345', routes={}), root); "
            "services.close()",
            str(tmp_path / "new-session"),
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(checkout), str(checkout / "src"))),
             "WORKBENCH_DB_PATH": str(live), "OUTSIDE_DB_PATH": str(live)},
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert child.returncode == 0, child.stderr
    assert live.read_bytes() == b"existing data sentinel"
    assert (tmp_path / "new-session" / "workbench.sqlite3").exists()
    assert not (tmp_path / "data").exists()


def test_model_mode_displays_validated_provider_assessment_and_read_trace(monkeypatch, tmp_path):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from helio_security.investigator import ModelInvestigator
    from helio_security.live_ledger import LiveLedger
    from showcase.session import ModelConfig

    class Provider(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    # The platform derives this stable ID from the seeded offboarding policy path.
    finding_id = "d0021554-01b7-5e38-8ba2-d4fdd9f1aa5b"
    replies = [
        AIMessage(content="", tool_calls=[{
            "name": "get_finding", "id": "read", "args": {"finding_id": finding_id},
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "Assessment", "id": "assessment", "args": {
                "summary": "The agent retained a CRM grant after Maya was offboarded.",
                "impact": "Six confidential records were reachable.",
                "likely_cause": "offboarding_gap", "confidence": "high",
                "evidence_refs": [finding_id, "grant-sales-agent"],
                "slack_message": {
                    "headline": "Sales Research Agent retains access",
                    "context": "Maya has been offboarded and the agent still has a CRM grant.",
                    "impact": "The grant covers six confidential customer records.",
                },
            },
        }]),
    ]
    monkeypatch.setattr(ModelInvestigator, "_build_model", lambda _: Provider(messages=iter(replies)))
    ledger = LiveLedger(tmp_path / "showcase-ledger.json")
    from helio_security.slack_app import SlackConfig

    slack = SlackRecorder()
    config = SlackConfig(bot_token="test-bot-token", channel="C123")
    with TestClient(create_app(ModelConfig("claude-haiku-5-5", "test-provider-key", ledger),
                               slack=config, slack_client=slack)) as client:
        start(client)
        state = trigger(client, "offboarding")
    case, = state["cases"]
    assert case["investigation"]["source"] == "model"
    assert case["investigation"]["evidence_refs"] == [finding_id, "grant-sales-agent"]
    assert any(span["name"] == "get_finding" for span in case["spans"])
    assert ledger.get_data()["total_requests"] == 2
    assert "test-provider-key" not in str(state)
    alert = slack.posts[0]["text"]
    assert "Live Anthropic assessment" in alert
    assert case["investigation"]["summary"] in alert
    assert case["investigation"]["impact"] in alert


class SlackRecorder:
    def __init__(self, fail=False):
        self.posts = []
        self.updates = []
        self.fail = fail

    async def chat_postMessage(self, **message):
        self.posts.append(message)
        if self.fail:
            raise TimeoutError("unconfirmed Slack post")
        return {"ts": f"100.00000{len(self.posts)}"}

    async def chat_update(self, **message):
        self.updates.append(message)
        return {"ts": message["ts"]}


@pytest.mark.parametrize("scenario", ["offboarding", "public", "public_view", "external"])
def test_scenarios_deliver_slack_alerts_from_the_browser(scenario):
    from helio_security.slack_app import SlackConfig

    slack = SlackRecorder()
    config = SlackConfig(bot_token="test-bot-token", channel="C123", default_user="U123")
    with TestClient(create_app(slack=config, slack_client=slack)) as client:
        assert client.get("/api/config").json()["slack_enabled"]
        baseline = start(client)
        assert not slack.posts
        state = trigger(client, scenario)
        case, = state["cases"]
        assert case["slack_delivery"] == "sent"
        root = slack.posts[0]
        assert root["channel"] == "C123"
        assert "Synthetic Helio data" in root["text"]
        assert "<@U123>" in root["text"]
        assert "showcase browser" in root["text"]
        assert "Recorded Anthropic assessment" in root["text"]
        assert all(block["type"] != "actions" for block in root["blocks"])
        assert "test-bot-token" not in str(state)
        if scenario == "offboarding":
            state = answer(client, case, "retire")
            assert "thread_ts" in slack.posts[-1]
            assert slack.posts[-1]["thread_ts"] == "100.000001"
            assert slack.updates[0]["ts"] == "100.000001"
            assert "Decision recorded" in slack.updates[0]["text"]
            assert state["cases"][0]["slack_delivery"] == "sent"
        post_count = len(slack.posts)
        reset = start(client)
        assert len(slack.posts) == post_count
        assert reset["id"] != baseline["id"]
        assert not reset["cases"]


def test_unconfirmed_slack_delivery_is_visible_and_does_not_block_case():
    from helio_security.slack_app import SlackConfig

    slack = SlackRecorder(fail=True)
    config = SlackConfig(bot_token="test-bot-token", channel="C123")
    with TestClient(create_app(slack=config, slack_client=slack)) as client:
        start(client)
        state = trigger(client, "public")
        case, = state["cases"]
        assert case["slack_delivery"] == "unknown"
        assert case["status"] == "waiting_owner"
        first_post = slack.posts[0]["text"]
        answer(client, case, "restrict")
        assert sum(post["text"] == first_post for post in slack.posts) == 1


def test_unconfigured_showcase_reports_local_only_mode(client):
    assert not client.get("/api/config").json()["slack_enabled"]
    start(client)
    assert trigger(client, "public")["cases"][0]["slack_delivery"] == "disabled"


@pytest.mark.parametrize(
    ("options", "slack_enabled", "live_model"),
    [([], True, True), (["--recorded-model"], True, False), (["--no-slack"], False, False)],
)
def test_cli_uses_configured_model_for_slack_alerts(options, slack_enabled, live_model, monkeypatch):
    import sys

    import dotenv
    from helio_security import live_ledger
    from showcase import app

    monkeypatch.setattr(live_ledger, "LiveLedger", lambda *args, **kwargs: object())
    for key in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "AWASI_SLACK_CHANNEL", "AWASI_SLACK_DEFAULT_USER",
                "ANTHROPIC_API_KEY", "AWASI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: {
        "SLACK_BOT_TOKEN": "test-bot-token",
        "AWASI_SLACK_CHANNEL": "C123",
        "AWASI_SLACK_DEFAULT_USER": "U123",
        "ANTHROPIC_API_KEY": "test-provider-key",
        "AWASI_MODEL": "claude-haiku-5-5",
    })
    monkeypatch.setenv("AWASI_SLACK_CHANNEL", "C456")
    monkeypatch.setattr(sys, "argv", ["showcase.app", *options])
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    launched = []
    monkeypatch.setattr(app.uvicorn, "run", lambda application, **kwargs: launched.append(application))
    app.main()

    with TestClient(launched[0]) as client:
        config = client.get("/api/config").json()
        assert config["slack_enabled"] is slack_enabled
        assert config["model"] == ("claude-haiku-5-5" if live_model else None)
    assert "test-bot-token" not in str(config)


class SlackSocket:
    def __init__(self):
        self.socket_mode_request_listeners = []
        self.acks = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def send_socket_mode_response(self, response):
        self.acks.append(response.envelope_id)

    async def click(self, message, choice, user="U123", envelope="click"):
        import json
        from types import SimpleNamespace

        button = next(
            button for block in message["blocks"] if block["type"] == "actions"
            for button in block["elements"] if json.loads(button["value"])["choice"] == choice
        )
        event = SimpleNamespace(type="interactive", envelope_id=envelope, payload={
            "type": "block_actions", "channel": {"id": message["channel"]},
            "user": {"id": user}, "container": {"message_ts": message["ts"]}, "actions": [button],
        })
        await self.socket_mode_request_listeners[0](self, event)
        assert self.acks[-1] == envelope


def test_slack_fix_buttons_apply_verified_changes_to_their_own_showcase_case():
    from helio_security.slack_app import SlackConfig

    slack = SlackRecorder()
    socket = SlackSocket()
    config = SlackConfig(bot_token="test-bot-token", app_token="test-app-token", channel="C123", default_user="U123")
    with TestClient(create_app(slack=config, slack_client=slack, slack_socket=socket)) as client:
        start(client)
        state = trigger(client, "offboarding")
        root = {**slack.posts[0], "ts": "100.000001"}
        assert "showcase browser" not in root["text"]

        client.portal.call(socket.click, root, "retire", "U999")
        assert client.get("/api/session").json()["samples"] == state["samples"]

        client.portal.call(socket.click, root, "retire", "U123", "retire")
        followup = {**slack.posts[-1], "ts": "100.000002"}
        assert followup["thread_ts"] == root["ts"]
        assert all(block["type"] != "actions" for block in slack.updates[0]["blocks"])
        client.portal.call(socket.click, followup, "approve", "U123", "approve")
        state = client.get("/api/session").json()
        case, = state["cases"]
        assert case["status"] == "resolved"
        assert state["scenarios"]["offboarding"]["evidence"]["grant_status"] == "revoked"
        assert all(action["verified_at"] for action in case["actions"])
        assert sum(event["data"].get("source") == "slack" for event in case["events"] if event["data"]) == 2
        assert slack.posts[-1]["thread_ts"] == root["ts"]

        reset = start(client)
        client.portal.call(socket.click, root, "retire", "U123", "old-case")
        assert client.get("/api/session").json()["cases"] == reset["cases"] == []
