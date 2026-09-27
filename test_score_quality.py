#!/usr/bin/env python3
"""Score-oriented regression checks for the 10 seed merchants.

These assertions are not a replacement for Magicpin's LLM judge. They protect the
concrete anchors that drive its five rubric dimensions: specificity, category fit,
merchant fit, trigger relevance, and engagement.

Usage:
  python3 test_score_quality.py ../expanded
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402


def push_all(expanded):
    bot.STORE.reset()
    specs = [
        ("category", "categories", "slug"),
        ("merchant", "merchants", "merchant_id"),
        ("customer", "customers", "customer_id"),
        ("trigger", "triggers", "id"),
    ]
    for scope, sub, key in specs:
        for path in sorted(glob.glob(os.path.join(expanded, sub, "*.json"))):
            payload = json.load(open(path, encoding="utf-8"))
            cid = payload[key]
            code, _ = bot.handle_context({
                "scope": scope,
                "context_id": cid,
                "version": 1,
                "payload": payload,
                "delivered_at": "2026-04-26T10:00:00Z",
            })
            assert code == 200, (scope, cid, code)


def body_for(trigger_id):
    # Isolate each target so suppression from a different target cannot affect it.
    out = bot.handle_tick({"now": "2026-04-26T10:30:00Z", "available_triggers": [trigger_id]})
    assert out["actions"], f"no action for {trigger_id}"
    return out["actions"][0]["body"]


def require(trigger_id, needles):
    body = body_for(trigger_id)
    low = body.lower()
    missing = [x for x in needles if x.lower() not in low]
    assert not missing, f"{trigger_id} missing {missing}\n{body}"
    return body


def main():
    expanded = sys.argv[1] if len(sys.argv) > 1 else "expanded"
    push_all(expanded)

    require("trg_004_perf_dip_bharat", [
        "Dr. Bharat", "50%", "12-call baseline", "4 calls", "980 views",
        "unverified", "no active offer", "verification", "Reply YES",
    ])
    require("trg_010_ipl_match_delhi", [
        "Suresh", "DC vs MI", "7:30pm", "covers", "Tue-Thu",
        "delivery-only", "₹399", "Reply YES",
    ])
    require("trg_009_winback_glamour", [
        "Anjali", "38 days", "30%", "24", "1,200 views", "8 calls",
        "Hair Spa @ ₹499", "not a blanket discount", "Reply YES",
    ])
    require("trg_006_festival_diwali", [
        "Lakshmi", "Diwali", "31 Oct", "188 days", "4,980 views", "62 calls",
        "Hair Spa @ ₹499", "3-post salon calendar", "Reply YES",
    ])
    require("trg_013_corporate_thali_planning", [
        "Suresh", "corporate", "Weekday Lunch Thali @ ₹149", "Indiranagar",
        "Proposal:", "AOV", "office-admin WhatsApp", "Reply YES",
    ])
    require("trg_011_review_theme_late_delivery", [
        "Suresh", "delivery late", "4 reviews", "50 mins", "15 min ride",
        "Buy 1 Pizza Get 1 Free", "covers", "trust gap", "Reply YES",
    ])
    seasonal = require("trg_014_seasonal_acquisition_dip_powerhouse", [
        "Karthik", "30%", "1,480 views", "Apr-Jun", "5.2%",
        "3 FREE Trial Classes", "retention", "Reply YES",
    ])
    for unsupported in ["245 active members", "4.5%", "10% vs 8%", "membership churn is"]:
        assert unsupported.lower() not in seasonal.lower(), f"seasonal message leaked judge-invisible aggregate/peer fact: {unsupported}\n{seasonal}"
    require("trg_018_supply_atorvastatin_recall", [
        "Ramesh", "Malviya Nagar", "atorvastatin", "AT2024-1102", "AT2024-1108",
        "MfrZ", "CDSCO", "sub-potency", "don't guess", "replacement", "Reply YES",
    ])
    require("trg_016_kids_yoga_program_drafting", [
        "Padma", "kids yoga", "4 weeks", "3 classes/week", "ages 7-12", "₹2,499",
        "First Month @ ₹499", "small-batch", "Reply YES",
    ])
    gbp = require("trg_021_unverified_gbp_sunrise", [
        "Vikas", "Gomti Nagar", "Sunrise Medicos", "unverified", "30%", "720 views", "14 calls",
        "postcard", "phone call", "verification checklist", "Reply YES",
    ])
    assert "pharmacist counsel tak hi route" not in gbp.lower(), gbp
    require("trg_023_competitor_opened_dentist", [
        "Dr. Meera", "Smile Studio", "1.3 km", "8 Apr", "₹199", "₹299", "₹100",
        "22 days", "price war", "clinical", "Reply YES",
    ])
    dormant = require("trg_025_dormancy_glamour", [
        "Anjali", "38 days", "subscription expiry", "Aundh", "1,200 views", "8 calls",
        "live offer", "renewal pitch", "practical salon-listing fix", "Reply YES",
    ])
    assert "5-minute profile check" not in dormant.lower(), dormant
    zen = require("trg_024_perf_spike_zen", [
        "Padma", "15%", "18-call baseline", "kids yoga", "First Month @ ₹499",
        "parent audience", "calm follow-up", "Reply YES",
    ])
    assert "gently nudge karo" not in zen.lower(), zen

    print("score-quality checks: PASS (13/13)")


if __name__ == "__main__":
    main()
