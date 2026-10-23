"""Measure Brandproof's accuracy on the labelled test set (data/testset.json).

  python scripts/eval.py                 # uses whatever AI is configured (GEMINI_API_KEY etc.)
  AI_PROVIDER=none python scripts/eval.py  # code checks only

Writes data/eval_results.json (shown on the app's "How it works" page) and prints a summary.
Precision = share of Brandproof's findings that match a planted error.
Recall    = share of planted errors that Brandproof found.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.ai import AIGateway  # noqa: E402
from app.checks.pipeline import run_checks  # noqa: E402
from app.db import Database  # noqa: E402
from app.kit import load_kit  # noqa: E402
from app.seed import seed_kit  # noqa: E402


def overlaps(expected_quote: str | None, found_quote: str | None) -> bool:
    if not expected_quote:
        return True
    if not found_quote:
        return False
    a, b = expected_quote.lower(), found_quote.lower()
    return a in b or b in a


def main() -> None:
    with open(os.path.join(ROOT, "data", "testset.json"), encoding="utf-8") as fh:
        testset = json.load(fh)
    db = Database(sqlite_path=os.path.join(ROOT, "data", "eval-cache.db"))
    db.init_schema()
    seed_kit(db)
    kit = load_kit(db)
    ai = AIGateway(db)
    per_dim: dict[str, dict] = {}
    tp = fp = fn = expected_total = 0
    details = []
    pause = float(os.environ.get("EVAL_PAUSE_SECONDS", "0" if not ai.enabled else "4"))
    for item in testset["drafts"]:
        result = run_checks(ai, kit, {"channel": item.get("channel"), "goal": "", "locked": []}, item["body"], None, None, "")
        found = [f for f in result["findings"] if f["severity"] != "ok"]
        acceptable = set(item.get("acceptable", []))
        unmatched = list(found)
        missed = []
        for exp in item["expect"]:
            expected_total += 1
            dim = kit.rules[exp["rule"]]["dimension"]
            d = per_dim.setdefault(dim, {"tp": 0, "fp": 0, "fn": 0, "expected": 0})
            d["expected"] += 1
            hit = next((f for f in unmatched if f["rule_id"] == exp["rule"] and overlaps(exp.get("quote"), f.get("quote"))), None)
            if hit:
                unmatched.remove(hit)
                tp += 1
                d["tp"] += 1
            else:
                fn += 1
                d["fn"] += 1
                missed.append(exp)
        extra = [f for f in unmatched if f["rule_id"] not in acceptable]
        for f in extra:
            fp += 1
            per_dim.setdefault(f["dimension"], {"tp": 0, "fp": 0, "fn": 0, "expected": 0})["fp"] += 1
        details.append({"id": item["id"], "missed": missed,
                        "extra": [{"rule": f["rule_id"], "quote": f.get("quote"), "title": f["title"]} for f in extra]})
        if pause:
            time.sleep(pause)  # stay inside free-tier rate limits

    def pct(a, b):
        return round(100 * a / b, 1) if b else 100.0

    out = {
        "description": testset["description"],
        "run_at": datetime.now(timezone.utc).strftime("%d %b %Y"),
        "ai": ai.label,
        "drafts": len(testset["drafts"]),
        "expected": expected_total,
        "true_positives": tp, "false_positives": fp, "missed": fn,
        "precision_pct": pct(tp, tp + fp),
        "recall_pct": pct(tp, tp + fn),
        "by_dimension": {k: {"precision_pct": pct(v["tp"], v["tp"] + v["fp"]), "recall_pct": pct(v["tp"], v["tp"] + v["fn"]),
                             "expected": v["expected"]} for k, v in sorted(per_dim.items())},
        "details": details,
    }
    path = os.path.join(ROOT, "data", "eval_results.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"{out['ai']}: precision {out['precision_pct']}%, recall {out['recall_pct']}% "
          f"({tp} found, {fp} extra, {fn} missed of {expected_total} planted)")
    for d in details:
        if d["missed"] or d["extra"]:
            print(" ", d["id"], "missed:", [m["rule"] for m in d["missed"]], "extra:", [(e["rule"], e["quote"]) for e in d["extra"]])
    print("wrote", path)


if __name__ == "__main__":
    main()
