#!/usr/bin/env python3
"""Reply-handling checks against a running bot (default http://localhost:8080).

Usage:  python test_replies.py [BOT_URL]
Run after contexts are loaded (e.g. after test_e2e.py), so replies can use merchant data.
"""
import json
import sys
import urllib.request

B = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")


def call(path, body):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def say(conv, mid, msg, turn, role="merchant", cid=None):
    out = call("/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": cid, "from_role": role,
                             "message": msg, "received_at": "2026-04-26T11:00:00Z", "turn_number": turn})
    print(f"  {role.upper():8} {msg!r}\n  -> {out.get('action')}: {out.get('body', '') or ''} "
          f"[{out.get('cta', '')}] {('wait=' + str(out['wait_seconds'])) if 'wait_seconds' in out else ''}\n"
          f"     why: {out.get('rationale')}")
    return out


SCENARIOS = [
    ("Engaged dentist", "conv_t_1", "m_001_drmeera_dentist_delhi",
     ["Yes please send the abstract. Also draft the patient WhatsApp.", "CONFIRM", "thanks"]),
    ("Auto-reply x3", "conv_t_2", "m_003_studio11_salon_hyderabad",
     ["Thank you for contacting Studio11! Our team will respond shortly."] * 3),
    ("Hard no", "conv_t_3", "m_005_pizzajunction_restaurant_delhi", ["Not interested. Stop messaging me."]),
    ("Curveball GST", "conv_t_4", "m_007_powerhouse_gym_bangalore", ["Btw can you also help me with my GST filing this month?"]),
    ("Hostile, then off-topic", "conv_t_5", "m_010_sunrisepharm_pharmacy_lucknow",
     ["Why are you bothering me. This is useless.", "can you also help me file my GST?"]),
    ("Hinglish yes", "conv_t_6", "m_006_southindiancafe_restaurant_bangalore", ["haan kar do, accha idea hai", "confirm"]),
    ("Busy", "conv_t_7", "m_004_glamour_salon_pune", ["busy right now, message me tomorrow"]),
    ("Price question", "conv_t_8", "m_002_bharat_dentist_mumbai", ["kitna lagega renewal?"]),
    ("Soft no twice", "conv_t_9", "m_008_zenyoga_gym_chennai", ["no", "no thanks"]),
    ("Question, then commit", "conv_t_10", "m_009_apollo_pharmacy_jaipur",
     ["How many customers are affected?", "Ok, let's do it. What's next?"]),
]

if __name__ == "__main__":
    for title, conv, mid, msgs in SCENARIOS:
        print("\n##", title)
        for i, m in enumerate(msgs):
            say(conv, mid, m, i + 2)
    print("\n## Customer picks slot 1 (recall)")
    say("conv_t_11", "m_001_drmeera_dentist_delhi", "1", 2, "customer", "c_001_priya_for_m001")
    print("\n## Customer confirms refill")
    say("conv_t_12", "m_009_apollo_pharmacy_jaipur", "YES", 2, "customer", "c_013_grandfather_for_m009")
