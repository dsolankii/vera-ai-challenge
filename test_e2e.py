#!/usr/bin/env python3
"""Simulates the judge's warmup + 12 ticks over HTTP.
Usage: python test_e2e.py BOT_URL path/to/expanded"""
import json, glob, sys, time, urllib.request, re
B = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
def call(method, path, body=None):
    t = time.time()
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30); code = r.status; out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        code = e.code; out = json.loads(e.read())
    return code, out, (time.time() - t) * 1000
E = sys.argv[2] if len(sys.argv) > 2 else "expanded"
# warmup: 5 + 50 + 200
for scope, sub, key in (("category","categories","slug"),("merchant","merchants","merchant_id"),("customer","customers","customer_id")):
    for f in sorted(glob.glob(f"{E}/{sub}/*.json")):
        p = json.load(open(f)); c, o, _ = call("POST", "/v1/context", {"scope": scope, "context_id": p[key], "version": 1, "payload": p, "delivered_at": "2026-04-26T09:45:00Z"})
        assert c == 200, (c, o)
print("healthz:", call("GET", "/v1/healthz")[1])
c, o, _ = call("POST", "/v1/context", {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi", "version": 1, "payload": {}, "delivered_at": "x"})
print("same version re-push ->", c, o)
c, o, _ = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
print("bad scope ->", c, o)
# push all triggers, then 12 ticks of 5 simulated minutes with all triggers available
trs = [json.load(open(f)) for f in sorted(glob.glob(f"{E}/triggers/*.json"))]
for t in trs:
    call("POST", "/v1/context", {"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t, "delivered_at": "2026-04-26T10:00:00Z"})
all_ids = [t["id"] for t in trs]
sent, maxlat, bodies = 0, 0, []
per_tick_merchants = []
for i in range(12):
    now = f"2026-04-26T{10 + (i*5)//60:02d}:{(i*5)%60:02d}:00Z"
    c, o, lat = call("POST", "/v1/tick", {"now": now, "available_triggers": all_ids})
    maxlat = max(maxlat, lat)
    acts = o["actions"]; sent += len(acts)
    mids = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
    assert len(mids) == len(set(mids)), "duplicate merchant-facing in one tick"
    assert len(acts) <= 20
    bodies += [a["body"] for a in acts]
    print(f"tick {i+1:2d} {now}: {len(acts):2d} actions ({lat:.0f} ms)")
print("total actions:", sent, "| unique bodies:", len(set(bodies)), "| max tick latency ms:", round(maxlat))
print("URLs in bodies:", sum(bool(re.search(r'https?://', b)) for b in bodies))
