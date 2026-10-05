"""A thin Telegram Bot API client (long polling).

Trimmed from dllmch-tgbot's tgapi.py, with inline keyboards and callback
queries added. Long polling means no port, no nginx location and nothing
inbound: the bot only dials out.

The token is part of every request URL, so no URL is ever logged. Errors name
the method only. urllib3 logs URLs at DEBUG; bot.py pins it to WARNING.
"""

import json
import logging
import os
import random
import time

import requests

# Overridable so selftest.py can drive the bot against a fake Bot API.
API_ROOT = os.environ.get("MTRBOT_TG_API_ROOT", "https://api.telegram.org")

POLL_SECONDS = 50
READ_TIMEOUT = POLL_SECONDS + 15
CONNECT_TIMEOUT = 15
MAX_MESSAGE_CHARS = 4096

log = logging.getLogger("mtrbot.tgapi")


class TelegramError(Exception):
    def __init__(self, method, code, description, retry_after=None):
        super().__init__("%s failed: %s %s" % (method, code, description))
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


class FatalTelegramError(TelegramError):
    """A bad token (401) or a second poller (409). Retrying fixes neither."""


class Bot(object):
    def __init__(self, token, session=None):
        self._base = "%s/bot%s/" % (API_ROOT, token)
        self._session = session or requests.Session()

    def call(self, method, params=None, timeout=None):
        try:
            resp = self._session.post(
                self._base + method,
                data=params or {},
                timeout=timeout or (CONNECT_TIMEOUT, 30),
            )
        except requests.RequestException as exc:
            raise TelegramError(method, 0, "network: %s" % exc.__class__.__name__)
        try:
            payload = resp.json()
        except ValueError:
            raise TelegramError(method, resp.status_code, "response was not JSON")
        if payload.get("ok"):
            return payload.get("result")
        code = payload.get("error_code", resp.status_code)
        description = str(payload.get("description", ""))[:300]
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        if code in (401, 409):
            raise FatalTelegramError(method, code, description, retry_after)
        raise TelegramError(method, code, description, retry_after)

    def close(self):
        """Drop the pool, which breaks a blocking long poll on shutdown."""
        self._session.close()

    # -- polling ----------------------------------------------------------

    def get_updates(self, offset, allowed_updates):
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": POLL_SECONDS,
                "allowed_updates": _json(list(allowed_updates)),
            },
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )

    def get_me(self):
        return self.call("getMe")

    def delete_webhook(self):
        return self.call("deleteWebhook", {"drop_pending_updates": "false"})

    def set_my_commands(self, commands):
        payload = [{"command": c, "description": d} for c, d in commands]
        return self.call("setMyCommands", {"commands": _json(payload)})

    # -- sending ----------------------------------------------------------

    def send_message(self, chat_id, text, reply_markup=None, reply_to=None):
        params = {
            "chat_id": chat_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = _json(reply_markup)
        if reply_to:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = "true"
        return self.call("sendMessage", params)

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE_CHARS],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = _json(reply_markup)
        return self.call("editMessageText", params)

    def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        try:
            self.call("answerCallbackQuery", params)
        except TelegramError:
            # Only stops the button spinner; never worth failing over.
            pass

    def send_chat_action(self, chat_id, action="typing"):
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError:
            pass


def _json(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def backoff_sleep(attempt, base=2.0, cap=60.0):
    delay = min(cap, base * (2 ** min(attempt, 6)))
    time.sleep(delay * (0.5 + random.random() * 0.5))
