#!/usr/bin/env python3
"""MTR / Light Rail next-train Telegram bot.

Send a location -> nearest MTR station or Light Rail stop with the next trains
(minutes left + HH:MM). Type a name -> that station. Inline buttons refresh or
switch to another nearby station.

Privacy: coordinates are never logged and never stored. They ride along in the
inline buttons' callback data so Refresh can rebuild the "nearby" list.
"""

import collections
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config
import mtr
import render
import tgapi

EX_CONFIG = 78

COMMANDS = [
    ("start", "How to use this bot"),
    ("nearby", "Find the nearest station (send your location)"),
    ("station", "Next trains at a station, e.g. /station Admiralty"),
]

# Not a quota on the data -- a guard so one chat cannot turn the bot into a
# hammer on the MTR API (which answers 429) or on Telegram's send limits.
BURST = 8
WINDOW = 30.0

# Location further than this from every station: say so instead of
# pretending Lo Wu is "nearby" to someone in Tokyo.
FAR_METRES = 30000
NEARBY_OTHERS = 4

log = logging.getLogger("mtrbot")


class RateLimiter(object):
    def __init__(self):
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, user_id):
        now = time.monotonic()
        with self._lock:
            q = self._hits[user_id]
            while q and now - q[0] > WINDOW:
                q.popleft()
            if len(q) >= BURST:
                return False
            q.append(now)
            if len(self._hits) > 5000:
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True


class App(object):
    def __init__(self, cfg, bot):
        self.cfg = cfg
        self.bot = bot
        self.limiter = RateLimiter()

    # -- access -----------------------------------------------------------

    def allowed(self, user_id):
        return not self.cfg.allowed_users or user_id in self.cfg.allowed_users

    # -- entry points -----------------------------------------------------

    def handle(self, update):
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    def on_message(self, msg):
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id", 0)
        if not self.allowed(user_id):
            log.info("refused user %s", user_id)
            self.bot.send_message(chat_id, "Sorry, this bot is private.")
            return

        loc = msg.get("location") or (msg.get("venue") or {}).get("location")
        if loc:
            if not self.limiter.allow(user_id):
                return self.slow_down(chat_id)
            return self.reply_nearest(chat_id, float(loc["latitude"]), float(loc["longitude"]), user_id)

        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd[1:].split("@", 1)[0].lower()
            if cmd in ("start", "help"):
                return self.bot.send_message(chat_id, render.WELCOME, reply_markup=render.LOCATION_KEYBOARD)
            if cmd == "nearby":
                return self.bot.send_message(
                    chat_id,
                    "Tap <b>📍 Nearest station</b> below to send your location.\n"
                    "<i>On desktop Telegram, type a station name instead.</i>",
                    reply_markup=render.LOCATION_KEYBOARD,
                )
            if cmd == "station":
                text = arg.strip()
                if not text:
                    return self.bot.send_message(chat_id, "Usage: <code>/station Admiralty</code>")
            else:
                return self.bot.send_message(chat_id, "Unknown command. Try /start.")

        if not self.limiter.allow(user_id):
            return self.slow_down(chat_id)
        self.reply_search(chat_id, text[:60], user_id)

    def on_callback(self, cq):
        user_id = (cq.get("from") or {}).get("id", 0)
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        if not self.allowed(user_id):
            return self.bot.answer_callback(cq["id"], "This bot is private.")
        if not self.limiter.allow(user_id):
            return self.bot.answer_callback(cq["id"], "Too many taps - wait a few seconds.")

        parts = data.split(":")
        if len(parts) not in (3, 5) or parts[0] != "b":
            return self.bot.answer_callback(cq["id"])
        st = mtr.get("%s:%s" % (parts[1], parts[2]))
        if st is None:
            return self.bot.answer_callback(cq["id"], "Unknown station.")
        loc = None
        if len(parts) == 5:
            try:
                loc = (float(parts[3]), float(parts[4]))
            except ValueError:
                loc = None

        self.bot.answer_callback(cq["id"], "Updating…")
        text, markup = self.build(st, loc)
        log.info("user %s board %s (button)", user_id, st.key)
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        try:
            self.bot.edit_message(chat_id, msg["message_id"], text, reply_markup=markup)
        except tgapi.TelegramError as exc:
            if "not modified" in (exc.description or ""):
                return
            # Too old to edit, or deleted: send fresh instead.
            self.bot.send_message(chat_id, text, reply_markup=markup)

    # -- replies ----------------------------------------------------------

    def build(self, st, loc):
        st = mtr.canonical(st)  # either half of an interchange: one board
        others = []
        distance = None
        if loc:
            ranked = mtr.nearest(loc[0], loc[1], limit=NEARBY_OTHERS + 1)
            distance = mtr.distance_to(st, loc[0], loc[1])
            others = [(m, s) for m, s in ranked if s.key != st.key][:NEARBY_OTHERS]
        board = mtr.board(st)
        return render.board_text(board, distance), render.board_keyboard(st, loc, others)

    def reply_nearest(self, chat_id, lat, lon, user_id):
        self.bot.send_chat_action(chat_id)
        ranked = mtr.nearest(lat, lon, limit=1)
        metres, st = ranked[0]
        log.info("user %s location -> %s", user_id, st.key)  # never the coordinates
        text, markup = self.build(st, (lat, lon))
        if metres > FAR_METRES:
            text = ("📍 You are %s from the nearest station - are you in Hong Kong?\n\n"
                    % render.fmt_distance(metres)) + text
        self.bot.send_message(chat_id, text, reply_markup=markup)

    def reply_search(self, chat_id, query, user_id):
        found = mtr.search(query)
        if not found:
            return self.bot.send_message(
                chat_id,
                "No station matches <b>%s</b>. Try the English or Chinese name, "
                "an MTR code like <code>ADM</code>, or send your location." % render.esc(query),
            )
        if len(found) > 1 and not _exact(query, found[0]):
            return self.bot.send_message(chat_id, "Which one?", reply_markup=render.choice_keyboard(found))
        st = found[0]
        log.info("user %s search -> %s", user_id, st.key)
        self.bot.send_chat_action(chat_id)
        text, markup = self.build(st, None)
        self.bot.send_message(chat_id, text, reply_markup=markup)

    def slow_down(self, chat_id):
        self.bot.send_message(chat_id, "Slow down a little - try again in a few seconds.")


def _exact(query, st):
    q = mtr._norm(query)
    if st.kind == mtr.HEAVY and q == st.id.lower():
        return True
    if q not in (mtr._norm(st.en), mtr._norm(st.zh)):
        return False
    # An interchange's two halves share a name but are one board.
    same = {mtr.canonical(s) for s in mtr.STATIONS.values() if q in (mtr._norm(s.en), mtr._norm(s.zh))}
    return len(same) == 1


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    # urllib3 logs full request URLs at DEBUG, and the Telegram URL holds the
    # token. Pin it so a global DEBUG can never print one.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = config.Config()
    except config.ConfigError as exc:
        log.error("%s -- put the token in /etc/mtr-nexttrain-bot/bot.conf and restart", exc)
        return EX_CONFIG

    bot = tgapi.Bot(cfg.token)
    try:
        me = bot.get_me()
        bot.delete_webhook()
        bot.set_my_commands(COMMANDS)
    except tgapi.FatalTelegramError as exc:
        log.error("Telegram refused the token (%s %s)", exc.code, exc.description)
        return EX_CONFIG
    except tgapi.TelegramError as exc:
        log.error("cannot reach Telegram: %s", exc)
        return 1
    log.info("connected as @%s (bot id %s), %s, %d stations loaded",
             me.get("username"), cfg.bot_id,
             "open to everyone" if not cfg.allowed_users else "%d allowed users" % len(cfg.allowed_users),
             len(mtr.STATIONS))

    app = App(cfg, bot)
    stopping = threading.Event()

    def stop(signum, _frame):
        log.info("signal %s, stopping", signum)
        stopping.set()
        bot.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="update")

    def run(update):
        try:
            app.handle(update)
        except Exception:
            log.exception("handler crashed on update %s", update.get("update_id"))

    offset = 0
    failures = 0
    while not stopping.is_set():
        try:
            updates = bot.get_updates(offset, ("message", "callback_query"))
            failures = 0
        except tgapi.FatalTelegramError as exc:
            if stopping.is_set():
                break
            log.error("fatal: %s %s", exc.code, exc.description)
            workers.shutdown(wait=False)
            return EX_CONFIG
        except tgapi.TelegramError as exc:
            if stopping.is_set():
                break
            failures += 1
            log.warning("getUpdates: %s", exc)
            if exc.retry_after:
                time.sleep(float(exc.retry_after))
            else:
                tgapi.backoff_sleep(failures)
            continue
        for update in updates or []:
            # Acknowledge before the work runs: a poison update is dropped,
            # not redelivered forever.
            offset = max(offset, update["update_id"] + 1)
            workers.submit(run, update)

    workers.shutdown(wait=True, cancel_futures=True)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
