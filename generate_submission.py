#!/usr/bin/env python3
"""Generate the 30-line submission.jsonl required by the offline challenge brief.

Usage:
    python generate_submission.py /path/to/expanded [output.jsonl]
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402


def load_dir(path, subdir, key):
    out = {}
    for fn in sorted(glob.glob(os.path.join(path, subdir, "*.json"))):
        with open(fn, encoding="utf-8") as f:
            obj = json.load(f)
        out[obj[key]] = obj
    return out


def main():
    expanded = sys.argv[1] if len(sys.argv) > 1 else "expanded"
    output = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "submission.jsonl")
    categories = load_dir(expanded, "categories", "slug")
    merchants = load_dir(expanded, "merchants", "merchant_id")
    customers = load_dir(expanded, "customers", "customer_id")
    triggers = load_dir(expanded, "triggers", "id")
    with open(os.path.join(expanded, "test_pairs.json"), encoding="utf-8") as f:
        pairs = json.load(f)["pairs"]

    rows = []
    for pair in pairs:
        trg = triggers[pair["trigger_id"]]
        merchant = merchants[trg["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = customers.get(trg.get("customer_id"))
        out = bot.compose(category, merchant, trg, customer)
        if not out:
            raise RuntimeError(f"No composition for {pair['test_id']} / {trg['id']}")
        rows.append({
            "test_id": pair["test_id"],
            "body": out["body"],
            "cta": out["cta"],
            "send_as": out["send_as"],
            "suppression_key": out["suppression_key"],
            "rationale": out["rationale"],
        })

    with open(output, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {len(rows)} lines -> {output}")


if __name__ == "__main__":
    main()
