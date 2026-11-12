# Awasi Product Showcase

Run from the repository root using the Python environment described in the
[project README](../README.md).

```sh
PYTHONPATH=src .venv/bin/python -m showcase.app
```

Open [http://127.0.0.1:3070](http://127.0.0.1:3070) and click **Start baseline**.
The showcase binds to loopback. Docker and the existing Awasi stack are optional.

| Scenario | What to demonstrate |
| --- | --- |
| Orphaned agent | Offboard Maya Chen. Inspect her agent’s CRM grant and the verified pause. Act as Hannah to retire the agent, then as Casey to approve revoking the grant. |
| Public internal app | Publish Renewal Tracker. Act as Luca to make it company-only. Inspect the verification of the anonymous URL and confirm the restriction. |
| Anonymous app access | Enable **Include anonymous access** before running the public scenario. Workbench records one anonymous view and Awasi restricts the app before asking Luca. |
| Unapproved destination | Run Lead Enrichment. Inspect the six synthetic records counted at the local receiver and the verified pause. Act as Casey to disable the connection and resume the agent. The receiver’s historical count stays at six. |

Select a graph node for its source identifier. Expand **Policy and evidence** for
the rule and current observed fields. The timeline can show case events, source
events, and technical traces with expandable details. Select a metric to plot its
values at each step. An observed finding can clear after containment while the
case still awaits a decision.

**New baseline** replaces the current browser session’s environment. Each session
seeds fresh Directory, CRM, Workbench, outside receiver, findings, cases, and
workflow checkpoint databases in its own temporary directory. The showcase runs
the existing HTTP handlers, observation worker, policy predicates, LangGraph
workflow, and playbooks. Its access graph uses the repository’s normalized
snapshots and an in-memory projection. Human decisions use the browser or Slack.
The server’s shutdown removes its temporary session data.

## Slack alerts

With `SLACK_BOT_TOKEN` and `AWASI_SLACK_CHANNEL` configured in the shell or the
repo's `.env`, **Run scenario** sends an **Awasi Showcase** alert for the case.
The bot must be in the configured channel. The alert includes the validated
Anthropic assessment and labels it as live or recorded.

Set `SLACK_APP_TOKEN` and `AWASI_SLACK_DEFAULT_USER` (or a role mapping via
`AWASI_SLACK_USERS`) to enable the same Slack buttons as the regular worker.
An authorized click advances its showcase case. Verified outcomes and subsequent
approval prompts appear in the same thread, and the browser refreshes as the case
progresses. Messages sent before enabling the buttons stay as posted, so start
a new baseline and run a new scenario.

Slack can deliver a click to any connected listener for the app. If the regular
`awasi-agent` is listening on the same Slack app, stop that worker during the
showcase or use a dedicated Slack app for the demo. See the
[bot and Socket Mode setup](../docs/slack-setup.md) and
[Slack's connection routing](https://docs.slack.dev/apis/events-api/using-socket-mode/#using-multiple-connections).

The case panel shows whether Slack confirmed the initial post. If delivery is
unconfirmed, check the showcase terminal for a Slack rejection code and check
the channel before replaying the scenario. For a browser-only demo, add
`--no-slack`.

## Anthropic output

When Slack alerts and `ANTHROPIC_API_KEY` are configured, running a scenario calls
Anthropic on the current session's synthetic evidence. The browser shows the
validated assessment and available tool traces. Slack shows the summary, impact
and confidence alongside the verified action and available decisions. Model
requests incur charges and use the showcase ledger in `showcase/data/`, with a
cumulative allowance of $0.50 estimated spend and 40 requests.

Add `--recorded-model` to use recorded Anthropic assessments from the
2026-11-12 Helio walkthrough. The showcase also labels and uses those recordings
when live Anthropic is unavailable. The anonymous-access variant uses the
related public-sharing recording. Use `--live-model` to require an API key and
enable live calls even with `--no-slack`. The model name comes from `AWASI_MODEL`
when set.

## Tests

```sh
PYTHONPATH=.:src .venv/bin/python -m pytest -q showcase/tests
node --check showcase/static/showcase.js
```

The integration tests use seeded temporary databases and the actual service
handlers. On executors that prohibit loopback sockets, they use ASGI transport
for the same HTTP requests. They exercise reset, browser isolation, all scenario
paths, human decisions and verified action outcomes.
