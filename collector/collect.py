#!/usr/bin/env python3
"""
Pontual collector — live data from Infraestruturas de Portugal (IP).

IP publishes delays only while a train is running ("Circula com atraso de 9 min.",
and "Hora Prevista:16:31" against each stop still ahead). Once a stop is passed,
its delay is no longer shown. So every run (about every 10 minutes):

  1. Read the departure boards of a few hub stations for the next ~90 minutes to
     discover which trains are about to run, and fetch each new train's timetable.
  2. Fetch every train that is running now. For each stop still ahead, keep the
     latest predicted delay. When a stop is passed, its last prediction becomes
     its recorded delay.
  3. When a train has passed its last stop (or is cancelled, or never shows up),
     write its record to data/days/YYYY-MM-DD.jsonl.

Standard library only. Times are Europe/Lisbon.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
BASE = os.environ.get("IP_BASE", "https://www.infraestruturasdeportugal.pt/negocios-e-servicos").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CP_DATA_DIR", ROOT / "data"))

TYPES = "INTERNACIONAL, ALFA, IC, IR, REGIONAL, URB|SUBUR, ESPECIAL"
SVC = {"ALFA": "AP", "IC": "IC", "IR": "IR", "REGIONAL": "R", "URB|SUBUR": "U",
       "INTERNACIONAL": "IN", "ESPECIAL": "ES"}

# Hub stations (IP node ids) whose boards, between them, see every CP train.
HUBS = [9431039, 9402006, 9469179, 9461002, 9436004, 9468007, 9473007, 9438000, 9406007,
        9444016, 9453009, 9475002, 9463008, 9410009, 9449007, 9434009, 9490290, 9407005,
        9430007, 9459006, 9469260, 9401008]   # + Santa Apolónia, Rossio, Cascais, São Bento

WINDOW_BACK, WINDOW_AHEAD = 40, 90   # minutes of board to read around "now"
FINISH_GRACE = 120                   # after scheduled end + this, close the train anyway
WORKERS = 8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-PT,pt;q=0.9",
    "Referer": "https://www.infraestruturasdeportugal.pt/",
    "X-Requested-With": "XMLHttpRequest",
}

stats = Counter()
unknown_status = Counter()


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def get_json(url: str, tries: int = 3):
    for attempt in range(tries):
        stats["requests"] += 1
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            out = json.loads(body)
            stats["ok"] += 1
            return out
        except urllib.error.HTTPError as e:
            stats["failed"] += 1
            if e.code == 404:
                return None
        except Exception:
            stats["failed"] += 1
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1) + random.random())
    return None


# ---------- small helpers ----------

def hm(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})", s.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def fmt(mins: int) -> str:
    mins %= 1440
    return f"{mins // 60:02d}:{mins % 60:02d}"


SMALL = {"de", "do", "da", "dos", "das", "e"}


def nice(name: str) -> str:
    """'VILA FR. DE XIRA' → 'Vila Fr. de Xira'; 'PORTO-CAMPANHÃ' → 'Porto-Campanhã'."""
    words = []
    for i, w in enumerate(name.strip().split(" ")):
        t = "-".join(p.capitalize() for p in w.lower().split("-"))
        t = ".".join(p[:1].upper() + p[1:] for p in t.split("."))
        words.append(w.lower() if i and w.lower() in SMALL else t)
    return " ".join(words)


def delay_of(text: str | None) -> int | None:
    m = re.search(r"atraso de (\d+)", text or "", re.I)
    return int(m.group(1)) if m else None


def is_cancelled(text: str | None) -> bool:
    return bool(re.search(r"suprim|cancel", text or "", re.I))


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


# ---------- IP calls ----------

def board(node: int, t0: datetime, t1: datetime):
    url = (f"{BASE}/partidas-chegadas/{node}/{quote(t0.strftime('%Y-%m-%d %H:%M'))}/"
           f"{quote(t1.strftime('%Y-%m-%d %H:%M'))}/{quote(TYPES)}")
    j = get_json(url)
    try:
        return j["response"][0]["NodesComboioTabelsPartidasChegadas"] or []
    except (TypeError, KeyError, IndexError):
        return []


BOARD_CAP = 40   # IP returns at most this many rows per board request


def board_all(node: int, t0: datetime, t1: datetime, depth: int = 0):
    """A board for [t0, t1], splitting the window when IP's row cap is hit."""
    rows = board(node, t0, t1)
    if len(rows) >= BOARD_CAP and depth < 4 and (t1 - t0) > timedelta(minutes=10):
        mid = t0 + (t1 - t0) / 2
        stats["board_splits"] += 1
        return board_all(node, t0, mid, depth + 1) + board_all(node, mid, t1, depth + 1)
    return rows


def train(n: int, d: date):
    j = get_json(f"{BASE}/horarios-ncombio/{n}/{d.isoformat()}")
    r = (j or {}).get("response") if isinstance(j, dict) else None
    return r if isinstance(r, dict) and r.get("NodesPassagemComboio") else None


def parse_timetable(t: dict) -> dict:
    """Compact timetable: stops as [node, scheduled 'HH:MM'], minutes since service-day midnight."""
    stops, seq, off, prev = [], [], 0, None
    for s in t["NodesPassagemComboio"]:
        v = hm(s.get("HoraProgramada"))
        if v is None:
            continue
        if prev is not None and v + off < prev - 180:
            off += 1440
        prev = v + off
        stops.append([str(s["NodeID"]), s["HoraProgramada"][:5]])
        seq.append(prev)
    return {"svc": SVC.get(t.get("TipoServico", ""), "?"), "stops": stops,
            "start": seq[0] if seq else None, "end": seq[-1] if seq else None}


# ---------- main steps ----------

def discover(now: datetime, catalog: dict, stations: dict, timetable: dict) -> int:
    """Read hub boards around now; add new (date, train) timetables to the catalog.
    While the persistent timetable is incomplete, sweep the whole service day so every
    train and station is selectable on the dashboard from the start."""
    t0, t1 = now - timedelta(minutes=WINDOW_BACK), now + timedelta(minutes=WINDOW_AHEAD)
    sweep = len(timetable) < 1000
    if sweep:
        t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        t1 = t0 + timedelta(hours=23, minutes=59)
    seen: set[tuple[str, int]] = set()
    with ThreadPoolExecutor(WORKERS) as ex:
        windows = [(h, t0 + timedelta(hours=k), min(t1, t0 + timedelta(hours=k + 1)))
                   for h in HUBS for k in range(int((t1 - t0).total_seconds() // 3600) + 1)]
        for rows in ex.map(lambda w: board_all(*w), windows):
            for r in rows:
                n = r.get("NComboio1")
                try:
                    d = datetime.strptime(r.get("DataRealizacao", ""), "%d-%m-%Y").date().isoformat()
                except ValueError:
                    d = now.date().isoformat()
                if isinstance(n, int):
                    seen.add((d, n))
    new = [(d, n) for d, n in seen if str(n) not in catalog.get(d, {})
           and not (sweep and str(n) in timetable and timetable[str(n)].get("seen") == d)]
    stats["board_trains"] = len(seen)

    def fetch(item):
        d, n = item
        return item, train(n, date.fromisoformat(d))

    with ThreadPoolExecutor(WORKERS) as ex:
        for (d, n), t in ex.map(fetch, new):
            if not t:
                continue
            tt = parse_timetable(t)
            if tt["start"] is None:
                continue
            timetable[str(n)] = {**tt, "seen": d}
            # Trains that already finished before tracking began go to the timetable only:
            # their delays can no longer be read, so there is nothing to observe.
            mid = datetime.fromisoformat(d).replace(tzinfo=TZ)
            if mid + timedelta(minutes=tt["end"]) >= now - timedelta(minutes=5):
                catalog.setdefault(d, {})[str(n)] = tt
            for s in t["NodesPassagemComboio"]:
                stations.setdefault(str(s["NodeID"]), [nice(s.get("NomeEstacao", "")), None, None])
    return len(new)


def observe(now: datetime, catalog: dict, live: dict) -> dict:
    """Sample every train that should be running; update per-stop estimates."""
    due = []
    for d, trains in catalog.items():
        mid = datetime.fromisoformat(d).replace(tzinfo=TZ)
        for n, tt in trains.items():
            key = f"{d}/{n}"
            if live.get(key, {}).get("done"):
                continue
            start = mid + timedelta(minutes=tt["start"] - 10)
            end = mid + timedelta(minutes=tt["end"] + FINISH_GRACE)
            if start <= now <= end or (key in live and now > end):
                due.append((d, n, tt))
    stats["due"] = len(due)

    def fetch(item):
        d, n, tt = item
        return item, train(int(n), date.fromisoformat(d))

    finished = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for (d, n, tt), t in ex.map(fetch, due):
            key = f"{d}/{n}"
            st = live.setdefault(key, {"est": {}, "passed": [], "polls": 0, "cur": None})
            mid = datetime.fromisoformat(d).replace(tzinfo=TZ)
            overdue = now > mid + timedelta(minutes=tt["end"] + FINISH_GRACE)
            if not t:
                if overdue:
                    finished[key] = ("UNRESOLVED" if st["polls"] else "NO_SERVICE", d, n, tt, st)
                continue
            first = st["polls"] == 0
            st["polls"] += 1
            sit = t.get("SituacaoComboio") or ""
            cur = delay_of(sit)
            nodes = t["NodesPassagemComboio"]
            any_passed = any(x.get("ComboioPassou") for x in nodes)
            if cur is None and any_passed and not is_cancelled(sit):
                cur = 0                          # running, no delay announced
            if sit and cur is None and not is_cancelled(sit) and sit.lower() != "programado":
                unknown_status[sit[:60]] += 1
            st["cur"] = cur
            for i, x in enumerate(nodes):
                k = str(i)
                if x.get("ComboioPassou"):
                    if k not in st["passed"]:
                        st["passed"].append(k)
                        # Passed since our last look: keep the estimate made before it passed.
                        # Already passed when we first saw the train: its delay is unknown.
                        if first:
                            st["est"].pop(k, None)
                    continue
                prev = hm((re.search(r"Prevista:\s*(\d{1,2}:\d{2})", x.get("Observacoes") or "") or [None, None])[1])
                sched = hm(x.get("HoraProgramada"))
                if prev is not None and sched is not None:
                    st["est"][k] = max(0, (prev - sched + 720) % 1440 - 720)
                elif cur is not None:
                    st["est"][k] = cur
            if is_cancelled(sit):
                finished[key] = ("CANCELLED", d, n, tt, st)
            elif nodes and nodes[-1].get("ComboioPassou"):
                finished[key] = ("OK", d, n, tt, st)
            elif overdue:
                finished[key] = ("UNRESOLVED" if any_passed else "NO_SERVICE", d, n, tt, st)

    counts = Counter()
    out: dict[str, list] = {}
    for key, (status, d, n, tt, st) in finished.items():
        counts[status] += 1
        live[key] = {"done": True}
        if status == "NO_SERVICE":
            rec = {"n": int(n), "st": "NO_SERVICE"}
        else:
            stops = [[code, hhmm, hhmm, (None if status == "CANCELLED" else st["est"].get(str(i)))]
                     for i, (code, hhmm) in enumerate(tt["stops"])]
            if status != "CANCELLED" and all(x[3] is None for x in stops):
                continue                   # seen only after it finished: nothing learned
            rec = {"n": int(n), "svc": tt["svc"], "st": status, "d": stops[-1][3] if stops else None,
                   "stops": stops, "polls": st["polls"], "t": now.strftime("%Y-%m-%dT%H:%M")}
        out.setdefault(d, []).append(rec)
    for d, recs in out.items():
        p = DATA / "days" / f"{d}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in sorted(recs, key=lambda r: r["n"]):
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return counts


def tidy(now: datetime, catalog: dict, live: dict):
    """Drop finished state and old catalog days; gzip finished day files."""
    keep = {(now.date() - timedelta(days=k)).isoformat() for k in range(2)}
    for d in list(catalog):
        if d not in keep:
            del catalog[d]
    for key in list(live):
        if key.split("/")[0] not in keep:
            del live[key]
    for p in sorted((DATA / "days").glob("*.jsonl")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if (now.date() - d).days >= 2:
            with p.open("rb") as src, gzip.open(str(p) + ".gz", "wb", compresslevel=9) as dst:
                dst.write(src.read())
            p.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="override current time (ISO, Lisbon) for testing")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now).replace(tzinfo=TZ) if args.now else datetime.now(TZ)

    catalog = load_json(DATA / "catalog.json", {})
    if catalog and not all(re.match(r"\d{4}-\d{2}-\d{2}$", k) for k in catalog):
        catalog = {}                       # old CP-format catalog: start fresh
    stations = load_json(DATA / "stations.json", {})
    stations = {k: v for k, v in stations.items() if "-" not in k}   # drop old CP-format codes
    live = load_json(DATA / "live.json", {})
    timetable = load_json(DATA / "timetable.json", {})

    t0 = time.time()
    new = discover(now, catalog, stations, timetable)
    counts = observe(now, catalog, live)
    tidy(now, catalog, live)
    save_json(DATA / "catalog.json", catalog)
    save_json(DATA / "stations.json", stations)
    save_json(DATA / "live.json", live)
    cutoff = (now.date() - timedelta(days=14)).isoformat()
    timetable = {n: t for n, t in timetable.items() if t.get("seen", "") >= cutoff}
    save_json(DATA / "timetable.json", timetable)

    health = {
        "source": "infraestruturasdeportugal.pt",
        "last_run": now.strftime("%Y-%m-%dT%H:%M"),
        "seconds": round(time.time() - t0, 1),
        "catalog_trains": sum(len(v) for v in catalog.values()),
        "timetable_trains": len(timetable),
        "new_trains": new,
        "tracking": sum(1 for v in live.values() if not v.get("done")),
        "completed": counts["OK"], "cancelled": counts["CANCELLED"],
        "no_service": counts["NO_SERVICE"], "unresolved": counts["UNRESOLVED"],
        "unknown_status": dict(unknown_status.most_common(10)),
        **{k: stats[k] for k in ("requests", "ok", "failed", "board_trains", "board_splits", "due")},
    }
    save_json(DATA / "health.json", health)
    (DATA / "probe.json").unlink(missing_ok=True)
    log("done", json.dumps(health, ensure_ascii=False))
    if stats["requests"] >= 10 and stats["ok"] == 0:
        sys.exit(2)                        # source unreachable: fail so GitHub emails the owner


if __name__ == "__main__":
    main()
