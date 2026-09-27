#!/usr/bin/env python3
"""Official-brief hardening: Phase-3 adaptive injections + Phase-4 replay flows.

Uses the public expanded dataset as base context, then injects only synthetic values shaped
like the official testing brief. It does not assume the secret payloads themselves.

Usage: python3 test_phase3_replay.py /path/to/expanded
"""
import copy
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot

E = sys.argv[1] if len(sys.argv) > 1 else "expanded"


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_scope(subdir, key):
    out = {}
    for fp in sorted(glob.glob(os.path.join(E, subdir, "*.json"))):
        obj = read_json(fp)
        out[obj[key]] = obj
    return out


CATS = load_scope("categories", "slug")
MERCHANTS = load_scope("merchants", "merchant_id")
CUSTOMERS = load_scope("customers", "customer_id")


def push(scope, cid, version, payload):
    code, out = bot.handle_context({"scope": scope, "context_id": cid, "version": version, "payload": payload})
    assert code == 200 and out.get("accepted"), (scope, cid, code, out)
    return out


def load_base():
    bot.STORE.reset()
    for slug, c in CATS.items():
        push("category", slug, 1, copy.deepcopy(c))
    for mid, m in MERCHANTS.items():
        push("merchant", mid, 1, copy.deepcopy(m))
    for cid, c in CUSTOMERS.items():
        push("customer", cid, 1, copy.deepcopy(c))


def merchant_for(slug, exclude=()):
    return next(m for m in MERCHANTS.values() if m.get("category_slug") == slug and m.get("merchant_id") not in set(exclude))


def mk_trigger(tid, merchant, kind, payload, urgency=3, customer_id=None, scope="merchant"):
    return {
        "id": tid, "scope": scope, "kind": kind, "source": "external" if scope == "merchant" else "internal",
        "merchant_id": merchant["merchant_id"], "customer_id": customer_id,
        "payload": payload, "urgency": urgency, "suppression_key": "phase3:" + tid,
        "expires_at": "2026-05-03T00:00:00Z",
    }


def body_for(trigger, customer=None):
    m = bot.STORE.get("merchant", trigger["merchant_id"])
    c = bot.STORE.get("category", m["category_slug"])
    return bot.compose(c, m, trigger, customer)


# ------------------------------------------------------------------
# Phase 3A: exact injection shapes from the official brief
# ------------------------------------------------------------------
load_base()

# Five fresh digest items per category, posted as a new category version.
latest_ids = {}
for slug in sorted(CATS):
    cat = copy.deepcopy(CATS[slug])
    for i in range(5):
        item = {
            "id": f"hidden_{slug}_{i}",
            "kind": "research",
            "title": f"Fresh {slug} field note {i + 1}",
            "source": "Post-submission digest",
            "summary": f"Newly pushed {slug} context item {i + 1}; use only after version 2 arrives.",
            "actionable": "Use this only when relevant to the merchant's current state.",
        }
        cat.setdefault("digest", []).append(item)
        latest_ids[slug] = item["id"]
    push("category", slug, 2, cat)
    assert bot.STORE.get("category", slug)["digest"][-1]["id"] == latest_ids[slug]

# Ten merchants receive new performance snapshots.
updated = []
for i, mid in enumerate(sorted(MERCHANTS)[:10], 1):
    m = copy.deepcopy(MERCHANTS[mid])
    m.setdefault("performance", {})["views"] = 3000 + i
    m.setdefault("performance", {})["calls"] = 30 + i
    m["performance"].setdefault("delta_7d", {})["views_pct"] = (0.10 if i % 2 else -0.10)
    push("merchant", mid, 2, m)
    updated.append(mid)
    assert bot.STORE.get("merchant", mid)["performance"]["views"] == 3000 + i

# Known post-submission trigger families named by the official challenge brief.
examples = []
for slug in ("pharmacies", "restaurants", "salons", "gyms"):
    m = merchant_for(slug)
    city = (m.get("identity") or {}).get("city")
    t = mk_trigger(f"hidden_weather_{slug}", m, "weather_heatwave",
                   {"category": slug, "city": city, "temperature_c": 42,
                    "headline": f"Heatwave alert in {city}", "category_relevance": "Customer needs may shift today."}, urgency=4)
    out = body_for(t)
    assert out and "42" in out["body"] and city in out["body"], out
    examples.append(t)

m = merchant_for("restaurants")
city = (m.get("identity") or {}).get("city")
t = mk_trigger("hidden_local_news", m, "local_news_event",
               {"category": "restaurants", "city": city,
                "headline": f"Main access road in {city} closed for 3 hours", "source_name": "City traffic bulletin"}, urgency=4)
out = body_for(t)
assert out and "3 hours" in out["body"] and "City traffic bulletin" in out["body"], out
examples.append(t)

m = merchant_for("dentists")
t = mk_trigger("hidden_trend", m, "category_trend_movement",
               {"category": "dentists", "query": "clear aligners Delhi", "delta_yoy": 0.62, "segment_age": "28-45"})
out = body_for(t)
assert out and "clear aligners Delhi" in out["body"] and "62%" in out["body"], out
examples.append(t)

m = merchant_for("dentists")
t = mk_trigger("hidden_digest_release", m, "category_research_digest_release",
               {"category": "dentists", "top_item_id": latest_ids["dentists"]})
out = body_for(t)
assert out and "Fresh dentists field note 5" in out["body"], out
examples.append(t)

m = merchant_for("salons")
t = mk_trigger("hidden_scheduled", m, "scheduled_recurring",
               {"category": "salons", "ask_template": "Which service are customers asking about most this week?"})
out = body_for(t)
assert out and out["cta"] == "open_ended" and "Which service" in out["body"], out
examples.append(t)

# Completely unseen kind: fact-first fallback must preserve the pushed headline rather than send a stale generic pitch.
m = merchant_for("restaurants")
t = mk_trigger("hidden_unknown", m, "neighbourhood_event_signal",
               {"category": "restaurants", "city": (m.get("identity") or {}).get("city"),
                "headline": "Office park footfall window changed this afternoon", "source_name": "Local operations bulletin"})
out = body_for(t)
assert out and "Office park footfall window changed this afternoon" in out["body"], out
assert "payload had no usable detail" not in out["rationale"]
examples.append(t)

# Explicitly wrong-city local trigger should be skipped, not converted to generic outreach.
m = merchant_for("restaurants")
wrong = "Mumbai" if (m.get("identity") or {}).get("city", "").lower() != "mumbai" else "Delhi"
t = mk_trigger("hidden_wrong_city", m, "local_news_event",
               {"category": "restaurants", "city": wrong, "headline": f"Road closure in {wrong}"})
assert body_for(t) is None

# Numeric grounding check on every hidden-family composition.
for t in examples:
    m = bot.STORE.get("merchant", t["merchant_id"])
    c = bot.STORE.get("category", m["category_slug"])
    comp = bot.Composer(c, m, t, None, bot.STORE)
    out = comp.compose()
    assert out and not comp.unknown_numbers(out["body"]), (t["kind"], out)

# Fifteen new triggers in one simulated tick must stay within the 20-action cap and never crash.
load_base()
new_ids = []
merchants = list(sorted(MERCHANTS.values(), key=lambda x: x["merchant_id"]))[:15]
kinds = ["weather_heatwave", "local_news_event", "category_trend_movement", "category_research_digest_release", "scheduled_recurring"]
for i, m in enumerate(merchants):
    slug = m["category_slug"]
    city = (m.get("identity") or {}).get("city")
    kind = kinds[i % len(kinds)]
    if kind == "weather_heatwave":
        payload = {"category": slug, "city": city, "temperature_c": 41,
                   "headline": f"Heatwave alert in {city}", "category_relevance": "Relevant same-day customer communication."}
    elif kind == "local_news_event":
        payload = {"category": slug, "city": city, "headline": f"Local access update in {city}", "source_name": "City bulletin"}
    elif kind == "category_trend_movement":
        payload = {"category": slug, "query": f"{slug} near me", "delta_yoy": 0.27}
    elif kind == "category_research_digest_release":
        # Inline item exercises the alternative injection form.
        payload = {"category": slug, "top_item": {"id": f"inline_{i}", "kind": "research", "title": f"Fresh {slug} research update", "source": "Injected digest"}}
    else:
        payload = {"category": slug, "ask_template": f"Which {bot.CATEGORY_ITEM.get(slug, 'service')} is getting the most questions this week?"}
    t = mk_trigger(f"phase3_new_{i:02d}", m, kind, payload)
    push("trigger", t["id"], 1, t)
    new_ids.append(t["id"])
actions = bot.handle_tick({"now": "2026-04-26T10:30:00Z", "available_triggers": new_ids})["actions"]
stress_count = len(actions)
assert 1 <= stress_count <= 15 and stress_count <= bot.MAX_ACTIONS_PER_TICK
assert len({a["body"] for a in actions}) == stress_count

# Five surprise customers arrive mid-test; recall triggers should wait before context and send after it arrives.
load_base()
base_merchants = list(sorted(MERCHANTS.values(), key=lambda x: x["merchant_id"]))[:5]
recall_triggers = []
new_customers = []
for i, m in enumerate(base_merchants, 1):
    cid = f"phase3_customer_{i}"
    cust = {
        "customer_id": cid, "merchant_id": m["merchant_id"],
        "identity": {"name": f"Customer {i}", "language_pref": "en"},
        "relationship": {"last_visit": "2026-01-26", "visits_total": 3, "services_received": ["visit"]},
        "state": "lapsed_soft", "preferences": {"preferred_slots": "weekday_evening", "reminder_opt_in": True},
        "consent": {"opted_in_at": "2025-01-01", "scope": ["recall_reminders"]},
    }
    trg = mk_trigger(f"phase3_recall_{i}", m, "recall_due",
                     {"service_due": "routine_followup", "last_service_date": "2026-01-26", "due_date": "2026-07-26",
                      "available_slots": [{"iso": "2026-04-27T18:00:00+05:30", "label": "Mon 27 Apr, 6pm"}]},
                     customer_id=cid, scope="customer")
    push("trigger", trg["id"], 1, trg)
    recall_triggers.append(trg)
    new_customers.append(cust)
assert bot.handle_tick({"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"] for t in recall_triggers]})["actions"] == []
for cust in new_customers:
    push("customer", cust["customer_id"], 1, cust)
actions = bot.handle_tick({"now": "2026-04-26T10:32:00Z", "available_triggers": [t["id"] for t in recall_triggers]})["actions"]
assert len(actions) == 5 and all(a["send_as"] == "merchant_on_behalf" for a in actions)

# ------------------------------------------------------------------
# Phase 4: exact public replay shapes
# ------------------------------------------------------------------
load_base()
m = merchant_for("salons")
trg = mk_trigger("replay_auto", m, "scheduled_recurring", {"category": "salons", "ask_template": "Which service is most asked about this week?"})
push("trigger", trg["id"], 1, trg)
action = bot.handle_tick({"now": "2026-04-26T11:00:00Z", "available_triggers": [trg["id"]]})["actions"][0]
conv = action["conversation_id"]
auto = "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki sabhi baatein aur sujhaav hamari team tak pahuncha deti hoon."
r1 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": auto, "received_at": "2026-04-26T11:01:00Z", "turn_number": 2})
r2 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": auto, "received_at": "2026-04-26T11:02:00Z", "turn_number": 3})
r3 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": auto, "received_at": "2026-04-26T11:03:00Z", "turn_number": 4})
r4 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": auto, "received_at": "2026-04-26T11:04:00Z", "turn_number": 5})
assert r1["action"] == "wait" and all(r["action"] == "end" for r in (r2, r3, r4)), (r1, r2, r3, r4)

# Intent transition after two qualification/question turns: third message must act immediately.
load_base()
m = merchant_for("restaurants")
trg = mk_trigger("replay_intent", m, "scheduled_recurring", {"category": "restaurants", "ask_template": "Which dish gets the most price questions this week?"})
push("trigger", trg["id"], 1, trg)
action = bot.handle_tick({"now": "2026-04-26T11:00:00Z", "available_triggers": [trg["id"]]})["actions"][0]
conv = action["conversation_id"]
q1 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": "How does this work?", "received_at": "2026-04-26T11:01:00Z", "turn_number": 2})
q2 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": "How long will it take?", "received_at": "2026-04-26T11:02:00Z", "turn_number": 3})
commit = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": "Ok, let's do it. What's next?", "received_at": "2026-04-26T11:03:00Z", "turn_number": 4})
assert q1["action"] == "send" and q2["action"] == "send"
assert commit["action"] == "send" and ("ready draft" in commit.get("body", "").lower() or "draft:" in commit.get("body", "").lower()), commit
assert "question" not in commit.get("rationale", "").lower(), commit

# Hostile then GST: one apology, then polite scope boundary / mission return.
load_base()
m = merchant_for("gyms")
trg = mk_trigger("replay_hostile", m, "scheduled_recurring", {"category": "gyms", "ask_template": "Which class gets the most questions this week?"})
push("trigger", trg["id"], 1, trg)
action = bot.handle_tick({"now": "2026-04-26T11:00:00Z", "available_triggers": [trg["id"]]})["actions"][0]
conv = action["conversation_id"]
h1 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": "This is useless nonsense, why are you bothering me?", "received_at": "2026-04-26T11:01:00Z", "turn_number": 2})
h2 = bot.handle_reply({"conversation_id": conv, "merchant_id": m["merchant_id"], "from_role": "merchant", "message": "Can you also help me file my GST?", "received_at": "2026-04-26T11:02:00Z", "turn_number": 3})
assert h1["action"] == "send" and "stop" in h1.get("body", "").lower(), h1
assert h2["action"] == "send" and ("ca" in h2.get("body", "").lower() or "outside" in h2.get("body", "").lower()), h2

print("phase3 adaptive injection checks: PASS")
print("phase4 replay checks: PASS")
print("new trigger families checked: weather_heatwave, local_news_event, category_trend_movement, category_research_digest_release, scheduled_recurring, unseen fact-first fallback")
print(f"15-trigger stress actions emitted: {stress_count}; cap={bot.MAX_ACTIONS_PER_TICK}")
