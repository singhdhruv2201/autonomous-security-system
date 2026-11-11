"""Slack transport, rendering, and action handlers for Awasi cases."""
import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional


def get_ssl_context():
    try:
        import certifi
        import ssl
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

try:
    from slack_sdk.errors import SlackApiError
except ImportError:
    SlackApiError = None

from .cases import CaseStore
from .slack_store import SlackStore

log = logging.getLogger(__name__)

EMPLOYEE_DIRECTORY = {
    "emp-hannah-reed": {"name": "Hannah Reed", "title": "Sales Lead"},
    "emp-casey-wong": {"name": "Casey Wong", "title": "CTO"},
    "emp-maya-chen": {"name": "Maya Chen", "title": "Account Executive"},
}


def format_role(employee_id: str) -> str:
    info = EMPLOYEE_DIRECTORY.get(employee_id)
    if info:
        return f"{info['name']} ({info['title']})"
    return employee_id


def format_name(employee_id: str) -> str:
    info = EMPLOYEE_DIRECTORY.get(employee_id)
    if info:
        return info["name"]
    return employee_id


@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    channel: str = ""
    default_user: str = ""
    user_mapping: Optional[Dict[str, str]] = None

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.app_token and self.channel and self.default_user)

    @classmethod
    def from_environment(
        cls, env: Optional[Dict[str, str]] = None, mapping: Optional[Dict[str, str]] = None
    ) -> "SlackConfig":
        source = os.environ if env is None else env
        user_mapping = mapping
        if user_mapping is None:
            raw_map = source.get("AWASI_SLACK_USERS", "")
            if raw_map:
                try:
                    user_mapping = json.loads(raw_map)
                except Exception:
                    user_mapping = {}
        return cls(
            bot_token=source.get("SLACK_BOT_TOKEN", ""),
            app_token=source.get("SLACK_APP_TOKEN", ""),
            channel=source.get("AWASI_SLACK_CHANNEL", ""),
            default_user=source.get("AWASI_SLACK_DEFAULT_USER", ""),
            user_mapping=user_mapping,
        )

    def __repr__(self) -> str:
        return f"SlackConfig(channel={self.channel!r}, default_user={self.default_user!r}, configured={self.configured})"


class LogNotifier:
    """Safe fallback notifier when Slack is not configured."""

    async def notify(self, case_id: str, key: str, payload: dict):
        log.info("CASE %s [%s]: %s", case_id, key, payload.get("key"))

    async def flush(self):
        pass


class SlackNotifier:
    """Delivers case notifications and updates into Slack threads."""

    def __init__(self, cases: CaseStore, client: Any, config: SlackConfig):
        self.cases = cases
        self.client = client
        self.config = config
        self.slack_store = SlackStore(cases)
        self.slack_store.recover()

    def delivery_status(self) -> dict:
        return self.slack_store.status()

    def render_body(self, case_id: str, key: str, payload: dict) -> dict:
        facts = payload.get("facts", {})
        record_count = facts.get("record_count", 6)
        addressed_to = payload.get("addressed_to", "emp-hannah-reed")

        root_msg = self.slack_store.root(case_id)
        is_root = root_msg is None or root_msg.get("key") == key

        actions = self.cases.get_actions(case_id)
        pause = next((a for a in reversed(actions)
                      if a["playbook"] == "crm.pause_principal" and not a.get("undo_of")), None)
        undo = next((a for a in reversed(actions) if pause and a.get("undo_of") == pause["id"]), None)
        has_verified_pause = bool(pause and pause["status"] == "executed"
                                  and pause.get("verified_at") is not None and not undo)

        decision = self.cases.decision(case_id, key)

        if key == "lead_decision":
            header_target = format_role(addressed_to)
            mention = f" <@{self.config.default_user}>" if is_root and self.config.default_user else ""
            header = f"To: {header_target}{mention}"
            if undo and undo["status"] == "executed" and undo.get("verified_at") is not None:
                action_text = "The pause was undone. Restored CRM access was verified."
            elif pause and pause["status"] == "undone":
                action_text = "The pause was undone; current CRM access has not been verified."
            elif undo:
                action_text = "Undo was attempted; current CRM access has not been verified."
            elif has_verified_pause:
                action_text = "I paused its CRM access. Last verified CRM state: disabled."
            else:
                action_text = "A pause of CRM access has not been verified."
            body_text = (
                f"{header}\nMaya Chen's Sales Research Agent retained customer database access "
                f"({record_count} customers) after its owner became inactive. {action_text} "
                "Dependency impact has not been independently verified. Does Sales still need it?"
            )
        elif key.startswith("approve:"):
            header_target = format_role(addressed_to)
            header = f"To: {header_target}"
            target_playbook = key[len("approve:"):]
            lead_dec = self.cases.decision(case_id, "lead_decision")
            if lead_dec and lead_dec.get("choice") == "retire":
                requester_name = format_name(lead_dec.get("employee_id", "emp-hannah-reed"))
                requester_line = f"{requester_name} requested to retire the Sales Research Agent.\n"
            else:
                requester_line = ""

            if "pause" in target_playbook:
                action_desc = f"Please approve pause of Sales Research Agent CRM access on the customer database ({record_count} customers)."
            elif "revoke" in target_playbook:
                if requester_line:
                    action_desc = f"Please approve revoking its CRM grant on the customer database ({record_count} customers)."
                else:
                    action_desc = f"Please approve revoking the Sales Research Agent CRM grant on the customer database ({record_count} customers)."
            else:
                action_desc = f"Please approve {target_playbook} for the Sales Research Agent ({record_count} customers)."

            body_text = f"{header}\n{requester_line}{action_desc}"
        elif key == "outcome":
            case = self.cases.get_case(case_id)
            status = case.get("status") if case else "resolved"
            actions = self.cases.get_actions(case_id)

            verified_undo = next((a for a in actions if a.get("undo_of") and a.get("status") == "executed" and a.get("verified_at")), None)
            unverified_undo = next((a for a in actions if a.get("undo_of") and not (a.get("status") == "executed" and a.get("verified_at"))), None)

            verified_revoke = next((a for a in actions if a.get("playbook") == "crm.revoke_grant" and a.get("status") == "executed" and a.get("verified_at")), None)
            unverified_revoke = next((a for a in actions if a.get("playbook") == "crm.revoke_grant" and not (a.get("status") == "executed" and a.get("verified_at"))), None)

            verified_pause = next((a for a in actions if a.get("playbook") == "crm.pause_principal" and a.get("status") == "executed" and a.get("verified_at")), None)
            unverified_pause = next((a for a in actions if a.get("playbook") == "crm.pause_principal" and not (a.get("status") == "executed" and a.get("verified_at"))), None)

            if verified_undo:
                body_text = "Case requires review: Sales Research Agent CRM access restored (Undo verified)."
            elif unverified_undo:
                body_text = "Case requires review: Undo action could not be verified."
            elif verified_revoke:
                body_text = "Case resolved: Sales Research Agent CRM grant revoked."
            elif unverified_revoke:
                body_text = "Case requires review: Revocation could not be verified."
            elif verified_pause:
                if status == "resolved":
                    body_text = "Case resolved: Sales Research Agent CRM access paused."
                else:
                    body_text = "Case requires review: Sales Research Agent CRM access remains paused."
            elif unverified_pause:
                body_text = "Case requires review: Pause action could not be verified."
            elif status == "resolved":
                body_text = "Case resolved."
            elif status == "needs_review":
                body_text = "Case requires review: Action could not be verified or human override requested."
            else:
                body_text = f"Case concluded with status: {status}."
        else:
            header_target = format_role(addressed_to)
            body_text = f"To: {header_target}\nSecurity decision required ({record_count} customers)."

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body_text}}]

        if decision:
            choice = decision["choice"]
            actor_name = format_name(decision["employee_id"])
            confirmation = f"\n\n*Decision: {choice.title()} by {actor_name}*"
            body_text += confirmation
            blocks[0]["text"]["text"] = body_text
        elif key != "outcome" and payload.get("choices"):
            for choice in payload["choices"]:
                if key.startswith("approve:"):
                    target_playbook = key[len("approve:"):]
                    action_noun = "pause" if "pause" in target_playbook else "revocation"
                    if choice == "approve":
                        label = f"Approve {action_noun}"
                    elif choice == "reject":
                        label = f"Reject {action_noun}"
                    else:
                        label = choice.title()
                else:
                    label = {
                        "retire": "Retire it",
                        "keep": "Keep it, needs a new owner",
                        "undo": "Undo pause",
                        "approve": "Approve revocation",
                        "reject": "Reject revocation",
                    }.get(choice, choice.title())
                btn_action_id = "awasi_undo" if choice == "undo" else "awasi_choice"
                blocks.append({
                    "type": "actions",
                    "block_id": f"actions_{choice}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": btn_action_id,
                            "value": json.dumps({"case_id": case_id, "key": key, "choice": choice}),
                        }
                    ],
                })

        return {"text": body_text, "blocks": blocks}

    async def notify(self, case_id: str, key: str, payload: dict):
        body = self.render_body(case_id, key, payload)
        self.slack_store.enqueue(case_id, key, self.config.channel, body)

    async def flush(self):
        now = time.time()
        for row in self.slack_store.questions_to_refresh():
            body = json.loads(row["body"])
            has_actions = any(b.get("type") == "actions" for b in body.get("blocks", []))
            if has_actions:
                q = self.cases.question(row["case_id"])
                updated_body = self.render_body(row["case_id"], row["key"], q or {})
                self.slack_store.enqueue(row["case_id"], row["key"], row["channel"], updated_body)

        pending = self.slack_store.pending(now)
        for row in pending:
            if not self.slack_store.claim(row):
                continue
            body = json.loads(row["body"])
            if row.get("ts"):
                try:
                    await self.client.chat_update(channel=row["channel"], ts=row["ts"], **body)
                    self.slack_store.sent(row, row["ts"])
                except Exception as err:
                    status_code = getattr(getattr(err, "response", None), "status_code", None)
                    if SlackApiError and isinstance(err, SlackApiError) and status_code == 429:
                        headers = getattr(err.response, "headers", {}) or {}
                        retry_after_hdr = headers.get("retry-after") or headers.get("Retry-After")
                        try:
                            retry_after = max(1.0, float(retry_after_hdr)) if retry_after_hdr is not None else 1.0
                        except (ValueError, TypeError):
                            retry_after = 1.0
                        log.warning("Slack chat_update rate limited (429) for case %s; retrying after %.1fs", row["case_id"], retry_after)
                        self.slack_store.failed(row, uncertain=False, retry_at=now + retry_after)
                    else:
                        log.warning("Slack chat_update failed for case %s: %s", row["case_id"], err.__class__.__name__)
                        self.slack_store.failed(row, uncertain=False, retry_at=now + 5.0)
            else:
                root = self.slack_store.root(row["case_id"])
                thread_ts = root["ts"] if root and root.get("ts") and root["key"] != row["key"] else None
                kwargs = dict(body)
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                try:
                    resp = await self.client.chat_postMessage(channel=row["channel"], **kwargs)
                    self.slack_store.sent(row, resp["ts"])
                except Exception as err:
                    status_code = getattr(getattr(err, "response", None), "status_code", None)
                    if SlackApiError and isinstance(err, SlackApiError) and status_code == 429:
                        headers = getattr(err.response, "headers", {}) or {}
                        retry_after_hdr = headers.get("retry-after") or headers.get("Retry-After")
                        try:
                            retry_after = max(1.0, float(retry_after_hdr)) if retry_after_hdr is not None else 1.0
                        except (ValueError, TypeError):
                            retry_after = 1.0
                        log.warning("Slack chat_postMessage rate limited (429) for case %s; retrying after %.1fs", row["case_id"], retry_after)
                        self.slack_store.failed(row, uncertain=False, retry_at=now + retry_after)
                    else:
                        log.warning("Slack chat_postMessage failed for case %s: %s", row["case_id"], err.__class__.__name__)
                        self.slack_store.failed(row, uncertain=True, retry_at=0)


class SlackActions:
    """Handles Slack interactivity block actions."""

    def __init__(
        self,
        store: CaseStore,
        notifier: SlackNotifier,
        config: SlackConfig,
        team_id: Optional[str] = None,
        wake: Optional[Callable[[], Any]] = None,
    ):
        self.store = store
        self.notifier = notifier
        self.config = config
        self.team_id = team_id
        self.wake = wake

    async def handle(self, ack: Any, body: dict):
        res = ack()
        if inspect.isawaitable(res):
            await res

        team = body.get("team", {}).get("id")
        channel = body.get("channel", {}).get("id")
        user = body.get("user", {}).get("id")
        container_ts = body.get("container", {}).get("message_ts")
        actions = body.get("actions", [])

        async def reject(reason: str):
            if channel and user and hasattr(self.notifier.client, "chat_postEphemeral"):
                try:
                    await self.notifier.client.chat_postEphemeral(
                        channel=channel, user=user, text=f"Action not processed: {reason}"
                    )
                except Exception:
                    pass

        if self.team_id and team != self.team_id:
            await reject("Invalid team")
            return

        if channel != self.config.channel:
            await reject("Invalid channel")
            return

        if not actions:
            await reject("No action provided")
            return

        raw_val = actions[0].get("value", "")
        try:
            val = json.loads(raw_val)
            case_id = val["case_id"]
            key = val["key"]
            choice = val["choice"]
        except Exception:
            await reject("Malformed action payload")
            return

        msg = self.notifier.slack_store.message(case_id, key)
        if not msg or msg.get("ts") != container_ts:
            await reject("Outdated message")
            return

        q = self.store.question(case_id)
        if not q or q.get("key") != key:
            await reject("Question is no longer active")
            return

        if user == self.config.default_user:
            employee_id = q.get("addressed_to", "emp-hannah-reed")
        elif self.config.user_mapping and user in self.config.user_mapping:
            mapped_emp = self.config.user_mapping[user]
            if mapped_emp != q.get("addressed_to"):
                await reject("You are not the addressed employee for this question")
                return
            employee_id = mapped_emp
        else:
            await reject("Unauthorized user")
            return

        now = time.time()
        try:
            accepted = self.store.submit_decision(
                "helio",
                case_id,
                key=key,
                choice=choice,
                employee_id=employee_id,
                source="slack",
                now=now,
                actor_id=user,
            )
        except ValueError as e:
            await reject(str(e))
            return

        if self.wake:
            try:
                self.wake()
            except Exception:
                pass

        updated_body = self.notifier.render_body(case_id, key, q)
        self.notifier.slack_store.enqueue(case_id, key, channel, updated_body)
        try:
            await self.notifier.client.chat_update(channel=channel, ts=container_ts, **updated_body)
            self.notifier.slack_store.sent(msg, container_ts)
        except Exception as err:
            log.warning("Failed to immediately update Slack message after click: %s", err)
