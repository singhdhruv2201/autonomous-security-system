# Model operation and validation

The case worker manages two MCP subprocesses: observation tools for the model,
and action tools reserved for the deterministic workflow. The six observation
tools read findings, entities, neighbors, CRM activity, Workbench events, and
public-app reachability. The public probe records an anonymous view even though
it does not change app configuration.

Read responses have a 64 KiB ceiling and are projected onto observation metadata.
Unknown customer fields and free-form notes are excluded. Assessment text and
trace identifiers are scrubbed; raw model responses are not persisted. Pattern
redaction is not a general-purpose DLP guarantee and does not sanitize old data
retroactively.

## Investigation and notification behavior

Each investigation permits at most four model requests and six evidence-tool
calls, with a 60-second overall deadline. The last two model turns are reserved
for completion and possible correction. Excess evidence calls return unavailable
results, allowing an assessment to disclose its gaps rather than aborting it.
Provider-level automatic retries are disabled.

Each model request is bounded to 80,000 estimated input tokens (conservative
UTF-8 size plus formatting allowance) and 2,048 output tokens. Evidence references
must come from successful tool results. Tool availability never grants mutation
authority to the model.

The model writes a technical assessment and short Slack headline/context/impact.
Missing, malformed, or ungrounded message copy may receive one automatic wording
correction within the same four-request ceiling. If that correction fails, an
already validated investigation is retained and the notification uses its fixed
template. Without a valid investigation, the scripted fallback is explicitly
recorded. Limit errors preserve a sanitized reason.

Saved messages are reused across clicks and restarts. Buttons, recipients,
approvals, deadlines, and action status are controlled by code. An uncertain
mutation requires read-back; it is not reported as resolved or blindly repeated.
See [Slack operation](slack-setup.md) for the notification contract.

## Configuration and spending

Set `ANTHROPIC_API_KEY` privately and use a model supported by the project's
accounting in `AWASI_MODEL`. The demo default is `claude-haiku-5-5`. Do not put
credentials in commands, screenshots, documentation, or source control.

| Mode | Ledger and configured ceiling |
| --- | --- |
| Showcase live mode | `showcase/data/anthropic-ledger.json`; $0.50 / 40 requests |
| Isolated acceptance stack and Phase 7 evaluation | `data/phase7_live_ledger.json`, mounted as `/budget/phase7_live_ledger.json` for the worker; defaults $0.50 / 40 requests |
| Normal case worker | No cumulative cap unless `AWASI_LIVE_LEDGER` is configured; configured defaults are $1.00 / 10 requests |
| Opt-in live unit test | `data/live_test_ledger.json`; separate accounting from the above |

Independent ledger files do **not** share an allowance. Configure a persistent
container path and mount before enabling a live worker. Per-investigation limits
remain enforced even when no cumulative ledger is configured.

Reservations cover the outgoing context, schemas, and maximum output before a
provider call. Concurrent reservations serialize; unknown usage retains a
conservative charge. Restarting does not release reservations or increase stored
ceilings. Estimates are not provider invoices. Legacy unreconciled accounting
blocks further reservations; preserve history and reconcile it rather than
resetting totals, deleting the ledger, or choosing a new file to bypass a cap.

## Verification

`make check` runs unpaid checks. `make test-integration` adds real Neo4j coverage
when the local database is running. The paid test is opt-in with `--live`; ordinary
tests skip it even if credentials exist. `make eval` is also paid and tests compact
assessment fixtures, not the entire live Slack/tool-selection loop.

The [archived Phase 7 results](archive/awasi-v1-plan.md#phase-7-results) describe a
specific live walkthrough. Subsequent message-recovery work also delivered a fresh
model-written Slack message automatically. These are demo observations, not
statistical model-accuracy or service-availability guarantees. New live tests
require explicit scope, a preserved budget, and separately recorded results.
