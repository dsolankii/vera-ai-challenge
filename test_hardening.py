#!/usr/bin/env python3
"""Regression tests for the gaps found in the live-page + official-ZIP audit.

Usage: python test_hardening.py /path/to/expanded
"""
import copy
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

E = sys.argv[1] if len(sys.argv) > 1 else "expanded"


def load_all():
    bot.STORE.reset()
    data = {}
    for scope, sub, key in (("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
                            ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")):
        for fn in sorted(glob.glob(os.path.join(E, sub, "*.json"))):
            with open(fn, encoding="utf-8") as f:
                obj = json.load(f)
            data[(scope, obj[key])] = obj
            code, _ = bot.handle_context({"scope": scope, "context_id": obj[key], "version": 1, "payload": obj})
            assert code == 200
    return data


def push(scope, cid, payload, version=1):
    return bot.handle_context({"scope": scope, "context_id": cid, "version": version, "payload": payload})


def main():
    data = load_all()

    # 1) Context contract: equal version is a no-op; lower version is stale; no state reset.
    cat = copy.deepcopy(data[("category", "dentists")])
    bot.STORE.conversations["keep_me"] = {"status": "open"}
    code, out = push("category", "dentists", cat, 1)
    assert code == 200 and out.get("no_op") is True and "keep_me" in bot.STORE.conversations
    cat2 = copy.deepcopy(cat); cat2["test_marker"] = "v2"
    code, _ = push("category", "dentists", cat2, 2)
    assert code == 200
    code, out = push("category", "dentists", cat, 1)
    assert code == 409 and out["reason"] == "stale_version"

    # 2) Public compose() contract + explicit suppression key.
    pair = json.load(open(os.path.join(E, "test_pairs.json"), encoding="utf-8"))["pairs"][0]
    trg = data[("trigger", pair["trigger_id"])]
    mer = data[("merchant", trg["merchant_id"])]
    cat = data[("category", mer["category_slug"])]
    cust = data.get(("customer", trg.get("customer_id"))) if trg.get("customer_id") else None
    comp = bot.compose(cat, mer, trg, cust)
    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        assert key in comp, key

    # 3) Same suppression key must not suppress another recipient.
    load_all()
    t1 = {"id": "shared_a", "scope": "merchant", "kind": "perf_spike", "merchant_id": "m_005_pizzajunction_restaurant_delhi",
          "customer_id": None, "payload": {"metric": "calls", "delta_pct": 0.11}, "urgency": 2,
          "suppression_key": "shared:key", "expires_at": "2026-05-10T00:00:00Z"}
    t2 = copy.deepcopy(t1); t2["id"] = "shared_b"; t2["merchant_id"] = "m_006_southindiancafe_restaurant_bangalore"; t2["payload"]["delta_pct"] = 0.12
    push("trigger", t1["id"], t1); push("trigger", t2["id"], t2)
    acts = bot.handle_tick({"now": "2026-04-26T10:00:00Z", "available_triggers": [t1["id"], t2["id"]]})["actions"]
    assert len(acts) == 2, acts

    # 4) Expired triggers stay silent.
    exp = copy.deepcopy(t1); exp["id"] = "expired_one"; exp["suppression_key"] = "expired:key"; exp["expires_at"] = "2026-04-25T00:00:00Z"
    push("trigger", exp["id"], exp)
    assert bot.handle_tick({"now": "2026-04-26T10:00:00Z", "available_triggers": [exp["id"]]})["actions"] == []

    # 5) Consent is purpose-specific, and customer must belong to the merchant.
    c = copy.deepcopy(data[("customer", "c_001_priya_for_m001")])
    c["consent"] = {"scope": ["appointment_reminders"]}
    assert not bot.consent_ok(c, "customer_lapsed_hard", c["merchant_id"])
    assert not bot.consent_ok(c, "recall_due", "m_002_bharat_dentist_mumbai")
    assert bot.consent_ok(c, "appointment_tomorrow", c["merchant_id"])

    # 6) Templates are explicit {{1}}/{{2}}/{{3}} definitions and actions match them.
    load_all()
    a = bot.handle_tick({"now": "2026-04-26T10:00:00Z", "available_triggers": [pair["trigger_id"]]})["actions"][0]
    assert a["template_name"] in bot.TEMPLATE_REGISTRY
    assert len(a["template_params"]) == 3
    assert all("{{%d}}" % i in bot.TEMPLATE_REGISTRY[a["template_name"]] for i in (1, 2, 3))

    # 7) Three unanswered proactive nudges on separate ticks -> fourth non-critical nudge is suppressed.
    load_all()
    mid = "m_005_pizzajunction_restaurant_delhi"
    tids = []
    for i, pct in enumerate((0.11, 0.12, 0.13, 0.14), 1):
        t = {"id": f"nudge_{i}", "scope": "merchant", "kind": "perf_spike", "merchant_id": mid, "customer_id": None,
             "payload": {"metric": "calls", "delta_pct": pct}, "urgency": 1, "suppression_key": f"nudge:{i}",
             "expires_at": "2026-05-10T00:00:00Z"}
        push("trigger", t["id"], t); tids.append(t["id"])
    for i in range(3):
        out = bot.handle_tick({"now": f"2026-04-26T10:{i*5:02d}:00Z", "available_triggers": [tids[i]]})
        assert len(out["actions"]) == 1, (i, out)
    assert bot.handle_tick({"now": "2026-04-26T10:15:00Z", "available_triggers": [tids[3]]})["actions"] == []

    # 8) Explicit 'later' schedules one follow-up and uses a NEW conversation id.
    load_all()
    initial = bot.handle_tick({"now": "2026-04-26T10:00:00Z", "available_triggers": [pair["trigger_id"]]})["actions"][0]
    wait = bot.handle_reply({"conversation_id": initial["conversation_id"], "merchant_id": initial["merchant_id"],
                             "customer_id": None, "from_role": "merchant", "message": "Later, I am busy",
                             "received_at": "2026-04-26T10:01:00Z"})
    assert wait["action"] == "wait"
    follow = bot.handle_tick({"now": "2026-04-26T14:02:00Z", "available_triggers": []})["actions"]
    assert len(follow) == 1 and follow[0]["conversation_id"] != initial["conversation_id"]

    # 9) YES delivers usable content immediately; CONFIRM never claims a real external action happened.
    load_all()
    trig_id = "trg_023_competitor_opened_dentist"
    first = bot.handle_tick({"now": "2026-04-26T10:00:00Z", "available_triggers": [trig_id]})["actions"][0]
    yes = bot.handle_reply({"conversation_id": first["conversation_id"], "merchant_id": first["merchant_id"], "customer_id": None,
                            "from_role": "merchant", "message": "Yes, let's do it", "received_at": "2026-04-26T10:02:00Z"})
    assert yes["action"] == "send" and "draft" in yes["body"].lower()
    assert "10 minute" not in yes["body"].lower() and "within 10" not in yes["body"].lower()
    confirm = bot.handle_reply({"conversation_id": first["conversation_id"], "merchant_id": first["merchant_id"], "customer_id": None,
                                "from_role": "merchant", "message": "CONFIRM", "received_at": "2026-04-26T10:03:00Z"})
    assert confirm["action"] == "send"
    assert "it's live" not in confirm["body"].lower() and "sab live" not in confirm["body"].lower()

    # 10) Newly injected relevant research can win even without top_item_id.
    load_all()
    dent = copy.deepcopy(data[("category", "dentists")])
    dent["digest"].append({"id": "d_new_aligner_2026", "kind": "research", "title": "New aligner retention study",
                           "source": "New Dental Research, Sep 2026", "summary": "Aligner follow-up study for adult cosmetic cases.",
                           "actionable": "Review follow-up instructions for aligner patients"})
    push("category", "dentists", dent, 2)
    new_t = {"id": "new_research_no_id", "scope": "merchant", "kind": "research_digest",
             "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None, "payload": {}, "urgency": 2,
             "suppression_key": "research:new:noid", "expires_at": "2026-10-10T00:00:00Z"}
    push("trigger", new_t["id"], new_t)
    act = bot.handle_tick({"now": "2026-09-27T10:00:00Z", "available_triggers": [new_t["id"]]})["actions"][0]
    assert "aligner" in act["body"].lower() and "new dental research" in act["body"].lower()

    # 11) Runtime taboo validation, not only test-time checking.
    bad_cat = copy.deepcopy(data[("category", "restaurants")])
    bad_mer = copy.deepcopy(data[("merchant", "m_005_pizzajunction_restaurant_delhi")])
    bad_mer["identity"]["name"] = "Guaranteed Packed House"
    bad_trg = {"id": "taboo_test", "kind": "unknown_kind", "scope": "merchant", "merchant_id": bad_mer["merchant_id"],
               "payload": {}, "suppression_key": "taboo:test"}
    assert bot.Composer(bad_cat, bad_mer, bad_trg, None, bot.STORE).compose() is None

    # 12) Merchant history affects ranking (Dr Meera explicitly asked to focus on aligners).
    meera = data[("merchant", "m_001_drmeera_dentist_delhi")]
    rel = {"kind": "research_digest", "urgency": 2, "payload": {"topic": "aligner"}}
    irr = {"kind": "research_digest", "urgency": 2, "payload": {"topic": "packaging"}}
    assert bot.priority(rel, meera, data[("category", "dentists")]) > bot.priority(irr, meera, data[("category", "dentists")])

    print("hardening checks: PASS (12/12)")


if __name__ == "__main__":
    main()
