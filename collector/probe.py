#!/usr/bin/env python3
"""
Diagnostic (temporary): samples Infraestruturas de Portugal's live data so the
collector can be adapted to it. Writes data/probe.json.
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
now = datetime.now(TZ)
BASE = "https://www.infraestruturasdeportugal.pt/negocios-e-servicos"
TYPES = "INTERNACIONAL, ALFA, IC, IR, REGIONAL, URB|SUBUR, ESPECIAL"
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*", "Referer": "https://www.infraestruturasdeportugal.pt/",
     "X-Requested-With": "XMLHttpRequest"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30) as r:
        return json.loads(r.read())


def board(node, t0, t1):
    return get(f"{BASE}/partidas-chegadas/{node}/{quote(t0.strftime('%Y-%m-%d %H:%M'))}/{quote(t1.strftime('%Y-%m-%d %H:%M'))}/{quote(TYPES)}")


out = {"run": now.strftime("%Y-%m-%dT%H:%M")}

# 1. A whole-day board at a hub: does a 24 h window work, and how many trains?
try:
    day0 = now.replace(hour=0, minute=0)
    b = board(9402006, day0, day0 + timedelta(hours=23, minutes=59))
    rows = b["response"][0]["NodesComboioTabelsPartidasChegadas"]
    out["campanha_full_day"] = {"rows": len(rows), "first": rows[:2], "last": rows[-1:],
                                "observacoes_values": sorted({r.get("Observacoes", "") for r in rows})[:40]}
except Exception as e:
    out["campanha_full_day"] = {"error": repr(e)[:300]}

# 2. Trains running now: full detail for a handful, both late and on time.
try:
    b = board(9431039, now - timedelta(minutes=90), now + timedelta(minutes=30))   # Lisboa Oriente
    rows = b["response"][0]["NodesComboioTabelsPartidasChegadas"]
    late = [r for r in rows if "atraso" in (r.get("Observacoes") or "").lower()]
    ontime = [r for r in rows if not r.get("Observacoes") and not r.get("ComboioPassou")]
    passed = [r for r in rows if r.get("ComboioPassou")]
    picks = late[:3] + ontime[:1] + passed[-2:]
    out["oriente_window_rows"] = len(rows)
    out["oriente_observacoes"] = sorted({r.get("Observacoes", "") for r in rows})
    out["details"] = []
    for r in picks:
        n = r["NComboio1"]
        d = get(f"{BASE}/horarios-ncombio/{n}/{now.strftime('%Y-%m-%d')}")
        out["details"].append({"board_row": r, "detail": d})
except Exception as e:
    out["details_error"] = repr(e)[:300]

p = Path(__file__).resolve().parent.parent / "data" / "probe.json"
p.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
print("probe written", {k: (v if not isinstance(v, (list, dict)) else len(v)) for k, v in out.items()})
