# Phase 2 handoff — 2026-11-10

## Current request and authorization

The user requested a quick handoff because their five-hour credits are almost exhausted. Stop implementation after writing this handoff. Resume Phase 2 when the user asks.

Project: `/Users/singhdhruv/Documents/development/awasi`
Branch: `main`
HEAD: `9238e9e Apply Phase 1 review fixes for durable actions, recovery, and transitions`
Canonical plan: `docs/awasi-v1-plan.md`, Phase 2 (Slack).
Follow `AGENTS.md`: TDD, confirm focused failures before implementation, integration tests across HTTP/database boundaries, report only executed checks. Do not spawn subagents unless explicitly requested. No new dependencies needed.

The user configured the Slack app and explicitly authorized a live test: **“Yes, post the demo case”**, answering whether we may post a clearly labeled Awasi demo case in the configured channel so they can test buttons. This permission persists; do not ask again for the same test. No live messages have been posted yet. Do not send unrelated messages.

## Slack setup (no secrets in this document)

- Workspace: https://helio-awasi.slack.com/
- Channel: https://helio-awasi.slack.com/archives/C0CBMP5UPRT
- All four local `.env` variables are present: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `AWASI_SLACK_CHANNEL`, `AWASI_SLACK_DEFAULT_USER`.
- Bot `auth.test` succeeded and its workspace URL matched the expected workspace.
- App-level token, Socket Mode connection, channel membership and real button delivery have NOT yet been tested.
- User was supplied a manifest with bot scopes `chat:write`, `im:write`, Socket Mode enabled and interactivity enabled. App-level token needs `connections:write`.
- Never print tokens, `.env`, expanded Compose configuration, or Socket Mode connection URLs. Parse values locally and report only sanitized status. Fake tokens in test failures are generated test data.
- Use our app's SDK and credentials to test the integration; a connected Slack connector would not exercise this specific bot.

## Phase 1 baseline (already committed)

Phase 1 review fixes are in HEAD. Previous validation: standard pytest **544 passed, 17 skipped**; `make test-integration` with Neo4j **561 passed, no skips**. These counts are BEFORE the current Phase 2 edits, not a claim that today's working tree passes.

Relevant modules:
- `actions.py`: saves before-state and dispatch marker before mutations, reconciles after crash, never blindly repeats ambiguous actions; handles legacy pending actions conservatively.
- `workflow.py`: explicit retire → Casey approval → grant revocation → verification; keep/reject/timeout require review. Undo uses saved state and a durable action, sets a persistent finding override.
- `agent_worker.py`: checkpointed decision receipts, retryable delivery, durable timeouts, recovery before first checkpoint and during notification.
- `cases.py`: cases/actions/decisions/events, overrides.
- Tests `test_case_recovery.py`, `test_case_crm_integration.py` provide useful real CRM HTTP and SQLite checkpoint fixtures.

The previous turn stopped Neo4j after integration testing. This turn has not started Docker services or reset any demo data. Do not reset volumes merely to test Phase 2.

## Uncommitted work at handoff

Exactly these Phase 2 files were changed/created before this handoff:

1. **Modified `src/helio_security/cases.py`**
   - Added additive `case_questions` table (one current question per case).
   - `offer_question(case_id, question, now=...)`: stores whitelisted question fields and updates waiting status/addressee/deadline transactionally; replay does not resurrect an answered question.
   - `question(case_id)` and `decision(case_id, key)` read helpers.
   - `submit_decision(company_id, case_id, key, choice, employee_id, source, now, actor_id=None)`: atomic validation of company, waiting status, current key, offered choice, employee, deadline and duplicate/conflicting answer; records decision and `decision_received` event.
   - `get_case_full` now includes `question`.
   - These additions have NOT yet been tested after implementation. Existing raw `record_decision` remains for internal worker timeouts and existing tests. API and workflow do NOT yet call the new methods.

2. **New `src/helio_security/slack_store.py`**
   - Durable `slack_messages` outbox, keyed by `(case_id, key)` with channel/ts, body, revisions, delivered revision, status and retry time.
   - Methods: `recover`, `enqueue`, `message`, `root`, `pending`, `claim`, `sent`, `failed`, `status`, `questions_to_refresh`.
   - `recover`: interrupted new posts become `unknown`; interrupted updates with known ts become queued.
   - Atomic claim protects duplicate sends; revision tracking preserves updates arriving during a send.
   - Uncertain new posts are deliberately not blindly retried. No history-reading scope was requested; do not claim exactly-once delivery across a lost Slack acknowledgment. Document this limitation and expose unknown delivery count.
   - This file is an unverified first implementation. Review it before relying on it.

3. **New `tests/test_slack_app.py`**
   - Fake Slack client; expected interfaces `SlackConfig`, `SlackNotifier`, `SlackActions`, `LogNotifier` from `helio_security.slack_app`.
   - Tests cover restart/message idempotency, one thread per case, first-post mention, Helio role prefixes, acknowledgment before decision processing, durable decisions, authorization, wrong workspace/channel/message, stale key, unoffered choice, closed case, malformed payload, double click, failed message update, no button resurrection, uncertain post suppression, safe rendering and missing configuration.
   - `setup` fixture demonstrates expected constructors and `offer_question` shape.
   - **`src/helio_security/slack_app.py` does not exist yet.**

4. **Modified `tests/test_platform_api.py`**
   - Added `test_decision_api_refuses_unoffered_choices_and_closed_cases`.
   - Added `test_overview_reports_slack_configuration_and_delivery`.
   - Existing API happy-path test currently opens a case and posts a decision without offering a question. Once API uses validated submission, update that fixture to offer its question first; do not weaken the new validation.

5. This handoff document.

No Phase 2 commit or push has been made.

## TDD results already observed

- `pytest -q tests/test_slack_app.py`: expected collection failure, `ModuleNotFoundError: helio_security.slack_app`.
- `pytest -q tests/test_platform_api.py -k 'unoffered or slack_configuration'`: **2 expected failures**:
  - API accepts an unoffered decision with 200 instead of 409.
  - `/overview` has no `slack` field.
- No green Phase 2 run yet. No ongoing tool processes need polling.

## Next implementation steps

### 1. Finish backend question binding

- In `workflow.notify_human`, call `store.offer_question` with the existing question + deadline instead of independently changing waiting status. Existing event creation remains useful for timeline and deadline preservation.
- Include safe facts/context in the notifier payload so it can render counts and the specific scenario; do not pass model prose or customer values into Slack verbatim.
- API `/cases/{id}/decisions`: use `submit_decision` with authenticated company and employee `operator`; turn validation errors into 409. Preserve 404 for foreign/missing cases and existing viewer restrictions.
- Current question keys: `lead_decision`; retirement approval `approve:crm.revoke_grant`.
- Case close status must still invalidate clicks even if an old Slack message remains visible.

### 2. Implement `slack_app.py` against the tests

Expected design (not yet implemented):

- `SlackConfig` dataclass with hidden repr for secrets; fields `bot_token`, `app_token`, `channel`, `default_user`, optional employee-user mapping. `from_environment(mapping=None)` and `.configured`. Missing settings use `LogNotifier`. Validate IDs without exposing values in errors. Optional mapping is secondary to solo operator mode.
- `LogNotifier` writes safe timeline notification information when Slack is absent.
- `SlackNotifier(store, client, config)` uses `SlackStore`:
  - `notify(case_id, key, payload)` durably queues a safely rendered message; does not block workflow on Slack availability.
  - `flush()` delivers queued rows, root first and later posts with root `thread_ts`; known `ts` uses `chat_update`.
  - Preserve stable client message identifiers if supported, but don't rely on them for a guarantee Slack doesn't provide.
  - If a new post's result is ambiguous, mark unknown rather than reposting. Known-message updates can safely retry. Explicit API rejection/rate limiting can be retried with delay.
  - Message refresh consults the durable decision and case status, strips buttons, appends Helio actor/time. Workflow replay must not bring buttons back.
  - `delivery_status()` returns pending/unknown counts.
  - Serialize flushes and use outbox claims; no network calls inside SQLite transactions.
- `SlackActions(store, notifier, config, team_id=..., wake=...)`:
  - `handle(ack=..., body=...)` must await `ack()` first.
  - Parse only `{case_id,key,choice}` from value. Validate workspace, configured channel, persisted message ts, action ID, current backend question and offered choice.
  - Default Slack user acts as current addressed Helio employee; mapped users only act for their own mapped addressed role.
  - Persist via `submit_decision`, then wake worker and update the message. Failed update must not lose the accepted decision.
  - Unauthorized/stale/malformed clicks: ephemeral explanation, no case changes.
- Build a Bolt `AsyncApp` and register `awasi_choice` / `awasi_undo` handlers. Socket Mode is the transport. Never let Slack invoke CRM playbooks directly.

Rendering cautions:
- Start with `To: Hannah Reed (Sales Lead)` or `To: Casey Wong (CTO)`; first message mentions configured default user.
- One brief paragraph covering discovery + verified action + one question. Counts/IDs only, no arbitrary summary or record values.
- Only claim pause/revocation/Undo when action ledger + verification supports it. The worker currently hard-codes dependents=0; do not confidently claim independent dependency analysis occurred. Say dependency impact wasn't independently verified if needed.
- Existing tests deliberately supply hostile/secret-looking summary strings; ignore them when rendering.
- Slack action IDs must be unique within a block. If retaining exact IDs `awasi_choice` and `awasi_undo`, put each choice in its own actions block with distinct block IDs (or verify a compliant alternative).
- Avoid repeated confirmation text when refreshing an already-confirmed message; retain a stable original text/body and derive current display from durable decision state.

### 3. Runtime wiring and outcome delivery

- `agent_worker.main` currently instantiates a nested `StdoutNotifier`, builds worker, then runs an event loop. Replace with config-driven notifier + async Bolt Socket Mode lifecycle alongside worker.
- Worker should wake promptly on clicks (asyncio event with thread-safe wake if necessary), while preserving exclusive process lock and per-case locks.
- Flush outbox independently enough that graph verification waits don't prevent Slack acknowledgments. Socket Mode must remain on the same live event loop.
- Add delivery/runtime heartbeat (e.g. `platform_store.set_runtime(company,'slack',...)`) and `/overview.slack`: missing → `not_configured`, fresh connected state → `connected`, stale heartbeat → `unavailable`; expose delivery failures.
- Send final outcome into the same case thread, including needs-review and Undo. Existing graph early exits after failed action/verification need an outcome notification path too.
- Configure Compose `awasi-agent` with optional Slack environment variables and optional user mapping. Never use expanded `docker compose config` where secrets could print; `--quiet` is safe.

### 4. Tests and docs

- Get initial tests green, then add meaningful failure/regression coverage: concurrent clicks, conflicting duplicate, question change, timeout race, optional mapped users, app handler registration/Socket Mode lifecycle, startup recovery, outbox claim/revision behavior, known-message retry, definite rejection/rate limit, outcome delivery and fallback.
- Add real CRM/SQLite workflow tests driven through fake Slack handlers (reuse `test_case_crm_integration.environment`). Cover retire → CTO approval, keep, Undo + next-case human override, restart while waiting.
- Check backend API changes against all existing tests and update only fixtures/expectations legitimately affected by offered-question validation.
- Write `docs/slack-setup.md` and manifest file; include scopes, app installation, invitation, environment variables, solo operator behavior, troubleshooting, unknown-delivery limitation, and testing steps.
- Update canonical plan with honest results. Phase 2 is not complete yet.
- Run `.venv/bin/python -m pytest -q`, `make test-integration` with Neo4j if feasible, Compose quiet validation. UI code need not change.

### 5. Authorized live walkthrough

Only after automated checks pass:
- Validate app-level token/Socket Mode without printing returned WebSocket URL.
- Post a clearly labeled demo case to the configured channel. Prefer an isolated demo CRM/database so testing irreversible retirement doesn't corrupt existing demo state.
- Ask user to click buttons once an actual message exists. Observe server-side receipt, case progression, Slack updates and verified CRM effects. A post succeeding is not proof buttons work.
- Do not reset Docker volumes or revoke unrelated live credentials. No real customer systems are involved in this repo; still keep demo tests clearly labeled.
- Report manual checks separately; don't claim live click coverage if user hasn't clicked.

## Reference documentation already checked

- https://docs.slack.dev/tools/bolt-python/concepts/actions/
- https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/
- https://docs.slack.dev/reference/methods/chat.postMessage/
- https://docs.slack.dev/reference/app-manifest/
- https://docs.slack.dev/reference/scopes/connections.write/

Installed libraries already include `slack_bolt`, `slack_sdk`, `aiohttp`.
`AsyncApp`, `AsyncSocketModeHandler`, and `AsyncWebClient.chat_postMessage` signatures were inspected locally. `chat_postMessage` accepts `**kwargs`; Socket Mode handler accepts `app`, `app_token`, optional `web_client`. Verify connection-state method signatures locally before using them.
