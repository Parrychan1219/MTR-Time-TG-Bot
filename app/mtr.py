"""Stations, nearest-station lookup and the two MTR next-train APIs.

Heavy rail: https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php?line=&sta=
    (data.gov.hk "mtr-data2-nexttrain-data", API spec v1.7). One call per
    line at a station, so an interchange such as Admiralty is four calls.
Light Rail: https://rt.data.gov.hk/v1/transport/mtr/lrt/getSchedule?station_id=
    (LR Next Train data dictionary v1.0). One call per stop, all platforms.

Neither API carries coordinates. stations.json pairs MTR's station lists
(opendata.mtr.com.hk CSVs) with OpenStreetMap positions; tools/
build_stations.py regenerates it.

Both endpoints sit behind CloudFront and answer 429 when pushed, so every
response is cached for CACHE_SECONDS: a burst of Refresh taps costs one call.
"""

import datetime
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests

API_ROOT = os.environ.get("MTRBOT_MTR_API_ROOT", "https://rt.data.gov.hk")
HEAVY_URL = API_ROOT + "/v1/transport/mtr/getSchedule.php"
LRT_URL = API_ROOT + "/v1/transport/mtr/lrt/getSchedule"

CACHE_SECONDS = 10
TIMEOUT = (5, 10)
HKT = datetime.timezone(datetime.timedelta(hours=8))

log = logging.getLogger("mtrbot.mtr")

LINES = {
    "AEL": ("Airport Express", "機場快綫"),
    "TCL": ("Tung Chung Line", "東涌綫"),
    "TML": ("Tuen Ma Line", "屯馬綫"),
    "TKL": ("Tseung Kwan O Line", "將軍澳綫"),
    "EAL": ("East Rail Line", "東鐵綫"),
    "SIL": ("South Island Line", "南港島綫"),
    "TWL": ("Tsuen Wan Line", "荃灣綫"),
    "ISL": ("Island Line", "港島綫"),
    "KTL": ("Kwun Tong Line", "觀塘綫"),
    "DRL": ("Disneyland Resort Line", "迪士尼綫"),
}

HEAVY = "M"
LIGHT = "L"


# -- stations ---------------------------------------------------------------

class Station(object):
    __slots__ = ("kind", "id", "en", "zh", "lat", "lon", "lines")

    def __init__(self, kind, sid, rec):
        self.kind = kind
        self.id = sid
        self.en = rec["en"]
        self.zh = rec["zh"]
        self.lat = rec["lat"]
        self.lon = rec["lon"]
        # Heavy rail: line codes to query. Light Rail: route numbers, display only.
        self.lines = rec["lines"] if kind == HEAVY else rec["routes"]

    @property
    def key(self):
        return "%s:%s" % (self.kind, self.id)


def _load(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out = {}
    for sid, rec in raw["mtr"].items():
        out["%s:%s" % (HEAVY, sid)] = Station(HEAVY, sid, rec)
    for sid, rec in raw["lrt"].items():
        out["%s:%s" % (LIGHT, sid)] = Station(LIGHT, sid, rec)
    return out


STATIONS = _load()


def get(key):
    return STATIONS.get(key)


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest(lat, lon, limit=5):
    """[(metres, Station)], closest first, heavy rail and Light Rail mixed."""
    ranked = sorted(
        ((distance_m(lat, lon, s.lat, s.lon), s) for s in STATIONS.values()),
        key=lambda pair: pair[0],
    )
    return ranked[:limit]


def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\-'’.()（）]+", "", text)


def search(query, limit=8):
    """Stations matching a typed name (English or Chinese) or code."""
    q = _norm(query)
    if not q:
        return []
    scored = []
    for s in STATIONS.values():
        en, zh = _norm(s.en), _norm(s.zh)
        code = s.id.lower() if s.kind == HEAVY else ""
        if q in (en, zh, code):
            rank = 0
        elif en.startswith(q) or zh.startswith(q):
            rank = 1
        elif q in en or q in zh:
            rank = 2
        else:
            continue
        # Heavy rail first on a tie: "Tuen Mun" is usually the MTR station.
        scored.append((rank, 0 if s.kind == HEAVY else 1, s.en, s))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]


# -- fetching ---------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()
_local = threading.local()


class FetchError(Exception):
    pass


def _session():
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = "mtr-nexttrain-bot/1.0 (+telegram bot)"
        _local.session = sess
    return sess


def _get_json(url, params):
    key = url + "?" + "&".join("%s=%s" % kv for kv in sorted(params.items()))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    try:
        resp = _session().get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError("network error (%s)" % exc.__class__.__name__)
    if resp.status_code == 429:
        raise FetchError("MTR API is rate limiting, try again in a minute")
    if resp.status_code != 200:
        raise FetchError("MTR API answered HTTP %d" % resp.status_code)
    try:
        payload = resp.json()
    except ValueError:
        raise FetchError("MTR API answered with something that is not JSON")
    with _cache_lock:
        _cache[key] = (now, payload)
        if len(_cache) > 512:
            for k in [k for k, v in _cache.items() if now - v[0] >= CACHE_SECONDS]:
                del _cache[k]
    return payload


# -- the normalised board ---------------------------------------------------
#
# Board
#   sections: [Section]          one per line (heavy) or per platform (LRT)
#   updated:  "HH:MM:SS" or None
# Section
#   title, title_zh, error (str or None), groups: [Group]
# Group
#   heading (str), trains: [Train]
# Train
#   minutes (int), label (str: "1 min" / "Arriving" ...), clock ("HH:MM"),
#   note (str: destination / platform / route, may be empty)


class Train(object):
    __slots__ = ("minutes", "label", "clock", "note")

    def __init__(self, minutes, label, clock, note=""):
        self.minutes = minutes
        self.label = label
        self.clock = clock
        self.note = note


class Group(object):
    def __init__(self, heading, trains):
        self.heading = heading
        self.trains = trains


class Section(object):
    def __init__(self, title, title_zh="", groups=None, error=None, warning=None):
        self.title = title
        self.title_zh = title_zh
        self.groups = groups or []
        self.error = error
        self.warning = warning


class Board(object):
    def __init__(self, station, sections, updated):
        self.station = station
        self.sections = sections
        self.updated = updated


def _hhmm(dt):
    return dt.strftime("%H:%M")


def _parse_ts(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=HKT)
    except (TypeError, ValueError):
        return None


def _min_label(minutes):
    return "%d min" % minutes if minutes != 1 else "1 min"


def _dest_name(code):
    st = STATIONS.get("%s:%s" % (HEAVY, code))
    return "%s %s" % (st.en, st.zh) if st else code


def heavy_section(line, sta):
    en, zh = LINES.get(line, (line, ""))
    try:
        payload = _get_json(HEAVY_URL, {"line": line, "sta": sta, "lang": "EN"})
    except FetchError as exc:
        return Section(en, zh, error=str(exc)), None

    if str(payload.get("status")) != "1":
        msg = str(payload.get("message") or "Service alert")[:300]
        url = payload.get("url")
        return Section(en, zh, error=msg + ("\n" + url if url else "")), None

    block = (payload.get("data") or {}).get("%s-%s" % (line, sta)) or {}
    updated = _parse_ts(block.get("curr_time") or payload.get("curr_time"))
    warning = None
    if payload.get("isdelay") == "Y":
        warning = "Train data is delayed or missing for this line."

    groups = []
    for direction in ("UP", "DOWN"):
        if direction not in block:
            continue  # terminus: trains only leave one way
        rows = block.get(direction) or []
        valid = [r for r in rows if r.get("valid", "Y") == "Y"] or rows
        dests = []
        for r in valid:
            if r.get("dest") and r["dest"] not in dests:
                dests.append(r["dest"])
        plats = sorted({str(r.get("plat")) for r in valid if r.get("plat")})
        heading = "→ " + " / ".join(_dest_name(d) for d in dests) if dests else "→ " + direction.title()
        if len(plats) == 1:
            heading += " · Platform %s" % plats[0]
        trains = []
        for r in valid:
            try:
                minutes = max(0, int(r.get("ttnt")))
            except (TypeError, ValueError):
                minutes = None
            ts = _parse_ts(r.get("time"))
            if minutes is None and ts and updated:
                minutes = max(0, int(round((ts - updated).total_seconds() / 60.0)))
            if minutes is None:
                continue
            label = "Arriving" if minutes == 0 else _min_label(minutes)
            notes = []
            if len(dests) > 1:
                st = STATIONS.get("%s:%s" % (HEAVY, r.get("dest")))
                notes.append(st.en if st else str(r.get("dest")))
            if len(plats) > 1 and r.get("plat"):
                notes.append("P%s" % r["plat"])
            clock = _hhmm(ts) if ts else "--:--"
            trains.append(Train(minutes, label, clock, " · ".join(notes)))
        # An empty list here is real: the last train this way has gone.
        groups.append(Group(heading, trains))

    if not groups and not warning:
        warning = "No train data right now."
    return Section(en, zh, groups=groups, warning=warning), updated


_LRT_MIN = re.compile(r"(\d+)\s*min", re.I)


def lrt_sections(station_id):
    try:
        payload = _get_json(LRT_URL, {"station_id": station_id})
    except FetchError as exc:
        return [Section("Light Rail", "輕鐵", error=str(exc))], None
    if str(payload.get("status")) != "1":
        msg = str(payload.get("message") or "Light Rail service alert")[:300]
        return [Section("Light Rail", "輕鐵", error=msg)], None

    updated = _parse_ts(payload.get("system_time"))
    base = updated or datetime.datetime.now(HKT)
    sections = []
    for plat in sorted(payload.get("platform_list") or [], key=lambda p: p.get("platform_id", 0)):
        title = "Platform %s" % plat.get("platform_id", "?")
        routes = plat.get("route_list") or []
        trains = []
        stopped = False
        for r in routes:
            if r.get("stop") not in (0, "0", None):
                stopped = True
                continue
            text = str(r.get("time_en") or "")
            m = _LRT_MIN.search(text)
            if m:
                minutes = int(m.group(1))
                label = _min_label(minutes)
            else:
                # "Arriving", "Departing" or "-" (arriving/departing now).
                minutes = 0
                label = text.strip().title() if text.strip() not in ("", "-") else "Now"
            clock = _hhmm(base + datetime.timedelta(minutes=minutes))
            note = "%s → %s %s" % (r.get("route_no", "?"), r.get("dest_en", ""), r.get("dest_ch", ""))
            if r.get("arrival_departure") == "D":
                note += " (dep)"
            cars = r.get("train_length")
            if cars and str(cars) != "1":
                note += " · %s cars" % cars
            trains.append(Train(minutes, label, clock, note.strip()))
        trains.sort(key=lambda t: t.minutes)
        warning = "Some routes at this platform are suspended." if stopped else None
        if not trains and not stopped:
            warning = "No trains listed."
        sections.append(Section(title, groups=[Group("", trains)] if trains else [], warning=warning))
    if not sections:
        sections.append(Section("Light Rail", "輕鐵", warning="No trains listed (outside service hours?)."))
    return sections, updated


_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mtr")


def board(station):
    """Fetch everything for one station. Never raises for API trouble: errors
    become per-section messages so one bad line does not hide the others."""
    if station.kind == HEAVY:
        futures = [_pool.submit(heavy_section, line, station.id) for line in station.lines]
        results = [f.result() for f in futures]
        sections = [r[0] for r in results]
        stamps = [r[1] for r in results if r[1]]
        updated = max(stamps) if stamps else None
    else:
        sections, updated = lrt_sections(station.id)
    return Board(station, sections, updated.strftime("%H:%M:%S") if updated else None)
