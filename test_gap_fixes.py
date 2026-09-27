#!/usr/bin/env python3
"""Regression checks for the consolidated live-page + challenge-pack audit gaps."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot

E = sys.argv[1] if len(sys.argv) > 1 else "/mnt/data/challenge_expanded"
def load_json(path):
    with open(path, encoding="utf-8") as f: return json.load(f)

def push(scope, cid, version, payload):
    return bot.handle_context({"scope":scope,"context_id":cid,"version":version,"payload":payload})

bot.STORE.reset()
cat = load_json(f"{E}/categories/dentists.json")
m1 = load_json(f"{E}/merchants/m_001_drmeera_dentist_delhi.json")
m2 = load_json(f"{E}/merchants/m_002_bharat_dentist_mumbai.json")
for scope,cid,p in [("category","dentists",cat),("merchant",m1["merchant_id"],m1),("merchant",m2["merchant_id"],m2)]:
    assert push(scope,cid,1,p)[0] == 200

# live page: equal version is idempotent no-op; older version is stale.
code, out = push("category","dentists",1,cat)
assert code == 200 and out.get("no_op") is True
code, out = push("category","dentists",0,cat)
assert code == 409 and out["reason"] == "stale_version"

# public compose() exists and returns complete message data.
t = load_json(f"{E}/triggers/trg_001_research_digest_dentists.json")
assert push("trigger",t["id"],1,t)[0] == 200
msg = bot.compose(cat,m1,t,None)
assert msg and msg["body"] and msg["cta"] and msg["send_as"] == "vera"

# same suppression key must not globally suppress another merchant.
t1={"id":"same1","scope":"merchant","kind":"perf_spike","merchant_id":m1["merchant_id"],"customer_id":None,"payload":{"metric":"calls"},"urgency":2,"suppression_key":"shared:key","expires_at":"2026-12-01T00:00:00Z"}
t2={"id":"same2","scope":"merchant","kind":"perf_spike","merchant_id":m2["merchant_id"],"customer_id":None,"payload":{"metric":"calls"},"urgency":2,"suppression_key":"shared:key","expires_at":"2026-12-01T00:00:00Z"}
push("trigger","same1",1,t1); push("trigger","same2",1,t2)
a=bot.handle_tick({"now":"2026-04-26T10:00:00Z","available_triggers":["same1","same2"]})["actions"]
assert {x["merchant_id"] for x in a} == {m1["merchant_id"],m2["merchant_id"]}

# expired triggers are ignored.
tex={"id":"expired","scope":"merchant","kind":"perf_spike","merchant_id":m1["merchant_id"],"payload":{},"urgency":3,"suppression_key":"expired:key","expires_at":"2026-01-01T00:00:00Z"}
push("trigger","expired",1,tex)
assert bot.handle_tick({"now":"2026-04-26T10:00:00Z","available_triggers":["expired"]})["actions"] == []

# consent must belong to the same merchant.
c=load_json(f"{E}/customers/c_001_priya_for_m001.json")
assert bot.consent_ok(c,"recall_due",m1["merchant_id"])
assert not bot.consent_ok(c,"recall_due",m2["merchant_id"])

# yes-flow delivers immediately and makes no future-work/live claim.
conv="gap_yes"
bot.STORE.conversations[conv]={"merchant_id":m1["merchant_id"],"customer_id":None,"trigger_id":t["id"],"kind":t["kind"],"sent":[],"stage":0,"status":"open","auto":0,"nos":0,"apologised":False,"send_as":"vera"}
r=bot.handle_reply({"conversation_id":conv,"merchant_id":m1["merchant_id"],"from_role":"merchant","message":"yes, do it","received_at":"2026-04-26T11:00:00Z","turn_number":2})
low=r.get("body","").lower()
assert r["action"] == "send" and "ready" in low
assert "10 minute" not in low and "next week" not in low and "it's live" not in low

print("gap regression checks: PASS")
