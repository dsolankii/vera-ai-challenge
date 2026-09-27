#!/usr/bin/env python3
"""Local checks: loads the expanded dataset into the bot (in-process) and prints every composed message.

Usage:  python test_local.py path/to/expanded [--all]
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

REQUIRED = ["conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"]


def load(expanded):
    data = {}
    for scope, sub in (("category", "categories"), ("merchant", "merchants"), ("customer", "customers"), ("trigger", "triggers")):
        for f in sorted(glob.glob(os.path.join(expanded, sub, "*.json"))):
            p = json.load(open(f, encoding="utf-8"))
            cid = p.get("slug") or p.get("merchant_id") if scope in ("category", "merchant") else None
            if scope == "category":
                cid = p["slug"]
            elif scope == "merchant":
                cid = p["merchant_id"]
            elif scope == "customer":
                cid = p["customer_id"]
            else:
                cid = p["id"]
            data[(scope, cid)] = p
    return data


def main():
    expanded = sys.argv[1] if len(sys.argv) > 1 else "expanded"
    show_all = "--all" in sys.argv
    data = load(expanded)
    bot.STORE.reset()
    for (scope, cid), p in data.items():
        code, _ = bot.handle_context({"scope": scope, "context_id": cid, "version": 1, "payload": p,
                                      "delivered_at": "2026-04-26T10:00:00Z"})
        assert code == 200, (scope, cid, code)
    print("contexts:", bot.STORE.counts())
    pairs = json.load(open(os.path.join(expanded, "test_pairs.json")))["pairs"]
    order = [(p["test_id"], p["trigger_id"]) for p in pairs]
    if show_all:
        seen = {t for _, t in order}
        order += [("--", cid) for (scope, cid) in sorted(data) if scope == "trigger" and cid not in seen]
    problems, n = [], 0
    taboo = {}
    for (scope, cid), p in data.items():
        if scope == "category":
            taboo[cid] = [w.lower() for w in p.get("voice", {}).get("vocab_taboo", [])]
    for test_id, tid in order:
        out = bot.handle_tick({"now": "2026-04-26T10:30:00Z", "available_triggers": [tid]})
        acts = out["actions"]
        trg = data[("trigger", tid)]
        if not acts:
            print(f"\n[{test_id}] {tid}: NO ACTION")
            problems.append((test_id, "no action"))
            continue
        a = acts[0]
        n += 1
        missing = [k for k in REQUIRED if k not in a]
        slug = data[("merchant", a["merchant_id"])]["category_slug"]
        bad_taboo = [w for w in taboo.get(slug, []) if w.split(" (")[0] in a["body"].lower()]
        issues = []
        if missing:
            issues.append(f"missing {missing}")
        if re.search(r"https?://|www\.", a["body"]):
            issues.append("URL in body")
        if bad_taboo:
            issues.append(f"taboo {bad_taboo}")
        if len(a["body"]) > 600:
            issues.append(f"long {len(a['body'])}")
        if issues:
            problems.append((test_id, tid, issues))
        print(f"\n[{test_id}] {trg['kind']}{' (placeholder)' if trg['payload'].get('placeholder') else ''} "
              f"-> {a['merchant_id']}{' / ' + a['customer_id'] if a['customer_id'] else ''}  "
              f"[{a['send_as']}, {a['cta']}, {len(a['body'])} chars]")
        print(a["body"])
        print("  rationale:", a["rationale"])
        if issues:
            print("  !! ISSUES:", issues)
    print(f"\n{n} messages composed; problems: {problems if problems else 'none'}")


if __name__ == "__main__":
    main()
