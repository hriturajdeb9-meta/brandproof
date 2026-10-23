"""HTTP tests of the whole workflow against a real server process (SQLite, AI off).

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        warnings.simplefilter("ignore", ResourceWarning)
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        env = dict(os.environ, SQLITE_PATH=os.path.join(cls.tmp, "t.db"), AI_PROVIDER="none", DATABASE_URL="")
        cls.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)],
                                    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                requests.get(cls.base + "/healthz", timeout=1)
                break
            except requests.ConnectionError:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def session(self, role):
        s = requests.Session()
        s.cookies.set("bp_role", role)
        return s

    def test_pages_load(self):
        for path in ["/", "/drafts/new", "/kit", "/kit?tab=facts", "/kit?tab=glossary", "/kit?tab=palette",
                     "/review", "/audit", "/about", "/healthz", "/kit/rules.csv"]:
            self.assertEqual(requests.get(self.base + path).status_code, 200, path)
        self.assertEqual(requests.get(self.base + "/drafts/99999").status_code, 404)

    def test_full_workflow(self):
        priya, meera = self.session("creator"), self.session("reviewer")
        r = priya.post(self.base + "/samples/instagram")
        self.assertEqual(r.status_code, 200)
        draft_url = re.sub(r"\?.*$", "", r.url)
        self.assertIn("Prohibited claim", r.text)
        keys = re.findall(r'id="f-([0-9a-f]{10})"', r.text)
        self.assertGreaterEqual(len(keys), 6)
        # submitting with an open blocker is refused
        r = priya.post(draft_url + "/submit")
        self.assertIn("blocker", r.text.lower())
        # reviewers cannot change the draft
        self.assertEqual(meera.post(f"{draft_url}/findings/{keys[0]}", data={"action": "accept"}).status_code, 403)
        # accept every text fix, then re-check
        for k in keys:
            priya.post(f"{draft_url}/findings/{k}", data={"action": "accept"})
        r = priya.get(draft_url)
        self.assertIn("Your working copy", r.text)
        r = priya.post(draft_url + "/recheck", data={"alt": "Paisaleaf banner: Earn up to 7.10% p.a. on Paisaleaf Save."},
                       files={"image": open(os.path.join(ROOT, "app/static/samples/paisaleaf-banner-fixed.png"), "rb")})
        self.assertIn("Version 2 checked", requests.utils.unquote(r.url))
        # submit -> routed to review because the post makes claims
        r = priya.post(draft_url + "/submit")
        self.assertIn("In review", r.text)
        # reviewer must comment to request changes; approve works
        r = meera.post(draft_url + "/review", data={"decision": "changes", "comment": ""})
        self.assertIn("comment required", r.text)
        r = meera.post(draft_url + "/review", data={"decision": "approve", "comment": "Looks good"})
        self.assertIn("Approval record", r.text)
        audit = requests.get(self.base + "/audit").text
        for action in ("ran the checks", "accepted a fix", "submitted for review", "approved"):
            self.assertIn(action, audit)

    def test_clean_draft_is_auto_approved(self):
        priya = self.session("creator")
        r = priya.post(self.base + "/drafts", data={"title": "Dark mode", "channel": "Email", "goal": "", "locked": "",
                                                    "body": "Hi everyone, the Paisaleaf app now has a dark mode. Open the app to try it."})
        draft_url = re.sub(r"\?.*$", "", r.url)
        r = priya.post(draft_url + "/submit")
        self.assertIn("Approved automatically", requests.utils.unquote(r.url))

    def test_reject_needs_reason_and_routes_to_review(self):
        priya = self.session("creator")
        r = priya.post(self.base + "/drafts", data={"title": "Rate post", "channel": "Ad", "goal": "", "locked": "",
                                                    "body": "Earn up to 7.25% p.a. on Paisaleaf Save. *Rates are subject to change. T&Cs apply."})
        draft_url = re.sub(r"\?.*$", "", r.url)
        key = re.findall(r'id="f-([0-9a-f]{10})"', r.text)[0]
        r = priya.post(f"{draft_url}/findings/{key}", data={"action": "reject", "reason": ""})
        self.assertIn("give a short reason", requests.utils.unquote(r.url))
        priya.post(f"{draft_url}/findings/{key}", data={"action": "reject", "reason": "Legal approved the old rate until Friday"})
        r = priya.post(draft_url + "/submit")
        self.assertIn("rejected by the creator", requests.utils.unquote(r.url))

    def test_brand_kit_permissions_and_validation(self):
        priya, arjun = self.session("creator"), self.session("brand")
        self.assertEqual(priya.post(self.base + "/kit/palette/save", data={"name": "X", "hex": "#000000"}).status_code, 403)
        r = arjun.post(self.base + "/kit/rules/save", data={"id": "R-TON-09", "dimension": "tone", "title": "No slang",
                                                           "severity": "minor", "method": "code", "pattern": "(unclosed",
                                                           "active_present": "1", "active": "1"})
        self.assertIn("not a valid regular expression", requests.utils.unquote(r.url))
        r = arjun.post(self.base + "/kit/rules/save", data={"id": "R-TON-09", "dimension": "tone", "title": "No slang",
                                                           "description": "Avoid slang like 'lit'.", "severity": "minor",
                                                           "method": "code", "pattern": r"\blit\b", "fix_hint": "remove",
                                                           "active_present": "1", "active": "1"})
        self.assertIn("Saved", requests.utils.unquote(r.url))
        r = priya.post(self.base + "/drafts", data={"title": "Slang", "channel": "X / Twitter", "body": "This app is lit."})
        self.assertIn("R-TON-09", r.text)  # a new rule applies to the very next check
        csv_text = requests.get(self.base + "/kit/rules.csv").text
        self.assertIn("R-TON-09", csv_text)

    def test_team_names_can_be_changed(self):
        priya, arjun = self.session("creator"), self.session("brand")
        self.assertEqual(requests.get(self.base + "/team").status_code, 200)
        data = {"creator_name": "Riya", "creator_label": "Social media lead", "reviewer_name": "Kabir",
                "reviewer_label": "Legal", "brand_name": "Arjun", "brand_label": "Brand manager"}
        self.assertEqual(priya.post(self.base + "/team", data=data).status_code, 403)
        r = arjun.post(self.base + "/team", data=dict(data, creator_name=""))
        self.assertIn("needs a name", requests.utils.unquote(r.url))
        r = arjun.post(self.base + "/team", data=data)
        self.assertIn("Riya", r.text)
        self.assertIn("Social media lead", requests.get(self.base + "/").text)
        r = arjun.post(self.base + "/team", data={"reset": "1"})
        self.assertIn("Priya", r.text)


if __name__ == "__main__":
    unittest.main()
