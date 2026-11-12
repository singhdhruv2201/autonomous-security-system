# Phase 3 operation and validation

The worker uses two managed MCP subprocesses: four read tools for the model, and
CRM action tools used only by the deterministic workflow. Lost mutation responses
trigger a read-back check. An unverified outcome remains `needs_review`; it is not
reported as a successful fix or blindly repeated.

Read responses are streamed with a 64 KiB ceiling and projected onto observation
metadata. Unknown customer fields and free-form notes are excluded. Assessment
text and trace identifiers are scrubbed before storage; raw model responses are
not persisted. This does not retroactively sanitize older database rows, nor is
pattern-based redaction a general-purpose DLP classifier.

## Model configuration and spending

For live use, configure `ANTHROPIC_API_KEY` privately and set `AWASI_MODEL` to the
supported model you intend to use. The existing default is `claude-haiku-5-5`.
Do not put keys in commands, documentation, tests, or source control.

Each investigation permits at most four model requests and six read-tool calls,
with a 60-second overall deadline. Model retries are disabled. Each request is
bounded to 80,000 estimated input tokens (conservative UTF-8 size plus formatting
allowance) and 2,048 output tokens. Missing credentials, limits, malformed output,
or provider errors produce the scripted fallback.

To apply the persistent demo allowance to the worker, set `AWASI_LIVE_LEDGER` to
the existing ledger's absolute path. The opt-in live test uses
`data/live_test_ledger.json`. Native worker walkthroughs should use that same
file. Docker paths refer to the container: use a file under the persistent `/data`
volume, with reconciled existing accounting carried over before the walkthrough.
Independent files do **not** share an allowance. Leaving `AWASI_LIVE_LEDGER` unset
means the worker has per-investigation limits but no cumulative dollar limit.

The default ledger ceiling is $1.00 and ten requests. Reservations cover the
actual messages, tool schemas, and maximum output before a request is sent.
Concurrent processes serialize reservations. Unknown token usage stays unknown
and retains the full reserved cost; interrupted calls are never treated as free.
Restarting does not release pending reservations or increase the stored ceilings.
These are conservative application estimates, not a provider billing statement.

Legacy accounting is preserved but blocks new reservations. Earlier failed calls
used invented token counts, so remaining allowance needs reconciliation against
provider usage before another paid run. Do not delete the ledger, reset totals,
or switch to a new path to bypass this. No migration or paid call was performed
as part of these fixes.

## Verification

Run the standard tests without any paid API requests:

```sh
.venv/bin/python -m pytest -q
```

Run database integration coverage with only Neo4j started, keeping the agent and
Slack services stopped:

```sh
docker compose up -d neo4j
make test-integration
docker compose stop neo4j
```

Wait for Neo4j to become healthy before running the integration command. Tests
use isolated company data and temporary SQLite files. No volume reset is needed.

The normal suite skips the paid test even if credentials exist. The live test is
explicitly opt-in with `--live` and uses fixture read tools; it does not prove the
complete Slack walkthrough. Final live acceptance still needs a real case with
a model-written Slack summary, stored traces, and the missing-key fallback path.
Resolve the historical accounting before doing that walkthrough.
