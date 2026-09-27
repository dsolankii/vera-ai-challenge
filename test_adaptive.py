#!/usr/bin/env python3
"""Checks: adaptive context injection, robustness and determinism (in-process, no server needed)."""
import copy, glob, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot

E = sys.argv[1] if len(sys.argv) > 1 else "expanded"
def load():
    bot.STORE.reset()
    for scope, sub, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                            ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
        for f in sorted(glob.glob(f"{E}/{sub}/*.json")):
            p = json.load(open(f, encoding="utf-8"))
            bot.handle_context({"scope": scope, "context_id": p[key], "version": 1, "payload": p})

# 1. new digest item pushed as category v2 + a new trigger that points at it
load()
cat = json.load(open(f"{E}/categories/dentists.json", encoding="utf-8"))
cat2 = copy.deepcopy(cat)
cat2["digest"].append({"id": "d_NEW_ortho", "kind": "research", "title": "Night-guard use cuts bruxism tooth wear by 41% in 18-30s",
                       "source": "Indian Journal of Dental Research, May 2026", "trial_n": 640,
                       "summary": "Six-month cohort study found nightly guards reduced enamel wear by 41% in young adults with exam-stress bruxism."})
print("category v2:", bot.handle_context({"scope": "category", "context_id": "dentists", "version": 2, "payload": cat2}))
trg = {"id": "trg_new_1", "scope": "merchant", "kind": "research_digest", "source": "external",
       "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
       "payload": {"category": "dentists", "top_item_id": "d_NEW_ortho"}, "urgency": 3,
       "suppression_key": "research:dentists:new_ortho", "expires_at": "2026-06-01T00:00:00Z"}
bot.handle_context({"scope": "trigger", "context_id": trg["id"], "version": 1, "payload": trg})
a = bot.handle_tick({"now": "2026-04-26T10:40:00Z", "available_triggers": ["trg_new_1"]})["actions"][0]
print("uses new digest item:", "41%" in a["body"] and "640" in a["body"], "\n ", a["body"])

# 2. updated performance snapshot (merchant v2) changes the numbers used
m = json.load(open(f"{E}/merchants/m_002_bharat_dentist_mumbai.json", encoding="utf-8"))
m2 = copy.deepcopy(m); m2["performance"]["views"] = 1310; m2["performance"]["calls"] = 9
bot.handle_context({"scope": "merchant", "context_id": m["merchant_id"], "version": 2, "payload": m2})
a = bot.handle_tick({"now": "2026-04-26T10:45:00Z", "available_triggers": ["trg_005_renewal_due_bharat"]})["actions"][0]
print("uses new perf numbers:", "1,310" in a["body"] and "9 calls" in a["body"], "\n ", a["body"])

# 3. surprise customer pushed mid-test, recall trigger 2 minutes later
cust = {"customer_id": "c_new_ravi", "merchant_id": "m_001_drmeera_dentist_delhi",
        "identity": {"name": "Ravi", "language_pref": "en", "age_band": "30-40"},
        "relationship": {"first_visit": "2025-10-02", "last_visit": "2026-04-02", "visits_total": 3, "services_received": ["cleaning"]},
        "state": "lapsed_soft", "preferences": {"preferred_slots": "weekday_evening", "reminder_opt_in": True},
        "consent": {"opted_in_at": "2025-10-02", "scope": ["recall_reminders"]}}
trg2 = {"id": "trg_new_recall", "scope": "customer", "kind": "recall_due", "source": "internal",
        "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": "c_new_ravi",
        "payload": {"service_due": "6_month_cleaning", "last_service_date": "2026-04-02", "due_date": "2026-10-02",
                    "available_slots": [{"iso": "2026-10-01T18:30:00+05:30", "label": "Thu 1 Oct, 6:30pm"},
                                        {"iso": "2026-10-02T19:00:00+05:30", "label": "Fri 2 Oct, 7pm"}]},
        "urgency": 3, "suppression_key": "recall:c_new_ravi:6mo", "expires_at": "2026-10-30T00:00:00Z"}
bot.handle_context({"scope": "trigger", "context_id": trg2["id"], "version": 1, "payload": trg2})
print("trigger before customer arrives ->", bot.handle_tick({"now": "2026-04-26T10:50:00Z", "available_triggers": ["trg_new_recall"]}))
bot.handle_context({"scope": "customer", "context_id": "c_new_ravi", "version": 1, "payload": cust})
a = bot.handle_tick({"now": "2026-04-26T10:52:00Z", "available_triggers": ["trg_new_recall"]})["actions"][0]
print("customer-facing:", a["send_as"], a["cta"], "\n ", a["body"])

# 4. robustness: junk trigger payloads never crash and never invent numbers
junk = [{"id": "j1", "kind": "perf_dip", "merchant_id": "m_001_drmeera_dentist_delhi", "payload": {}},
        {"id": "j2", "kind": "totally_new_kind", "merchant_id": "m_003_studio11_salon_hyderabad", "payload": {"x": 1}},
        {"id": "j3", "kind": "festival_upcoming", "merchant_id": "m_005_pizzajunction_restaurant_delhi", "payload": {"festival": "Holi"}},
        {"id": "j4", "kind": "recall_due", "scope": "customer", "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": "c_missing", "payload": {}},
        {"id": "j5", "kind": "competitor_opened", "merchant_id": "m_009_apollo_pharmacy_jaipur", "payload": {"competitor_name": "MedPlus", "distance_km": 2}}]
for j in junk:
    bot.handle_context({"scope": "trigger", "context_id": j["id"], "version": 1, "payload": j})
out = bot.handle_tick({"now": "2026-04-26T11:00:00Z", "available_triggers": [j["id"] for j in junk] + ["does_not_exist"]})
for a in out["actions"]:
    print(" junk ->", a["trigger_id"], ":", a["body"])

# 5. determinism: same inputs twice -> identical outputs
def run_all():
    load()
    ids = [os.path.basename(f)[:-5] for f in sorted(glob.glob(f"{E}/triggers/*.json"))]
    return json.dumps([bot.handle_tick({"now": "2026-04-26T10:30:00Z", "available_triggers": [i]}) for i in ids], sort_keys=True)
print("deterministic:", run_all() == run_all())
