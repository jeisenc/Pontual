#!/usr/bin/env python3
"""
Diagnostic: records what CP's and IP's live-data endpoints actually return
(status, content type, final URL, first bytes) into data/probe.json.
Temporary — used to adapt the collector if CP's site changes.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
now = datetime.now(TZ)
d0, d1 = now.strftime("%Y-%m-%d %H:%M"), (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
types = "INTERNACIONAL, ALFA, IC, IR, REGIONAL, URB|SUBUR, ESPECIAL"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
H_CP = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.cp.pt/", "Origin": "https://www.cp.pt"}
H_CP_MIN = {"User-Agent": "Chrome"}
H_IP = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.infraestruturasdeportugal.pt/",
        "X-Requested-With": "XMLHttpRequest"}

PROBES = [
    ("cp_board", "https://www.cp.pt/sites/spring/station/trains?stationId=94-38000", H_CP),
    ("cp_board_min_headers", "https://www.cp.pt/sites/spring/station/trains?stationId=94-38000", H_CP_MIN),
    ("cp_train", "https://www.cp.pt/sites/spring/station/trains/train?trainId=831", H_CP),
    ("cp_home", "https://www.cp.pt/", {"User-Agent": UA}),
    ("ip_station_search", "https://www.infraestruturasdeportugal.pt/negocios-e-servicos/estacao-nome/Aveiro", H_IP),
    ("ip_departures", "https://www.infraestruturasdeportugal.pt/negocios-e-servicos/partidas-chegadas/9438000/"
        + quote(d0) + "/" + quote(d1) + "/" + quote(types), H_IP),
    ("ip_train", "https://www.infraestruturasdeportugal.pt/negocios-e-servicos/horarios-ncombio/831/" + now.strftime("%Y-%m-%d"), H_IP),
]

out = {"run": now.strftime("%Y-%m-%dT%H:%M"), "results": {}}
for name, url, headers in PROBES:
    r = {"url": url}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read()
            r.update(status=resp.status, final_url=resp.geturl(),
                     headers={k: v for k, v in resp.headers.items() if k.lower() in
                              ("content-type", "server", "cf-ray", "location", "set-cookie", "x-cache", "content-length")})
    except urllib.error.HTTPError as e:
        body = e.read() or b""
        r.update(status=e.code, final_url=url, headers={k: v for k, v in e.headers.items() if k.lower() in ("content-type", "server", "cf-ray")})
    except Exception as e:
        body = b""
        r.update(error=repr(e)[:300])
    text = body.decode("utf-8", "replace")
    r["bytes"] = len(body)
    r["head"] = text[:1500]
    try:
        j = json.loads(text)
        r["json"] = True
        r["json_shape"] = (type(j).__name__, list(j)[:15] if isinstance(j, dict) else len(j))
    except Exception:
        r["json"] = False
    out["results"][name] = r
    print(name, r.get("status"), r.get("headers", {}).get("Content-Type") or r.get("headers", {}).get("content-type"), r["bytes"], r.get("error", ""))

p = Path(__file__).resolve().parent.parent / "data" / "probe.json"
p.parent.mkdir(exist_ok=True)
p.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
