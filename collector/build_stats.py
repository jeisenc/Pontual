#!/usr/bin/env python3
"""
Turns data/days/*.jsonl(.gz) into the small JSON files the dashboard reads:

  site/data/index.json        stations list, per-train delay at final stop, run health
  site/data/st/<code>.json    every train through one station, delay there per day
                              (the dashboard joins two of these for a journey A → B)

Delay values per day:  n >= 0 minutes late (early arrivals count as 0),
                       -1 cancelled, null no observation / did not run.
The dashboard does all threshold maths in the browser, so X can be anything.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CP_DATA_DIR", ROOT / "data"))
OUT = Path(os.environ.get("CP_SITE_DATA", ROOT / "site" / "data"))
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "84"))  # 12 weeks

SERVICES = {"AP": "Alfa Pendular", "IC": "Intercidades", "IR": "InterRegional",
            "R": "Regional", "U": "Urbano", "IN": "Internacional"}


def read_day(p: Path):
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def main():
    stations = json.loads((DATA / "stations.json").read_text("utf-8"))
    files = {}
    for p in (DATA / "days").glob("*.jsonl*"):
        files[date.fromisoformat(p.name[:10])] = p
    if not files:
        print("no data yet")
    today = datetime.now(TZ).date()
    end = max(files) if files else today
    start = max(min(files) if files else end, end - timedelta(days=WINDOW_DAYS - 1))
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    di = {d: i for i, d in enumerate(dates)}
    D = len(dates)

    # per (station, train): delays by day, last scheduled time, endpoints
    st_rows: dict[str, dict[int, dict]] = defaultdict(dict)
    final: dict[int, dict] = {}
    statuses = Counter()

    for d in dates:
        p = files.get(d)
        if not p:
            continue
        i = di[d]
        for r in read_day(p):
            statuses[r["st"]] += 1
            if r["st"] == "NO_SERVICE" or not r.get("stops"):
                continue
            n, stops = r["n"], r["stops"]
            cancelled = r["st"] == "CANCELLED"
            o, dst = stops[0][0], stops[-1][0]

            ft = final.setdefault(n, {"n": n, "svc": r.get("svc", "?"), "o": o, "d": dst,
                                      "t0": None, "t1": None, "v": [None] * D})
            ft.update(svc=r.get("svc", ft["svc"]), o=o, d=dst,
                      t0=stops[0][2] or stops[0][1], t1=stops[-1][1] or stops[-1][2],
                      p=[s[0] for s in stops])   # calling points, for "where can I go from here"
            last_delay = stops[-1][3]
            ft["v"][i] = -1 if cancelled else (max(0, last_delay) if last_delay is not None else None)

            for k, (code, arr, dep, delay) in enumerate(stops):
                row = st_rows[code].get(n)
                if row is None:
                    row = st_rows[code][n] = {"n": n, "svc": r.get("svc", "?"), "o": o, "d": dst,
                                              "t": None, "last": k == len(stops) - 1, "v": [None] * D}
                row.update(t=dep or arr, a=arr or dep, o=o, d=dst, last=(k == len(stops) - 1))
                row["v"][i] = -1 if cancelled else (max(0, delay) if delay is not None else None)

    iso = [d.isoformat() for d in dates]
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "st").mkdir(parents=True)

    def name(c):
        return (stations.get(c) or [c])[0]

    st_index = []
    for code, rows in st_rows.items():
        trains = sorted(rows.values(), key=lambda r: (r["t"] or "99:99", r["n"]))
        observed = sum(1 for r in trains for x in r["v"] if x is not None)
        body = {"code": code, "name": name(code), "dates": iso, "trains": trains}
        (OUT / "st" / f"{code}.json").write_text(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")), "utf-8")
        s = stations.get(code) or [code, None, None]
        st_index.append({"code": code, "name": s[0], "lat": s[1], "lon": s[2],
                         "trains": len(trains), "obs": observed})
    st_index.sort(key=lambda s: s["name"])

    health = {}
    hp = DATA / "health.json"
    if hp.exists():
        health = json.loads(hp.read_text("utf-8"))

    names = {c: name(c) for c in {x for t in final.values() for x in (t["o"], t["d"])}}
    # Calling points as indexes into the stations list (keeps index.json small).
    pos = {s["code"]: i for i, s in enumerate(st_index)}
    for t in final.values():
        t["p"] = [pos[c] for c in t.get("p", []) if c in pos]
    index = {
        "generated": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
        "dates": iso,
        "services": SERVICES,
        "stations": st_index,
        "names": names,
        "trains": sorted(final.values(), key=lambda t: t["n"]),
        "totals": dict(statuses),
        "health": health,
    }
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"{len(dates)} days ({iso[0] if iso else '-'} → {iso[-1] if iso else '-'}), "
          f"{len(final)} trains, {len(st_index)} stations, statuses {dict(statuses)}")


if __name__ == "__main__":
    main()
