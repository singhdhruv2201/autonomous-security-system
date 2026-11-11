# Awasi Slack App Setup

Awasi uses Slack as an interactive human-in-the-loop adapter for security case decisions and approvals. Decisions are authorized and recorded by the backend; Slack delivers questions and captures button clicks.

---

## 1. App Manifest

Create a new Slack App using the manifest below in your Slack workspace (e.g. `https://api.slack.com/apps` -> **Create New App** -> **From an app manifest**):

```json
{
  "display_information": {
    "name": "Awasi Security",
    "description": "Autonomous security remediation agent",
    "background_color": "#1f2937"
  },
  "features": {
    "bot_user": {
      "display_name": "Awasi",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "chat:write",
        "im:write"
      ]
    }
  },
  "settings": {
    "interactivity": {
      "is_enabled": true
    },
    "socket_mode_enabled": true,
    "org_deploy_enabled": false
  }
}
```

---

## 2. Tokens & Credentials

1. **Install App to Workspace**:
   - Go to **Install App** and click **Install to Workspace**.
   - Copy the **Bot User OAuth Token** (`xoxb-...`).

2. **Generate App-Level Token (Socket Mode)**:
   - Go to **Basic Information** -> **App-Level Tokens** -> **Generate Token and Scopes**.
   - Token Name: `awasi-socket-mode`
   - Scopes: Add `connections:write`.
   - Copy the **App-Level Token** (`xapp-...`).

3. **Invite Bot to Channel**:
   - In Slack, open the channel designated for Awasi alerts (e.g. `#awasi`).
   - Run `/invite @Awasi` to add the bot to the channel.
   - Copy the channel ID (e.g. `C0CBMP5UPRT` from the channel details or URL).

4. **Copy Operator User ID**:
   - In Slack, open your user profile -> click **More (...)** -> **Copy member ID** (`U...`).

---

## 3. Configuration (.env)

Add the four variables to `.env` in the repository root:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
AWASI_SLACK_CHANNEL=C0CBMP5UPRT
AWASI_SLACK_DEFAULT_USER=U...
```

*(Optional)* For multi-user role mapping, set `AWASI_SLACK_USERS` to a JSON dictionary mapping Slack member IDs to Helio employee IDs:
```bash
AWASI_SLACK_USERS='{"U12345": "emp-hannah-reed", "U67890": "emp-casey-wong"}'
```
When `AWASI_SLACK_USERS` is empty or unset, Awasi runs in **Solo Operator Mode**.

---

## 4. Solo Operator Mode Behavior

In Solo Operator Mode:
- Every case question is posted to `AWASI_SLACK_CHANNEL` in a dedicated thread per case.
- The root message mentions `@AWASI_SLACK_DEFAULT_USER` to trigger a Slack notification.
- Each message begins with a `To: {Name} ({Title})` line stating the addressed Helio role:
  - Phase 1 lead triage: `To: Hannah Reed (Sales Lead)`
  - Phase 1 CTO approval: `To: Casey Wong (CTO)`
- Clicks by `AWASI_SLACK_DEFAULT_USER` are accepted and recorded under the addressed Helio role.
- Clicks by any unauthorized or unmapped user receive an ephemeral rejection message and do not modify case state.

---

## 5. Thread Lifecycle & Button Transitions

- **First Post**: Describes the discovery, verified pause action (if taken), and provides buttons:
  - `Retire it`: Transitions case to CTO approval for permanent grant revocation.
  - `Keep it, needs a new owner`: Keeps agent paused and marks case `needs_review`.
  - `Undo pause`: Restores agent access in CRM and sets a persistent override so it is not immediately re-paused.
- **CTO Follow-up**: If `Retire it` is chosen, a follow-up posts in the same thread addressed to Casey Wong asking to approve `crm.revoke_grant` with buttons `Approve revocation` and `Reject revocation`.
- **Button Removal**: When a button is clicked and acknowledged, the message is updated in place, removing the buttons and appending `*Decision: {Choice} by {Name}*`.
- **Replay Safety**: If a notification node re-runs, durable outbox checks prevent button resurrection.
- **Verified State**: Lead messages describe the last verified pause, a verified Undo,
  or explicitly unverified access. An attempted or undone pause is not presented as
  proof that CRM access is currently disabled.

---

## 6. Delivery Guarantees & Limitations

- **Outbox Pattern**: Outgoing Slack messages are staged in the SQLite `slack_messages` table.
- **Unknown Delivery Limitation**: If Slack's response to a new root message post is ambiguous (e.g. network timeout or connection reset before HTTP response), the message is marked with status `unknown` and is not blindly retried, avoiding duplicate channel notifications.
- **Known-Message Updates**: Message updates (`chat_update`) with known timestamps are safely retried.
- **Closed-Case Recovery**: Closing a case does not stop recovery of an unfinished
  outcome report. The worker resumes its saved checkpoint after restart; the
  existing outbox key prevents duplicate enqueueing. Reporting completion means
  the notification was queued, not that Slack has delivered it.
- **Startup Outages**: Socket Mode connects and retries alongside the case worker.
  A pending connection does not block case processing or worker heartbeats. On
  shutdown, the pending connection task is cancelled and the transport is closed.
- **Monitoring**: `/overview` reports `slack.status`:
  - `not_configured`: Slack tokens are missing; fallback `LogNotifier` is in use.
  - `connected`: Active Socket Mode connection with fresh heartbeat (<60s).
  - `unavailable`: Stale heartbeat or lost connection.
  - `pending` and `unknown`: Count of queued and ambiguous messages.

---

## 7. Testing

### Automated Unit & Transport Tests
Run tests with fake Slack clients:
```bash
.venv/bin/python -m pytest -q tests/test_slack_app.py tests/test_slack_recovery.py tests/test_platform_api.py
```
This tests thread grouping, role mapping, authorization, idempotency, button updates,
SQLite checkpoint recovery, and startup outages without contacting Slack.
