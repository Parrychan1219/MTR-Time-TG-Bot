#!/usr/bin/env python3
"""End-to-end test against a fake Bot API and a fake MTR API on loopback.

    python3 selftest.py

Starts bot.py as a child process, feeds it updates, and checks what it sends.
Touches neither Telegram nor data.gov.hk. Exit 0 = all passed.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "123456789:" + "A" * 35

updates = queue.Queue()
sent = queue.Queue()
mtr_calls = []


def heavy(line, sta):
    if line == "SIL":
        return {"status": 0, "message": "Special train service arrangements are now in place on this line.",
                "url": "https://www.mtr.com.hk/alert/alert_title_wap.html"}
    t = "2026-10-05 17:00:00"
    rows = lambda dest, plat: [
        {"seq": str(i + 1), "dest": dest, "plat": plat, "time": "2026-10-05 17:%02d:00" % (i * 3),
         "ttnt": str(i * 3), "valid": "Y", "source": "-"} for i in range(4)]
    block = {"curr_time": t, "sys_time": t, "UP": rows("TSW", "1"), "DOWN": []}
    return {"sys_time": t, "curr_time": t, "data": {"%s-%s" % (line, sta): block},
            "isdelay": "N", "status": 1, "message": "successful"}


def lrt(sid):
    return {"status": 1, "system_time": "2026-10-05 17:00:00", "platform_list": [
        {"platform_id": 1, "route_list": [
            {"route_no": "705", "dest_en": "TSW Circular", "dest_ch": "天水圍循環綫", "time_en": "Arriving",
             "time_ch": "即將抵達", "train_length": 2, "arrival_departure": "A", "stop": 0},
            {"route_no": "751", "dest_en": "Tin Yat", "dest_ch": "天逸", "time_en": "7 mins",
             "time_ch": "7 分鐘", "train_length": 1, "arrival_departure": "A", "stop": 0}]},
        {"platform_id": 2}]}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        mtr_calls.append((u.path, q))
        if u.path.endswith("/getSchedule.php"):
            return self._reply(heavy(q["line"], q["sta"]))
        if u.path.endswith("/lrt/getSchedule"):
            return self._reply(lrt(q["station_id"]))
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        params = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        if not u.path.startswith("/bot%s/" % TOKEN):
            return self._reply({"ok": False, "error_code": 401, "description": "Unauthorized"})
        method = u.path.rsplit("/", 1)[1]
        if method == "getMe":
            return self._reply({"ok": True, "result": {"id": 123456789, "username": "fake_mtr_bot"}})
        if method == "getUpdates":
            try:
                upd = updates.get(timeout=1.0)
                return self._reply({"ok": True, "result": [upd]})
            except queue.Empty:
                return self._reply({"ok": True, "result": []})
        if method in ("sendMessage", "editMessageText", "answerCallbackQuery"):
            sent.put((method, params))
        return self._reply({"ok": True, "result": {"message_id": 1} if method == "sendMessage" else True})


_uid = [0]


def push(**kw):
    _uid[0] += 1
    kw["update_id"] = _uid[0]
    updates.put(kw)


def msg(text=None, location=None, user=42):
    m = {"message_id": _uid[0] + 100, "chat": {"id": user, "type": "private"}, "from": {"id": user}}
    if text is not None:
        m["text"] = text
    if location is not None:
        m["location"] = {"latitude": location[0], "longitude": location[1]}
    push(message=m)


def expect(method, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            m, p = sent.get(timeout=deadline - time.time())
        except queue.Empty:
            break
        if m == method:
            return p
    raise AssertionError("no %s within %ss" % (method, timeout))


def drain():
    while not sent.empty():
        sent.get_nowait()


results = []


def check(name, cond, detail=""):
    results.append(cond)
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "  -- " + detail[:400]))


def start_bot(conf_text, server_port):
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write(conf_text)
    conf.close()
    env = dict(os.environ,
               MTRBOT_CONF=conf.name,
               MTRBOT_TG_API_ROOT="http://127.0.0.1:%d" % server_port,
               MTRBOT_MTR_API_ROOT="http://127.0.0.1:%d" % server_port,
               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("CREDENTIALS_DIRECTORY", None)
    return subprocess.Popen([sys.executable, os.path.join(HERE, "bot.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 1. Missing token -> exit 78 without a restart loop.
    p = start_bot("bot_token =\n", port)
    out, _ = p.communicate(timeout=20)
    check("empty token exits 78", p.returncode == 78, out)

    p = start_bot("bot_token = %s\n" % TOKEN, port)
    try:
        msg("/start")
        r = expect("sendMessage")
        kb = json.loads(r.get("reply_markup", "{}"))
        check("/start offers location button",
              kb.get("keyboard", [[{}]])[0][0].get("request_location") is True, str(r))

        # Near Admiralty: EAL/SIL/TWL/ISL in one board, SIL carries an alert.
        msg(location=(22.27945, 114.16500))
        r = expect("sendMessage")
        t = r["text"]
        check("location -> nearest is Admiralty", "Admiralty" in t, t)
        check("all four Admiralty lines", all(x in t for x in ("East Rail", "South Island", "Tsuen Wan", "Island Line")), t)
        check("minutes + HH:MM shown", "3 min</code>  17:03" in t and "Arriving</code>  17:00" in t, t)
        check("alert line shows MTR message", "Special train service" in t, t)
        check("empty direction says no more trains", "No more trains" in t, t)
        kb = json.loads(r["reply_markup"])["inline_keyboard"]
        check("refresh + nearby buttons", kb[0][0]["text"].startswith("🔄") and len(kb) == 5, str(kb))
        check("callback data fits 64 bytes", all(len(b[0]["callback_data"].encode()) <= 64 for b in kb), str(kb))

        # Tap a Light Rail stop button -> edited in place. Tin Shui Wai is
        # also a Tuen Ma Line station, so the board carries both, MTR first.
        drain()
        push(callback_query={"id": "cq1", "from": {"id": 42}, "data": "b:L:430:22.44800:114.00300",
                             "message": {"message_id": 5, "chat": {"id": 42}}})
        r = expect("editMessageText")
        t = r["text"]
        check("LRT board via button", "Tin Shui Wai" in t and "Platform 1" in t and "751" in t, t)
        check("LRT Arriving + computed clock", "Arriving</code>  17:00" in t and "7 min</code>  17:07" in t, t)
        check("LRT platform without trains", "Platform 2" in t and "No trains listed" in t, t)
        check("interchange: heavy rail, then Light Rail",
              "Tuen Ma Line" in t and "Light Rail 輕鐵" in t
              and t.index("Tuen Ma Line") < t.index("Light Rail 輕鐵") < t.index("<u>Platform 1</u>"), t)

        # User 43 from here on, to stay clear of user 42's burst limit.
        # A Light Rail stop that is not an MTR station stays Light Rail only.
        drain()
        push(callback_query={"id": "cq2", "from": {"id": 43}, "data": "b:L:445",
                             "message": {"message_id": 6, "chat": {"id": 43}}})
        t = expect("editMessageText")["text"]
        check("plain LRT stop has no heavy rail", "Tin Yiu" in t and "Platform 1" in t and "Tuen Ma Line" not in t, t)

        # Standing on the Light Rail platform at Siu Hong: one board with
        # both, and Siu Hong is not offered again as a "nearby" button.
        drain()
        msg(location=(22.41201, 113.978271), user=43)
        r = expect("sendMessage")
        t = r["text"]
        check("location at interchange -> both, heavy first",
              "Siu Hong" in t and "Tuen Ma Line" in t and "Light Rail 輕鐵" in t
              and t.index("Tuen Ma Line") < t.index("Light Rail 輕鐵"), t)
        kb = json.loads(r["reply_markup"])["inline_keyboard"]
        check("interchange not repeated in nearby buttons",
              not any("Siu Hong" in row[0]["text"] for row in kb[1:]) and len(kb) == 5, str(kb))
        check("interchange buttons use the MTR station", kb[0][0]["callback_data"].startswith("b:M:SIH:"), str(kb))

        drain()
        msg("tuen mun", user=43)
        r = expect("sendMessage")
        t = r["text"]
        check("interchange name goes straight to the board",
              "Tuen Mun" in t and t.index("Tuen Ma Line") < t.index("Light Rail 輕鐵"), t)

        msg("siu", user=43)
        r = expect("sendMessage")
        kb = r.get("reply_markup", "")
        check("ambiguous name asks which one, interchange listed once",
              r["text"] == "Which one?" and kb.count("Siu Hong") == 1 and "M:SIH" in kb, str(r))

        msg("TKO")
        r = expect("sendMessage")
        check("code search -> TKO board", "Tseung Kwan O" in r["text"] and "Tseung Kwan O Line" in r["text"], r["text"])

        msg("/station 金鐘")
        r = expect("sendMessage")
        check("/station with Chinese name", "Admiralty" in r["text"], r["text"])

        msg("Narnia")
        r = expect("sendMessage")
        check("unknown station", "No station matches" in r["text"], r["text"])

        msg(location=(35.6812, 139.7671))
        r = expect("sendMessage")
        check("far location warns", "are you in Hong Kong" in r["text"], r["text"])

        drain()
        for _ in range(10):
            msg("ADM", user=77)
        texts = []
        for _ in range(10):
            texts.append(expect("sendMessage")["text"])
        check("burst is rate limited", any("Slow down" in x for x in texts), "")

        time.sleep(0.5)
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=70)
        check("SIGTERM exits 0", p.returncode == 0, out[-500:])
        check("log names the bot", "connected as @fake_mtr_bot" in out, out[:500])
        check("token never logged", TOKEN.split(":")[1] not in out, "")
        check("coordinates never logged", "22.279" not in out and "139.76" not in out, out)
        check("cache: one MTR call per line despite repeats",
              sum(1 for c in mtr_calls if c[1].get("sta") == "ADM" and c[1].get("line") == "TWL") <= 3,
              str(len(mtr_calls)))
    finally:
        if p.poll() is None:
            p.kill()
    srv.shutdown()
    failed = results.count(False)
    print("\n%d passed, %d failed" % (results.count(True), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
