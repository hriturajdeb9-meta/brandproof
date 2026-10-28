"""Tests for the Round 3 work: model fallback, masking, second opinion, guideline import, insights,
workspaces and sign-in, background checks, double-submit protection and the approval record.

Run with:  python -m unittest discover -s tests -v
(PostgreSQL row-level-security tests run too when TEST_DATABASE_URL points to an empty database.)
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("FAKE_AI_FILE", os.path.join(ROOT, "tests", "fake_ai.json"))

from app.ai import AIGateway, parse_route  # noqa: E402
from app.db import Database  # noqa: E402
from app.guidelines import heuristic_rules, locate, phrase_pattern  # noqa: E402
from app.kit import load_kit  # noqa: E402
from app.redact import Redactor, luhn_ok  # noqa: E402
from app.seed import seed_kit_rows  # noqa: E402


def app_client(provider="none", database=None):
    """A fresh in-process app with its own database."""
    warnings.simplefilter("ignore", ResourceWarning)
    os.environ["AI_PROVIDER"] = provider
    from starlette.testclient import TestClient
    from app import main
    from app.web import S
    S.jobs = None
    main.init(database or Database(sqlite_path=os.path.join(tempfile.mkdtemp(), "t.db")))
    return main, S, TestClient(main.app)


def wait(S):
    assert S.jobs.wait_idle(30), "background jobs did not finish"


def url_of(r) -> str:
    return re.sub(r"[?#].*$", "", str(r.url))


class GatewayTests(unittest.TestCase):
    def setUp(self):
        os.environ["AI_PROVIDER"] = "fake"
        self.db = Database(sqlite_path=":memory:")
        self.db.init_schema()
        self.ai = AIGateway(self.db)

    def test_route_parsing(self):
        self.assertEqual(parse_route("gemini:gemini-2.5-flash, groq:openai/gpt-oss-120b,bad, x:y"),
                         [("gemini", "gemini-2.5-flash"), ("groq", "openai/gpt-oss-120b")])

    def test_busy_model_falls_back_and_cools_down(self):
        route = [("fake", "busy"), ("fake", "fake")]
        data, meta = self.ai.json("claims", "s", "GUARANTEED 12% returns with Paisaleaf Save", route=route)
        self.assertIsNotNone(data)
        self.assertTrue(meta["fallback"])
        self.assertEqual([a["ok"] for a in meta["attempts"]], [False, True])
        self.ai.use_cache = False
        _, meta2 = self.ai.json("claims", "s", "something else", route=route)
        self.assertEqual(meta2["attempts"][0]["model"], "fake")  # the busy model is resting, so it goes last

    def test_keys_decide_which_steps_run(self):
        os.environ["AI_PROVIDER"] = ""
        saved = {k: os.environ.pop(k, None) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY")}
        try:
            ai = AIGateway(self.db)
            self.assertFalse(ai.enabled)
            os.environ["GROQ_API_KEY"] = "test"
            ai = AIGateway(self.db)
            self.assertEqual(ai.route("claims")[0][0], "groq")
            os.environ["AI_ROUTE_TONE"] = "groq:openai/gpt-oss-20b"
            self.assertEqual(ai.route("tone"), [("groq", "openai/gpt-oss-20b")])
        finally:
            os.environ.pop("AI_ROUTE_TONE", None)
            os.environ.pop("GROQ_API_KEY", None)
            for k, v in saved.items():
                if v:
                    os.environ[k] = v
            os.environ["AI_PROVIDER"] = "fake"


class RedactionTests(unittest.TestCase):
    def test_mask_and_restore(self):
        text = "Mail priya@example.com or call +91 98765 43210 about Project Neem. PAN ABCDE1234F."
        r = Redactor(text, ["Project Neem"])
        for secret in ("priya@example.com", "98765 43210", "Project Neem", "ABCDE1234F"):
            self.assertNotIn(secret, r.text)
        self.assertIn("[CONFIDENTIAL-1]", r.text)
        self.assertEqual(r.unmask(r.text), text)
        self.assertEqual(r.counts, {"email": 1, "phone": 1, "confidential": 1, "pan": 1})

    def test_card_numbers_need_luhn(self):
        self.assertTrue(luhn_ok("4111 1111 1111 1111"))
        self.assertFalse(luhn_ok("4111 1111 1111 1112"))
        self.assertIn("card", Redactor("Card 4111 1111 1111 1111").counts)
        self.assertNotIn("card", Redactor("Order 4111 1111 1111 1112").counts)
        # part of a longer group of numbers is not an Aadhaar or phone number
        self.assertEqual(Redactor("Order 4111 1111 1111 1112").masked, 0)
        self.assertEqual(Redactor("Ref 98765 43210 12").masked, 0)
        self.assertEqual(Redactor("Aadhaar 2345 6789 0123, call 98765 43210.").counts, {"aadhaar": 1, "phone": 1})

    def test_rupee_amounts_are_not_phone_numbers(self):
        self.assertEqual(Redactor("Start with ₹100. Insured up to ₹5,00,000. Rated 4.6★.").masked, 0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        os.environ["AI_PROVIDER"] = "fake"
        self.db = Database(sqlite_path=":memory:")
        self.db.init_schema()
        seed_kit_rows(self.db, 1, "demo")
        self.kit = load_kit(self.db, 1)
        self.ai = AIGateway(self.db)

    def run_checks(self, text):
        from app.checks.pipeline import run_checks
        return run_checks(self.ai, self.kit, {"channel": "Instagram", "goal": "", "locked": []}, text, None, None, "")

    def test_confidential_terms_and_ids_are_blockers_and_masked(self):
        res = self.run_checks("Project Neem is coming. Your PAN ABCDE1234F is safe. Email help@paisaleaf.in")
        rules = [f["rule_id"] for f in res["findings"] if f["severity"] == "blocker"]
        self.assertIn("R-CNF-01", rules)
        self.assertIn("R-CNF-02", rules)
        self.assertEqual(res["summary"]["masked"]["counts"].get("confidential"), 1)
        self.assertEqual(res["summary"]["masked"]["counts"].get("email"), 1)

    def test_second_opinion_disagreement_needs_a_human(self):
        res = self.run_checks("GUARANTEED 12% returns with Paisaleaf Save 🚀 The best savings app in India. Sign-up now!")
        best = next(f for f in res["findings"] if (f.get("quote") or "").startswith("The best savings app"))
        self.assertEqual(best["second_opinion"]["agree"], False)
        self.assertTrue(best["needs_human"])
        self.assertGreaterEqual(res["summary"]["second_opinion"]["checked"], 1)

    def test_large_kits_send_only_relevant_rules(self):
        from app.checks.ai_checks import ai_rules
        from app.retrieval import select_rules
        rules = ai_rules(self.kit) + [{"id": f"R-TON-{50 + i}", "title": f"Rule about topic{i}", "description": f"topic{i} words",
                                       "dimension": "tone", "method": "ai", "severity": "minor"} for i in range(30)]
        chosen, selected = select_rules(rules, "Optimise your liquidity with our high-yield APY product.")
        self.assertTrue(selected)
        self.assertLessEqual(len(chosen), 20)
        self.assertIn("R-TON-03", [r["id"] for r in chosen])  # the jargon rule is relevant to this draft


class GuidelineTests(unittest.TestCase):
    PAGES = ["Acme Coffee Brand Guide\nWe never claim that coffee cures or prevents any illness.\nPrimary colour: Roast Brown #6B3E26",
             "Voice\nNever use slang such as lit or epic.\nWrite Acme Coffee, not ACME coffee."]

    def test_locate_checks_the_quote_is_on_the_page(self):
        self.assertEqual(locate("Never use slang such as lit or epic.", self.PAGES, 1), (2, "exact"))
        self.assertEqual(locate("never  use slang such as lit or EPIC", self.PAGES, 2)[0], 2)
        self.assertEqual(locate("Something the guideline never says at all.", self.PAGES, 1), (None, "not found"))

    def test_banned_words_become_a_safe_pattern(self):
        pat = re.compile(phrase_pattern(["lit", "epic", "a.b"]), re.I)
        self.assertTrue(pat.search("This is LIT"))
        self.assertFalse(pat.search("literally fine"))
        self.assertFalse(pat.search("aXb"))

    def test_patterns_work_without_ai(self):
        kinds = {c["kind"] for c in heuristic_rules(self.PAGES)}
        self.assertEqual(kinds, {"rule", "glossary"})

    def test_import_verify_and_approve(self):
        main, S, client = app_client("fake")
        client.post("/demo/as", data={"role": "brand"})
        r = client.post("/guidelines", files={"file": ("acme.txt", "\f".join(self.PAGES).encode(), "text/plain")})
        gurl = url_of(r)
        wait(S)
        page = client.get(gurl).text
        self.assertIn("quote not found", page)               # the made-up rule is flagged
        self.assertIn("✓ quote found in document", page)
        self.assertIn("#6B3E26", page)                       # colour found by code
        r = client.post(gurl + "/decide", data={"decision": "approve_verified"})
        self.assertIn("Approved", r.text)
        kit = client.get("/kit").text
        self.assertIn("Never use slang", kit)
        self.assertNotIn("Made up rule", kit)
        rule_id = re.search(r'id="r-(R-TON-\d+)"', kit[kit.index("Never use slang") - 900:]).group(1)
        rule = client.get(f"/kit/rules/{rule_id}").text
        self.assertIn("tests pass", rule)                    # examples ran as tests on approval
        self.assertIn("acme.txt, p. 2", rule)
        r = client.post("/drafts", data={"title": "t", "body": "This coffee is lit and epic."})
        wait(S)
        self.assertIn(rule_id, client.get(url_of(r)).text)   # the new rule applies to the next check


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main, cls.S, cls.client = app_client("none")

    def test_background_check_and_double_submit(self):
        c, S = self.client, self.S
        r = c.post("/drafts", data={"title": "Once", "body": "Hurry! Earn 9% p.a.", "token": "tok-1"})
        first = url_of(r)
        r = c.post("/drafts", data={"title": "Once", "body": "Hurry! Earn 9% p.a.", "token": "tok-1"})
        self.assertEqual(url_of(r), first)                   # same form sent twice -> one draft
        wait(S)
        self.assertIn("R-CLM-02", c.get(first).text)
        c.post(first + "/recheck", data={"base": "1", "body": "Start saving with ₹100."})
        r = c.post(first + "/recheck", data={"base": "1", "body": "Start saving with ₹100."})
        self.assertIn("Already re-checked", r.text)
        wait(S)
        self.assertIn("v2", c.get(first).text)

    def test_reviewer_cannot_create_or_fix(self):
        c = self.client
        c.post("/demo/as", data={"role": "reviewer"})
        try:
            self.assertEqual(c.post("/drafts", data={"title": "x", "body": "y"}).status_code, 403)
            self.assertEqual(c.post("/kit/palette/save", data={"name": "X", "hex": "#000000"}).status_code, 403)
        finally:
            c.post("/demo/as", data={"role": "creator"})

    def test_approval_record_pdf(self):
        c, S = self.client, self.S
        r = c.post("/samples/email")
        url = url_of(r)
        wait(S)
        c.post(url + "/submit")
        c.post("/demo/as", data={"role": "reviewer"})
        c.post(url + "/review", data={"decision": "approve", "comment": "Fine"})
        c.post("/demo/as", data={"role": "creator"})
        r = c.get(url + "/record.pdf")
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_insights_radar_and_expiry(self):
        page = self.client.get("/insights").text
        self.assertIn("R-TON-01", page[page.index("Clarity Radar"):])  # the often-rejected pressure rule
        self.assertIn("Expiry watch", page)
        self.assertIn("F-02", page)                                     # approved content with an expired rate

    def test_signup_workspace_isolation_and_team(self):
        from starlette.testclient import TestClient
        a = TestClient(self.main.app)
        r = a.post("/signup", data={"name": "Asha", "email": "asha@example.com", "password": "longpassword",
                                    "workspace": "Asha Club", "kit": "starter"})
        self.assertIn("Welcome, Asha", r.text)
        self.assertEqual(a.get("/drafts/1").status_code, 404)           # demo drafts are invisible
        r = a.post("/team", data={"action": "invite", "email": "ben@example.com", "role": "reviewer"})
        self.assertIn("Added ben@example.com", r.text)
        b = TestClient(self.main.app)
        b.post("/signup", data={"name": "Ben", "email": "ben@example.com", "password": "longpassword",
                                "workspace": "Ben's own", "kit": "empty"})
        b.post("/workspaces/switch", data={"workspace": next(
            m for m in __import__("app.auth", fromlist=["x"]).memberships(self.S.db, __import__("app.auth", fromlist=["x"]).find_user(self.S.db, "ben@example.com")["id"])
            if m["name"] == "Asha Club")["workspace_id"]})
        self.assertIn("Asha Club", b.get("/").text)
        self.assertEqual(b.post("/kit/palette/save", data={"name": "X", "hex": "#000000"}).status_code, 403)
        r = a.post("/signin", data={"email": "asha@example.com", "password": "wrong-password"})
        self.assertIn("Wrong email or password", r.text)

    def test_bad_input_is_handled(self):
        c = self.client
        r = c.post("/drafts", data={"title": "x", "body": "   "})
        self.assertIn("Add some text", r.text)
        r = c.post("/drafts", data={"title": "x"}, files={"doc": ("a.pdf", b"not a pdf", "application/pdf")})
        self.assertEqual(r.status_code, 400)
        r = c.post("/drafts", data={"title": "x"}, files={"image": ("a.png", b"\x89PNG broken", "image/png")})
        self.assertIn("could not be read", r.text)
        self.assertEqual(c.get("/drafts/999999").status_code, 404)
        self.assertEqual(c.get("/kit/rules/R-NOPE-1").status_code, 404)


class UpgradeTests(unittest.TestCase):
    def test_round2_database_is_kept_and_replaced(self):
        path = os.path.join(tempfile.mkdtemp(), "r2.db")
        old = Database(sqlite_path=path)
        old.execute("CREATE TABLE drafts (id INTEGER PRIMARY KEY, title TEXT)")  # Round 2 shape: no workspace_id
        old.execute("INSERT INTO drafts (title) VALUES ('round 2 draft')")
        old.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        old.execute("CREATE INDEX idx_comments_draft ON drafts (title)")
        old._conn.close()
        db = Database(sqlite_path=path)
        db.init_schema()  # used to fail with "no such column: workspace_id"
        self.assertIn("workspace_id", db._columns("drafts"))
        self.assertEqual(db.scalar("SELECT title FROM r2_drafts"), "round 2 draft")
        self.assertIsNotNone(db._columns("r2_meta"))
        self.assertIn("Round 2", db.warning)
        db.init_schema()  # a second start changes nothing
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM r2_drafts"), 1)
        db._conn.close()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to an empty PostgreSQL database")
class RowLevelSecurityTests(unittest.TestCase):
    def test_database_enforces_roles(self):
        from app import auth
        main, S, client = app_client("none", Database(url=os.environ["TEST_DATABASE_URL"]))
        db = S.db
        self.assertTrue(db.rls)
        ws = S.demo_ws
        with db.acting_as(auth.demo_user_id(db, "creator"), ws):
            db.execute("UPDATE rules SET title = 'changed' WHERE id = 'R-TON-01'")  # silently matches no rows
            self.assertNotEqual(db.scalar("SELECT title FROM rules WHERE id = 'R-TON-01'"), "changed")
            with self.assertRaises(Exception):
                db.execute("DELETE FROM audit")
        with db.acting_as(auth.demo_user_id(db, "reviewer"), ws):
            with self.assertRaises(Exception):
                db.execute("INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, action) "
                           "VALUES (?, 1, 1, 'k', 'accept')", (ws,))
            with self.assertRaises(Exception):
                db.scalar("SELECT COUNT(*) AS n FROM users")


if __name__ == "__main__":
    unittest.main()
