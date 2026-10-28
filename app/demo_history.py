"""Sample history for the public demo workspace, so the Insights page has something to show on a fresh server.

Every item is a real draft that went through Brandproof's code checks when the demo was created; only the
dates, people and decisions are simulated. The Insights page says that it includes this sample history.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .ai import AIGateway
from .checks.pipeline import run_checks
from .kit import load_kit

D = "*Rates are subject to change. T&Cs apply."
REASONS = {
    "R-TON-01": ["Diwali offer really ends on Sunday; legal said a real deadline is fine.",
                 "'Don't miss' is the campaign line approved by marketing.",
                 "The sale is genuinely limited-time, so this isn't pressure.",
                 "Only today is a fact here: the webinar is today."],
    "R-ACC-04": ["This blog is for finance professionals, not first-time savers."],
    "R-TRM-05": ["This is a B2B post; our partners are customers, not savers."],
}
# (days ago, channel, creator, title, first version, fixed version or None, outcome)
HISTORY = [
    (118, "Email", "Priya", "Email: summer rate update", f"Earn up to 7.25% p.a. on Paisaleaf Save. {D}",
     None, "approved"),
    (66, "Instagram", "Priya", "Instagram: Hurry, last chance", "Hurry! Last chance to start saving with ₹100.",
     "Start saving with ₹100 a day, at your own pace.", "approved"),
    (61, "Ad", "Rahul", "Ad: double your money", "Double your money in two years with Paisaleaf!",
     f"Grow your savings steadily with Paisaleaf Save. Earn up to 7.10% p.a.* {D}", "approved"),
    (57, "Instagram", "Rahul", "Instagram: Diwali offer", "Don't miss our Diwali offer! Limited-time savings boost on Paisaleaf.",
     None, "approved_override"),
    (52, "Blog", "Priya", "Blog: how interest works",
     "By leveraging our proprietary interest-optimisation infrastructure, Paisaleaf Save systematically maximises the "
     "compounding of your liquidity so that every rupee of yield is captured across the entire term of your deposit.",
     "Your interest is added to your savings every month. Start saving with just ₹100.", "approved"),
    (47, "Website", "Rahul", "Website: trust banner", "Over 5 lakh savers trust Paisaleaf.",
     "Trusted by over 1.2 lakh savers across India.", "approved"),
    (43, "Instagram", "Rahul", "Instagram: festive savings", "Hurry, only today: festive savings with Paisaleaf! 🔥",
     None, "approved_override"),
    (39, "LinkedIn", "Priya", "LinkedIn: partner update", "Our customers love the new app.", None, "approved_override_trm"),
    (34, "Email", "Priya", "Email: rate reminder", f"Earn up to 7.10% p.a. on Paisaleaf Save*. {D}", None, "auto_claims"),
    (29, "Instagram", "Rahul", "Instagram: app rating", "Rated 4.9★ by thousands of happy savers.",
     "Rated 4.6★ on Google Play. Start saving with just ₹100.", "approved"),
    (24, "Ad", "Rahul", "Ad: limited time", "Limited-time offer: open Paisaleaf Save before it's too late!",
     None, "approved_override"),
    (19, "Email", "Priya", "Email: welcome", "Hey guys, saving is easy with Paisaleaf. Click here to start.",
     "Hi everyone, saving is easy with Paisaleaf. Open the app to start.", "auto"),
    (15, "Instagram", "Rahul", "Instagram: guaranteed returns", "GUARANTEED 12% returns with Paisaleaf Save 🚀",
     f"Earn up to 7.10% p.a. on Paisaleaf Save.* {D}", "approved"),
    (11, "X / Twitter", "Priya", "X: brand name", "PaisaLeaf Save makes saving simple.", "Paisaleaf Save makes saving simple.",
     "auto"),
    (8, "Instagram", "Rahul", "Instagram: last chance webinar", "Last chance! Join our savings webinar today.", None,
     "changes"),
    (4, "Blog", "Priya", "Blog: fixed deposits", "Open an FD with our partner bank through Paisaleaf.",
     "Open a fixed deposit with our partner bank through Paisaleaf.", "review"),
]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def seed_history(db, ws: int) -> int:
    off = AIGateway(None)
    off.mode = "none"
    kit = load_kit(db, ws)
    now = datetime.now(timezone.utc)
    meera = "Meera"
    made = 0
    for i, (ago, channel, creator, title, body1, body2, outcome) in enumerate(HISTORY):
        t0 = now - timedelta(days=ago, hours=(i * 5) % 9)
        t = lambda h: _iso(t0 + timedelta(hours=h))  # noqa: E731
        did = db.insert(
            "INSERT INTO drafts (workspace_id, title, channel, goal, locked, status, created_by, created_at, updated_at, "
            "current_version, working_body, working_alt, working_dirty, client_token) VALUES (?, ?, ?, '', '[]', 'checked', ?, ?, ?, "
            "1, ?, '', 0, ?) RETURNING id", (ws, title, channel, creator, t(0), t(0), body1, f"demo-history-{i}"))

        def audit(action, actor, role, h, detail):
            db.execute("INSERT INTO audit (workspace_id, draft_id, actor, role, action, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (ws, did, actor, role, action, json.dumps(detail), t(h)))

        def version(n, body, h):
            res = run_checks(off, kit, {"channel": channel, "goal": "", "locked": []}, body, None, None, "")
            db.execute("INSERT INTO versions (workspace_id, draft_id, n, body, alt_text, state, findings, summary, kit_hash, "
                       "created_by, created_at) VALUES (?, ?, ?, ?, '', 'done', ?, ?, ?, ?, ?)",
                       (ws, did, n, body, json.dumps(res["findings"]), json.dumps(res["summary"]), kit.hash, creator, t(h)))
            audit("check.completed", creator, "creator", h, {"version": n, "issues": res["summary"]["issues"]})
            return res["findings"]

        audit("draft.created", creator, "creator", 0, {"title": title, "sample_history": True})
        f1 = version(1, body1, 0)
        override = outcome.startswith("approved_override")
        for j, f in enumerate([f for f in f1 if f["severity"] != "ok"]):
            reasons = REASONS.get(f["rule_id"])
            if override and reasons and (f["rule_id"] != "R-TRM-05" or outcome == "approved_override_trm"):
                action, reason = "reject", reasons[(i + j) % len(reasons)]
            elif outcome in ("changes", "review") or body2 is None and not override:
                continue
            else:
                action, reason = ("edit" if (i + j) % 4 == 0 else "accept"), ""
            db.execute("INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, rule_id, severity, action, reason, "
                       "before_text, after_text, actor, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                       (ws, did, f["key"], f["rule_id"], f["severity"], action, reason, f.get("quote") or f["title"],
                        creator, t(0.2)))
            audit(f"finding.{action}", creator, "creator", 0.2, {"version": 1, "rule": f["rule_id"], "reason": reason})
        n = 1
        if body2:
            version(2, body2, 1)
            n = 2
        status, when_h = "checked", 1
        if outcome.startswith("approved") or outcome == "auto_claims":
            audit("draft.submitted", creator, "creator", 1.5, {"version": n})
            audit("review.approved", meera, "reviewer", 20, {"version": n, "comment": "OK to publish."})
            status, when_h = "approved", 20
        elif outcome == "auto":
            audit("draft.auto_approved", creator, "creator", 1.5, {"version": n, "policy": "No claims and no open blocker or major findings"})
            status, when_h = "approved", 1.5
        elif outcome == "changes":
            audit("draft.submitted", creator, "creator", 1, {"version": n})
            db.execute("INSERT INTO comments (workspace_id, draft_id, actor, role, body, created_at) VALUES (?, ?, ?, 'reviewer', ?, ?)",
                       (ws, did, meera, "Please drop 'Last chance': the webinar repeats every month.", t(5)))
            audit("review.changes_requested", meera, "reviewer", 5, {"version": n, "comment": "Please drop 'Last chance'."})
            status, when_h = "changes_requested", 5
        elif outcome == "review":
            audit("draft.submitted", creator, "creator", 1.2, {"version": n})
            status, when_h = "in_review", 1.2
        db.execute("UPDATE drafts SET status = ?, current_version = ?, working_body = ?, updated_at = ?, review_reason = ? "
                   "WHERE id = ?", (status, n, body2 or body1, t(when_h),
                                    "it makes factual or promotional claims" if status == "in_review" else None, did))
        made += 1
    # a fourth demo person who appears in the history
    return made
