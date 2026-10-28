"""Measure Brandproof's accuracy, speed and reliability on the labelled test set (data/testset.json).

Examples
  python scripts/eval.py                                   # the AI configured in your environment
  AI_PROVIDER=none python scripts/eval.py                  # code checks only
  python scripts/eval.py --route gemini:gemini-2.5-flash --no-cache --out data/eval-gemini.json
  python scripts/eval.py --route groq:openai/gpt-oss-120b --no-cache --out data/eval-groq.json
  python scripts/eval.py --compare data/eval-gemini.json data/eval-groq.json

--route sets the model list for every text job (claims, tone, second opinion), so two runs can be compared
fairly. Keys come from GEMINI_API_KEY / GROQ_API_KEY in your environment (or a .env file); never put keys in
this command. Results go to data/eval_results.json (shown on the app's "How it works" page) unless --out is given.

Precision = share of Brandproof's findings that match a planted error.
Recall    = share of planted errors that Brandproof found.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_dotenv() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def overlaps(expected_quote: str | None, found_quote: str | None) -> bool:
    if not expected_quote:
        return True
    if not found_quote:
        return False
    a, b = expected_quote.lower(), found_quote.lower()
    return a in b or b in a


def pct(a, b):
    return round(100 * a / b, 1) if b else 100.0


def run(args) -> dict:
    from app.ai import AIGateway
    from app.checks.pipeline import run_checks
    from app.db import Database
    from app.kit import load_kit
    from app.seed import seed_kit_rows

    if args.route:
        for job in ("CLAIMS", "TONE", "VERIFY"):
            os.environ[f"AI_ROUTE_{job}"] = args.route
    with open(os.path.join(ROOT, "data", "testset.json"), encoding="utf-8") as fh:
        testset = json.load(fh)
    db = Database(sqlite_path=os.path.join(ROOT, "data", "eval-cache.db"))
    db.init_schema()
    if not db.scalar("SELECT 1 FROM rules WHERE workspace_id = 1 LIMIT 1"):
        seed_kit_rows(db, 1, "demo")
    kit = load_kit(db, 1)
    ai = AIGateway(db)
    ai.use_cache = not args.no_cache
    per_dim: dict[str, dict] = {}
    tp = fp = fn = expected_total = 0
    details, calls, check_ms = [], [], []
    drafts = testset["drafts"][: args.limit] if args.limit else testset["drafts"]
    pause = args.pause if args.pause is not None else (0 if not ai.enabled else 4)
    for i, item in enumerate(drafts, 1):
        t0 = time.time()
        result = run_checks(ai, kit, {"channel": item.get("channel"), "goal": "", "locked": []}, item["body"], None, None, "")
        check_ms.append(int((time.time() - t0) * 1000))
        for c in result["summary"]["ai"]["calls"]:
            if not c.get("cached"):
                calls.append({k: c.get(k) for k in ("purpose", "provider", "model", "ok", "ms", "error", "fallback")})
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
        details.append({"id": item["id"], "missed": missed, "ms": check_ms[-1],
                        "extra": [{"rule": f["rule_id"], "quote": f.get("quote"), "title": f["title"]} for f in extra]})
        print(f"\r{i}/{len(drafts)} drafts checked", end="", flush=True)
        if pause and i < len(drafts):
            time.sleep(pause)  # stay inside free-tier rate limits
    print()
    models: dict[str, dict] = {}
    for c in calls:
        m = models.setdefault(f"{c['provider']}:{c['model']}", {"calls": 0, "ok": 0, "ms": [], "errors": []})
        m["calls"] += 1
        if c["ok"]:
            m["ok"] += 1
            m["ms"].append(c["ms"] or 0)
        elif c.get("error") and len(m["errors"]) < 5:
            m["errors"].append(c["error"][:160])
    model_rows = {k: {"calls": v["calls"], "failures": v["calls"] - v["ok"], "success_pct": pct(v["ok"], v["calls"]),
                      "median_ms": int(statistics.median(v["ms"])) if v["ms"] else None,
                      "p90_ms": int(sorted(v["ms"])[int(0.9 * (len(v["ms"]) - 1))]) if v["ms"] else None,
                      "errors": v["errors"]} for k, v in models.items()}
    return {
        "description": testset["description"],
        "run_at": datetime.now(timezone.utc).strftime("%d %b %Y"),
        "ai": ai.label,
        "route": args.route or "configured default",
        "cache": not args.no_cache,
        "drafts": len(drafts),
        "expected": expected_total,
        "true_positives": tp, "false_positives": fp, "missed": fn,
        "precision_pct": pct(tp, tp + fp),
        "recall_pct": pct(tp, tp + fn),
        "check_median_ms": int(statistics.median(check_ms)) if check_ms else 0,
        "check_p90_ms": int(sorted(check_ms)[int(0.9 * (len(check_ms) - 1))]) if check_ms else 0,
        "ai_calls": len(calls), "ai_failures": sum(1 for c in calls if not c["ok"]),
        "ai_fallbacks": sum(1 for c in calls if c.get("fallback")),
        "models": model_rows,
        "by_dimension": {k: {"precision_pct": pct(v["tp"], v["tp"] + v["fp"]), "recall_pct": pct(v["tp"], v["tp"] + v["fn"]),
                             "expected": v["expected"]} for k, v in sorted(per_dim.items())},
        "details": details,
    }


def compare(paths: list[str]) -> None:
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            r = json.load(fh)
        rows.append((os.path.basename(p), r))
    print(f"{'run':28} {'route':40} {'prec%':>6} {'recall%':>8} {'median s':>9} {'p90 s':>6} {'AI fail':>8}")
    for name, r in rows:
        print(f"{name:28} {str(r.get('route'))[:40]:40} {r['precision_pct']:>6} {r['recall_pct']:>8} "
              f"{r.get('check_median_ms', 0) / 1000:>9.1f} {r.get('check_p90_ms', 0) / 1000:>6.1f} "
              f"{r.get('ai_failures', 0):>4}/{r.get('ai_calls', 0):<3}")


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--route", help="model list for every text job, e.g. groq:openai/gpt-oss-120b")
    p.add_argument("--no-cache", action="store_true", help="ask the model again instead of reusing cached answers")
    p.add_argument("--limit", type=int, help="only the first N drafts")
    p.add_argument("--pause", type=float, help="seconds between drafts (default 4 with AI, 0 without)")
    p.add_argument("--out", help="where to write the results (default data/eval_results.json)")
    p.add_argument("--compare", nargs="+", metavar="RESULT_JSON", help="print a comparison table of earlier runs")
    args = p.parse_args()
    if args.compare:
        compare(args.compare)
        return
    out = run(args)
    path = args.out or os.path.join(ROOT, "data", "eval_results.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"{out['ai']} [{out['route']}]: precision {out['precision_pct']}%, recall {out['recall_pct']}% "
          f"({out['true_positives']} found, {out['false_positives']} extra, {out['missed']} missed of {out['expected']} planted)")
    print(f"check time: median {out['check_median_ms'] / 1000:.1f} s, p90 {out['check_p90_ms'] / 1000:.1f} s · "
          f"AI calls {out['ai_calls']}, failed {out['ai_failures']}, answered by a backup {out['ai_fallbacks']}")
    for m, v in out["models"].items():
        print(f"  {m}: {v['calls']} calls, {v['success_pct']}% ok, median {(v['median_ms'] or 0) / 1000:.1f} s"
              + (f", errors: {v['errors'][0]}" if v["errors"] else ""))
    for d in out["details"]:
        if d["missed"] or d["extra"]:
            print(" ", d["id"], "missed:", [m["rule"] for m in d["missed"]], "extra:", [(e["rule"], e["quote"]) for e in d["extra"]])
    print("wrote", path)


if __name__ == "__main__":
    main()
