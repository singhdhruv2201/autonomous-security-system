"""Deliver synthetic case updates using Awasi's existing Slack outbox."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
import json
import logging
import re

from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from helio_security.slack_app import SlackNotifier, escape_slack_text, get_ssl_context

log = logging.getLogger(__name__)
KNOWN_REJECTIONS = {
    "account_inactive", "channel_not_found", "invalid_auth", "is_archived",
    "missing_scope", "not_in_channel", "token_revoked",
}


class ShowcaseSlackClient:
    """Label showcase messages and render the available decision controls."""

    def __init__(self, client, default_user, interactive):
        self.client = client
        self.interactive = interactive
        self.default_user = default_user if re.fullmatch(r"[UW][A-Z0-9]+", default_user or "") else ""

    def message(self, text, blocks, *, mention=False):
        header = "*Awasi Showcase* · Synthetic Helio data"
        if mention and self.default_user:
            header += f" <@{self.default_user}>"
        sections = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
        sections.extend(block for block in blocks if self.interactive or block["type"] != "actions")
        if not self.interactive and any(block["type"] == "actions" for block in blocks):
            hint = "Review and respond in the showcase browser."
            sections.append({"type": "section", "text": {"type": "mrkdwn", "text": hint}})
            text += f"\n\n{hint}"
        return {"text": f"{header}\n\n{text}", "blocks": sections}

    async def chat_postMessage(self, *, text, blocks, **destination):
        body = self.message(text, blocks, mention="thread_ts" not in destination)
        try:
            return await self.client.chat_postMessage(**destination, **body)
        except SlackApiError as error:
            reason = error.response.get("error")
            if reason in KNOWN_REJECTIONS:
                log.warning("Showcase Slack post rejected (%s)", reason)
            raise

    async def chat_update(self, *, text, blocks, **destination):
        return await self.client.chat_update(**destination, **self.message(text, blocks))



    async def chat_postEphemeral(self, **message):
        return await self.client.chat_postEphemeral(**message)


class ShowcaseSlackNotifier(SlackNotifier):
    def __init__(self, cases, client, config, *, model_name, recording):
        super().__init__(cases, client, config)
        self.model_name = model_name
        self.recording = recording

    def render_body(self, case_id, key, payload):
        body = super().render_body(case_id, key, payload)
        root = self.slack_store.root(case_id)
        if key == "outcome" or (root and root["key"] != key):
            return body

        assessment = self.cases.get_investigation(case_id)
        if assessment and assessment["source"] == "model":
            heading = f"Live Anthropic assessment · {self.model_name}"
        else:
            recorded = self.recording(case_id)
            if not recorded:
                return body
            assessment = recorded["assessment"]
            heading = f"Recorded Anthropic assessment · {recorded['model']} · previous walkthrough"
            if self.model_name:
                heading += " (live assessment unavailable)"

        section = "\n".join((
            f"*{escape_slack_text(heading)}*",
            escape_slack_text(assessment["summary"]),
            f"*Impact* {escape_slack_text(assessment['impact'])}",
            f"*Confidence* {escape_slack_text(assessment['confidence'])}",
        ))
        body["text"] += f"\n\n{section}"
        # Present the assessment before the action controls.
        index = next((i for i, block in enumerate(body["blocks"]) if block["type"] == "actions"), len(body["blocks"]))
        body["blocks"].insert(index, {"type": "section", "text": {"type": "mrkdwn", "text": section}})
        return body


def notifier_factory(config, client=None, *, model_name=None):
    if not config or not (config.bot_token and config.channel):
        return None
    if client is None:
        client = AsyncWebClient(token=config.bot_token, ssl=get_ssl_context(), timeout=5, retry_handlers=[])
    transport = ShowcaseSlackClient(client, config.default_user, interactive=has_actions(config))
    return partial(ShowcaseSlackNotifier, client=transport, config=replace(config, default_user=""),
                   model_name=model_name)


def has_actions(config):
    return bool(config and config.bot_token and config.channel and config.app_token
                and (config.default_user or config.user_mapping))


@asynccontextmanager
async def listen_for_actions(config, handle, socket=None):
    if not has_actions(config):
        yield
        return
    if socket is None:
        web = AsyncWebClient(ssl=get_ssl_context(), timeout=5)
        socket = SocketModeClient(config.app_token, web_client=web)

    async def receive(client, request):
        if request.envelope_id:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        body = request.payload or {}
        if request.type != "interactive" or body.get("type") != "block_actions":
            return
        if body.get("channel", {}).get("id") != config.channel or not body.get("container", {}).get("message_ts"):
            return
        actions = body.get("actions") or []
        if not actions or actions[0].get("action_id") not in {"awasi_choice", "awasi_undo"}:
            return
        try:
            value = json.loads(actions[0]["value"])
            case_id, choice = value["case_id"], value["choice"]
        except (KeyError, TypeError, ValueError):
            return
        if isinstance(case_id, str) and isinstance(choice, str):
            await handle(body, case_id, choice)

    async def connect():
        while True:
            try:
                await socket.connect()
                log.info("Showcase connected to Slack Socket Mode")
                return
            except Exception:
                log.warning("Showcase Slack Socket Mode connection failed; retrying")
                await asyncio.sleep(5)

    socket.socket_mode_request_listeners.append(receive)
    connection = asyncio.create_task(connect())
    try:
        yield
    finally:
        connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
        await socket.close()
