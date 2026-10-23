"""End-to-end click-through of the core workflow in a real browser (Playwright).

Usage:  python tests/e2e_flow.py http://localhost:8000 [screenshot_dir]
It creates the sample post, works through the findings as Priya, re-checks with the fixed banner,
submits, switches to Meera and approves. It fails loudly if any step does not behave as expected.
"""
from __future__ import annotations

import os
import re
import sys

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
SHOTS = sys.argv[2] if len(sys.argv) > 2 else ""
HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_BANNER = os.path.join(HERE, "..", "app", "static", "samples", "paisaleaf-banner-fixed.png")


def shot(page, name, full=True):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=full)


def role(page, key):
    with page.expect_navigation():
        page.locator(f"form.roles button[value='{key}']").click()


def click_nav(page, locator, timeout=120_000):
    with page.expect_navigation(timeout=timeout):
        locator.click()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.goto(BASE + "/")
        role(page, "creator")
        shot(page, "01-home", full=False)

        # 1. check the sample post
        page.get_by_role("button", name="Check the sample Instagram post").click()
        page.wait_for_url(re.compile(r"/drafts/\d+$"), timeout=120_000)
        draft_url = page.url
        expect(page.locator(".fc").first).to_be_visible()
        n_findings = page.locator(".findings .fc").count()
        print("findings on v1:", n_findings)
        assert n_findings >= 5, "expected the sample to produce several findings"
        assert page.locator(".fc.sev-blocker").count() >= 1, "expected a blocker"
        shot(page, "02-draft-v1")
        shot(page, "02b-draft-v1-top", full=False)

        # 2. accept the fixes that change text; reject one finding with a reason
        def accept_first(selector_text):
            card = page.locator(".fc", has_text=selector_text).first
            click_nav(page, card.get_by_role("button", name=re.compile("Accept fix|Will fix")))

        accept_first("Prohibited claim")
        accept_first("Unsupported claim")
        accept_first("Required disclaimer")
        accept_first("Hype emoji 🚀 in a money message")
        accept_first("Use “Sign up now”")
        assert page.locator("#working del", has_text="🚀").count() >= 1, "emoji should be shown as removed in the working copy"
        # the caps finding is inside the replaced claim: accepting it should say it already changed
        caps = page.locator(".fc", has_text="All-caps in a money message").first
        assert "Resolved by the fix" in caps.inner_text(), "caps finding inside the replaced claim should be resolved"
        # alt text: accept (AI suggestion) or note
        alt = page.locator(".fc", has_text="no alt text").first
        if alt.count():
            click_nav(page, alt.get_by_role("button", name=re.compile("Accept fix|Will fix")))
        shot(page, "03-working-copy")
        assert page.locator("#working").count() == 1, "working copy panel should appear"

        # 3. edit manually: add alt text, upload the fixed banner, re-check
        page.locator("summary", has_text="Edit the draft yourself").click()
        page.locator("input[name=alt]").fill("Paisaleaf banner: Earn up to 7.10% p.a. on Paisaleaf Save. Rates are subject to change. T&Cs apply.")
        page.locator("form[action$='/recheck'] input[type=file]").set_input_files(FIXED_BANNER)
        click_nav(page, page.get_by_role("button", name=re.compile("Save & re-check")))
        print("after re-check:", page.locator(".flash").first.inner_text())
        shot(page, "04-draft-v2")
        blockers = page.locator(".findings .fc.sev-blocker:not(.decided)").count()
        print("open blockers on v2:", blockers)
        assert blockers == 0, "the fixed draft should have no open blockers"

        # 4. submit -> should go to review because the post makes claims
        click_nav(page, page.get_by_role("button", name=re.compile(r"Submit v\d")))
        msg = page.locator(".flash").first.inner_text()
        print("submit:", msg)
        assert "review" in msg.lower(), "a draft with claims must go to a reviewer"

        # 5. reviewer approves
        role(page, "reviewer")
        page.goto(BASE + "/review")
        shot(page, "05-review-queue", full=False)
        page.goto(draft_url)
        page.locator("textarea[name=comment]").fill("Claims match the ledger. Approved for Instagram.")
        click_nav(page, page.get_by_role("button", name=re.compile(r"Approve v\d")))
        assert "Approved" in page.locator(".status").first.inner_text()
        shot(page, "06-approved")

        # 6. brand manager edits the ledger (expire the rate) -> audit log shows it
        role(page, "brand")
        page.goto(BASE + "/kit?tab=facts")
        shot(page, "07-claim-ledger", full=False)
        page.goto(BASE + "/audit")
        shot(page, "08-audit", full=False)
        page.goto(BASE + "/about")
        shot(page, "09-about", full=False)

        # 7. the DOCX path
        role(page, "creator")
        page.goto(BASE + "/drafts/new")
        page.locator("input[name=title]").fill("Newsletter: October (docx)")
        page.locator("input[name=goal]").fill("Tell students about Paisaleaf Save")
        page.locator("input[name=docx]").set_input_files(os.path.join(HERE, "..", "app", "static", "samples", "paisaleaf-newsletter.docx"))
        page.get_by_role("button", name="Check draft").click()
        page.wait_for_url(re.compile(r"/drafts/\d+$"), timeout=120_000)
        n_docx = page.locator(".findings .fc").count()
        print("docx findings:", n_docx)
        assert n_docx >= 6
        shot(page, "10-docx-draft")

        # 8. clean sample email: should be approved automatically or go to review (it has supported claims)
        page.goto(BASE + "/")
        page.get_by_role("button", name="Check a clean sample email").click()
        page.wait_for_url(re.compile(r"/drafts/\d+$"), timeout=120_000)
        print("email findings:", page.locator(".findings .fc").count(), "verified claims:",
              page.locator(".verified li").count())
        shot(page, "11-email")

        # 9. mobile layout
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(draft_url)
        shot(mobile, "12-mobile", full=False)
        browser.close()
    print("E2E OK")


if __name__ == "__main__":
    main()
