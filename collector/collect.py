#!/usr/bin/env python3
"""
CP delay collector.

Runs every ~15 minutes. Each run:
  1. Reads the departure boards of a few hub stations to discover train numbers
     (and adds new ones to the catalogue, with their full timetable).
  2. For every catalogued train whose scheduled arrival at its final stop has
     passed, fetches the train once. If CP reports it COMPLETED or CANCELLED,
     the final per-stop delays are appended to data/days/YYYY-MM-DD.jsonl.
     Trains still running are retried on the next run.

Standard library only. All times are Europe/Lisbon.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
BASE = os.environ.get("CP_BASE", "https://www.cp.pt/sites/spring").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CP_DATA_DIR", ROOT / "data"))

# Hub stations whose boards, between them, see every train on the network
# (greedy set cover over a full CP timetable: these 18 cover 100% of services).
HUBS = [
    "94-31039",  # Lisboa Oriente
    "94-2006",   # Porto Campanhã
    "94-69179",  # Oeiras
    "94-61002",  # Agualva-Cacém
    "94-36004",  # Coimbra-B
    "94-68007",  # Pinhal Novo
    "94-73007",  # Faro
    "94-38000",  # Aveiro
    "94-6007",   # Nine
    "94-44016",  # Espinho-Vouga
    "94-53009",  # Castelo Branco
    "94-75002",  # Beja
    "94-63008",  # Caldas da Rainha
    "94-10009",  # Régua
    "94-49007",  # Guarda
    "94-34009",  # Entroncamento
    "94-90290",  # Portimão
    "94-7005",   # Valença
]

BUFFER_MIN = 15          # wait this long after scheduled final arrival before first check
NO_SERVICE_AFTER = 60    # status still null this long after due → train didn't run today
GIVE_UP_AFTER = 8 * 60   # still not completed this long after due → save what we have
WORKERS = 6

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Referer": "https://www.cp.pt/",
    "Origin": "https://www.cp.pt",
}

stats = {"requests": 0, "ok": 0, "failed": 0}


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def get_json(url: str, tries: int = 3):
    for attempt in range(tries):
        stats["requests"] += 1
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            stats["ok"] += 1
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            stats["failed"] += 1
            code = getattr(e, "code", None)
            if code == 404:
                return None
            if attempt == tries - 1:
                log("FAIL", url, repr(e)[:160])
                return None
            time.sleep(1.5 * (attempt + 1) + random.random())


def unwrap(x):
    """Some CP responses wrap the payload in {"data": ...} or {"response": ...}."""
    if isinstance(x, dict):
        for k in ("data", "response"):
            if k in x and len(x) <= 3:
                return x[k]
    return x


# ---------- files ----------

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except FileNotFoundError:
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), "utf-8")
    tmp.replace(p)


def day_file(d: date) -> Path:
    return DATA / "days" / f"{d.isoformat()}.jsonl"


def done_trains(d: date) -> set[int]:
    p = day_file(d)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text("utf-8").splitlines():
        if line.strip():
            out.add(json.loads(line)["n"])
    return out


# ---------- timetable helpers ----------

def hm(s: str | None) -> int | None:
    if not s:
        return None
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def schedule_span(stops: list) -> tuple[int, int] | None:
    """(start, end) in minutes after service-day midnight; end may exceed 1440."""
    # stops are [code, arr, dep]
    seq = []
    for code, arr, dep in stops:
        for t in (arr, dep):
            v = hm(t)
            if v is not None:
                seq.append(v)
    if not seq:
        return None
    start, prev, offset = seq[0], seq[0], 0
    for v in seq[1:]:
        if v + offset < prev - 180:   # wrapped past midnight
            offset += 1440
        prev = v + offset
    return start, prev


def update_catalog(catalog: dict, stations: dict, train: dict) -> None:
    stops = train.get("trainStops") or []
    if not stops:
        return
    n = str(train["trainNumber"])
    svc = (train.get("serviceCode") or {}).get("code") or "?"
    compact = []
    for s in stops:
        st = s.get("station") or {}
        code = st.get("code")
        if not code:
            continue
        compact.append([code, s.get("arrival"), s.get("departure")])
        if code not in stations:
            stations[code] = [st.get("designation") or code, s.get("latitude"), s.get("longitude")]
    span = schedule_span(compact)
    if not span:
        return
    catalog[n] = {"svc": svc, "stops": compact, "start": span[0], "end": span[1]}


def to_record(train: dict, status: str) -> dict:
    stops = []
    for s in train.get("trainStops") or []:
        code = (s.get("station") or {}).get("code")
        if code:
            stops.append([code, s.get("arrival"), s.get("departure"), s.get("delay")])
    return {
        "n": train["trainNumber"],
        "svc": (train.get("serviceCode") or {}).get("code") or "?",
        "st": status,
        "d": train.get("delay"),
        "stops": stops,
        "t": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
    }


# ---------- main steps ----------

def discover(catalog: dict, stations: dict) -> int:
    seen: set[int] = set()

    def board(code):
        return unwrap(get_json(f"{BASE}/station/trains?stationId={code}"))

    with ThreadPoolExecutor(WORKERS) as ex:
        for res in ex.map(board, HUBS):
            if isinstance(res, list):
                for row in res:
                    n = row.get("trainNumber")
                    if isinstance(n, int):
                        seen.add(n)
    new = [n for n in seen if str(n) not in catalog]
    log(f"boards: {len(seen)} trains seen, {len(new)} new")

    def detail(n):
        return unwrap(get_json(f"{BASE}/station/trains/train?trainId={n}"))

    with ThreadPoolExecutor(WORKERS) as ex:
        for t in ex.map(detail, new):
            if isinstance(t, dict) and t.get("trainNumber"):
                update_catalog(catalog, stations, t)
    return len(new)


def collect(catalog: dict, stations: dict, now: datetime) -> dict:
    today = now.date()
    pending: list[tuple[int, date, datetime]] = []
    for sd in (today - timedelta(days=1), today):
        done = done_trains(sd)
        midnight = datetime(sd.year, sd.month, sd.day, tzinfo=TZ)
        for n, c in catalog.items():
            n = int(n)
            if n in done:
                continue
            # A service belongs to the day it departs. After midnight, only late-
            # evening trains (due from 22:00 on) are still looked up for
            # yesterday's date; an early-morning train fetched today is today's run.
            if sd < today and c["end"] + BUFFER_MIN < 22 * 60:
                continue
            due = midnight + timedelta(minutes=c["end"] + BUFFER_MIN)
            if due <= now <= due + timedelta(minutes=GIVE_UP_AFTER + 30):
                pending.append((n, sd, due))
    log(f"pending checks: {len(pending)}")

    def fetch(item):
        n, sd, due = item
        return item, unwrap(get_json(f"{BASE}/station/trains/train?trainId={n}"))

    counts = {"completed": 0, "cancelled": 0, "no_service": 0, "unresolved": 0, "waiting": 0, "error": 0}
    out: dict[date, list] = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for (n, sd, due), t in ex.map(fetch, pending):
            if not isinstance(t, dict) or not t.get("trainNumber"):
                counts["error"] += 1
                continue
            update_catalog(catalog, stations, t)
            status = (t.get("status") or "").upper()
            late_by = (now - due).total_seconds() / 60
            has_delays = any((s.get("delay") is not None) for s in t.get("trainStops") or [])
            yesterday = sd < today
            if status == "COMPLETED":
                rec, k = to_record(t, "OK"), "completed"
            elif status == "CANCELLED":
                rec, k = to_record(t, "CANCELLED"), "cancelled"
            elif yesterday and not status and not has_delays:
                # After midnight a null status may already be *today's* run, so
                # it says nothing about yesterday. Wait; drop if never resolved.
                counts["waiting"] += 1
                continue
            elif not status and not has_delays and late_by >= NO_SERVICE_AFTER:
                rec, k = {"n": n, "st": "NO_SERVICE"}, "no_service"
            elif late_by >= GIVE_UP_AFTER and has_delays:
                rec, k = to_record(t, "UNRESOLVED"), "unresolved"
            else:
                counts["waiting"] += 1
                continue
            counts[k] += 1
            out.setdefault(sd, []).append(rec)

    for sd, recs in out.items():
        p = day_file(sd)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in sorted(recs, key=lambda r: r["n"]):
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return counts


def compress_old_days(today: date) -> None:
    """Days older than 2 days are final: gzip them to keep the repo small (~6x smaller)."""
    import gzip
    for p in sorted((DATA / "days").glob("*.jsonl")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if (today - d).days >= 2:
            with p.open("rb") as src, gzip.open(str(p) + ".gz", "wb", compresslevel=9) as dst:
                dst.write(src.read())
            p.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="override current time (ISO, Lisbon) for testing")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now).replace(tzinfo=TZ) if args.now else datetime.now(TZ)

    catalog = load_json(DATA / "catalog.json", {})
    stations = load_json(DATA / "stations.json", {})
    t0 = time.time()
    new = discover(catalog, stations)
    counts = collect(catalog, stations, now)
    save_json(DATA / "catalog.json", catalog)
    save_json(DATA / "stations.json", stations)
    compress_old_days(now.date())

    health = {
        "last_run": now.strftime("%Y-%m-%dT%H:%M"),
        "seconds": round(time.time() - t0, 1),
        "catalog_trains": len(catalog),
        "new_trains": new,
        **counts,
        **stats,
    }
    save_json(DATA / "health.json", health)
    log("done", json.dumps(health))
    # Fail loudly if CP is unreachable/blocking, so GitHub emails the owner.
    if stats["requests"] >= 10 and stats["ok"] == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
