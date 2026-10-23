"""Demo brand kit for Paisaleaf, a fictional savings app.

Everything here is invented for the hackathon demo. Real organisations replace
it through the Brand Kit screens or CSV import.
"""
from __future__ import annotations

from .db import Database, now_iso

DISCLAIMER = "*Rates are subject to change. T&Cs apply."

# id, dimension, title, description, severity, method, pattern, fix_hint, do, dont, source
RULES = [
    # ---- claims
    ("R-CLM-01", "claims", "No guaranteed or risk-free return promises",
     "Savings and investment messages must never promise guaranteed, assured or risk-free returns.",
     "blocker", "code", r"\b(guarantee[ds]?|assured\s+returns?|risk[- ]free|zero[- ]risk|100%\s+safe)\b", "",
     "Earn up to 7.10% p.a. on Paisaleaf Save*", "Guaranteed 12% returns", "Brand Guidelines v3, §6.2, p. 14"),
    ("R-CLM-02", "claims", "Numbers must match the Claim Ledger",
     "Any rate, price, count or rating must match a current fact in the Claim Ledger.",
     "blocker", "code", "", "", "Earn up to 7.10% p.a.", "Earn up to 9% p.a.", "Brand Guidelines v3, §6.1, p. 13"),
    ("R-CLM-03", "claims", "No expired facts",
     "Facts past their valid-until date must not be published. Use the current fact instead.",
     "major", "code", "", "", "Earn up to 7.10% p.a.", "Earn up to 7.25% p.a. (old rate)", "Brand Guidelines v3, §6.1, p. 13"),
    ("R-CLM-04", "claims", "Superlatives and comparisons need evidence",
     "Words like best, #1, top, fastest, cheapest or 'better than' need a ledger fact that says exactly that.",
     "major", "code+ai", "", "", "Rated 4.6★ on Google Play", "The best savings app in India", "Brand Guidelines v3, §6.3, p. 14"),
    ("R-CLM-05", "claims", "Every factual claim needs an approved source",
     "Statements of fact about Paisaleaf (numbers, features, safety) must be backed by the Claim Ledger.",
     "major", "code+ai", "", "", "Withdraw anytime. Money reaches your bank within 1 working day.",
     "Instant withdrawals in 5 seconds", "Brand Guidelines v3, §6.1, p. 13"),
    # ---- tone
    ("R-TON-01", "tone", "Calm confidence, no pressure",
     "We never rush people into money decisions. Avoid urgency, fear of missing out and pressure language.",
     "minor", "code+ai", r"\b(hurry|act now|don'?t miss( out)?|last chance|limited[- ]time|before it'?s too late|only today)\b",
     "remove", "Start saving from ₹100 a day, at your own pace.", "Hurry! Last chance to grab these returns!",
     "Brand Guidelines v3, §3.1, p. 6"),
    ("R-TON-02", "tone", "No all-caps or hype emoji in money messages",
     "Messages about rates, returns or savings use sentence case and no hype emoji such as 🚀 🔥 💰 🤑.",
     "minor", "code", "", "", "Earn up to 7.10% p.a.", "GUARANTEED RETURNS 🚀🔥", "Brand Guidelines v3, §3.2, p. 6"),
    ("R-TON-03", "tone", "Plain words for first-time savers",
     "Explain money terms in everyday words. Avoid jargon such as yield, APY, liquidity or compounding without a short explanation.",
     "minor", "ai", "", "", "Your interest is added to your savings every month.",
     "Optimise your liquidity with our high-yield APY product.", "Brand Guidelines v3, §3.3, p. 7"),
    ("R-TON-04", "tone", "Warm and respectful, never mocking",
     "Never shame or mock people for how they save, spend or earn.",
     "minor", "ai", "", "", "Every rupee you save counts.", "Still keeping cash under your mattress, grandpa?",
     "Brand Guidelines v3, §3.4, p. 7"),
    # ---- terminology (matches come from the glossary)
    ("R-TRM-01", "terminology", "Write Paisaleaf as one word with a capital P", "", "minor", "code", "", "",
     "Paisaleaf", "Paisa Leaf, PaisaLeaf", "Brand Guidelines v3, §2.1, p. 4"),
    ("R-TRM-02", "terminology", "Use the product name Paisaleaf Save", "", "minor", "code", "", "",
     "Paisaleaf Save", "Paisaleaf Savings, PaisaleafSave", "Brand Guidelines v3, §2.1, p. 4"),
    ("R-TRM-03", "terminology", "Write email, not e-mail", "", "minor", "code", "", "", "email", "e-mail",
     "Brand Guidelines v3, §2.4, p. 5"),
    ("R-TRM-04", "terminology", "Spell out fixed deposit", "", "minor", "code", "", "", "fixed deposit", "FD",
     "Brand Guidelines v3, §2.4, p. 5"),
    ("R-TRM-05", "terminology", "We call the people we serve savers", "", "minor", "code", "", "", "savers",
     "customers, clients", "Brand Guidelines v3, §2.3, p. 5"),
    ("R-TRM-07", "terminology", "Sign up is the verb; sign-up is the noun", "", "minor", "code", "", "",
     "Sign up now", "Sign-up now", "Brand Guidelines v3, §2.4, p. 5"),
    # ---- messaging
    ("R-MSG-01", "messaging", "Banned phrases",
     "Phrases that overpromise or trivialise money are never used.",
     "major", "code", r"\b(get rich( quick)?|double your money|money printing|free money|tax[- ]free returns)\b",
     "remove", "Grow your savings steadily.", "Double your money with Paisaleaf!", "Brand Guidelines v3, §5.2, p. 11"),
    ("R-MSG-03", "messaging", "Rate and return messages need the disclaimer",
     f"Any message about rates, returns or interest must include: \"{DISCLAIMER}\"",
     "major", "code", r"\b(returns?|interest|p\.\s?a\.|rates?)\b", f"append: {DISCLAIMER}",
     f"Earn up to 7.10% p.a.* {DISCLAIMER}", "Earn up to 7.10% p.a. (no disclaimer)", "Brand Guidelines v3, §5.4, p. 12"),
    # ---- visual
    ("R-VIS-01", "visual", "Use only brand palette colours in graphics",
     "Graphics and banners use colours from the approved palette only.",
     "major", "code", "", "", "Leaf Green #11734F on Cream #FBFAF6", "Bright orange #FF7A00 headline",
     "Brand Guidelines v3, §7.1, p. 16"),
    # ---- accessibility
    ("R-ACC-01", "accessibility", "Text contrast at least 4.5:1 (3:1 for large text)",
     "Text in images and on screens must meet WCAG 2.2 AA contrast.",
     "major", "code", "", "", "Deep Ink on Cream (15:1)", "Orange on Cream (2.6:1)", "WCAG 2.2 SC 1.4.3"),
    ("R-ACC-02", "accessibility", "Every image needs alt text",
     "Describe what the image shows and any text in it, in one or two short sentences.",
     "major", "code", "", "", "Banner: 'Earn up to 7.10% p.a. on Paisaleaf Save'", "(no alt text)", "WCAG 2.2 SC 1.1.1"),
    ("R-ACC-03", "accessibility", "Descriptive link text",
     "Links say where they go. Avoid 'click here' or 'read more' on their own.",
     "minor", "code", r"\b(click here|tap here|read more|learn more here)\b",
     "Use link text that says where it goes, for example 'See Paisaleaf Save rates'.",
     "See Paisaleaf Save rates", "Click here", "WCAG 2.2 SC 2.4.4"),
    ("R-ACC-04", "accessibility", "Readable for first-time savers",
     "Aim for a reading-ease score of 60 or more and sentences under 30 words.",
     "minor", "code", "", "", "Short, plain sentences.", "One 45-word sentence full of terms.", "Brand Guidelines v3, §4.1, p. 8"),
    ("R-ACC-05", "accessibility", "Inclusive language",
     "Use words that include everyone.",
     "minor", "code", r"\b(guys|crazy|insane|handicapped|manpower|lame)\b",
     "map: guys=>everyone; crazy=>surprising; insane=>remarkable; handicapped=>disabled; manpower=>team; lame=>weak",
     "Hi everyone", "Hey guys, insane returns!", "Brand Guidelines v3, §4.3, p. 9"),
]

# preferred, avoid (comma-separated), note, rule
GLOSSARY = [
    ("Paisaleaf", "Paisa Leaf, Paisa-Leaf, PaisaLeaf, paisaleaf", "Brand name: one word, capital P.", "R-TRM-01"),
    ("Paisaleaf Save", "Paisaleaf Savings, PaisaleafSave, Paisaleaf saver", "Official product name.", "R-TRM-02"),
    ("email", "e-mail, E-mail", "One word, no hyphen.", "R-TRM-03"),
    ("fixed deposit", "FD, F.D.", "Spell it out for first-time savers.", "R-TRM-04"),
    ("savers", "customers, clients", "We call the people we serve savers.", "R-TRM-05"),
    ("sign up now", "sign-up now, signup now", "'Sign up' is the verb; 'sign-up' is the noun.", "R-TRM-07"),
    ("sign up", "signup today, sign-up today", "'Sign up' is the verb.", "R-TRM-07"),
]

# id, topic, label, statement, value, approved_wording, keywords, evidence, owner, valid_until
FACTS = [
    ("F-01", "min-deposit", "Minimum deposit", "You can start saving with ₹100.", "100",
     "Start saving with just ₹100.", "start, minimum, deposit, ₹100, 100, begin",
     "Product spec PS-14 (Sep 2026)", "Product", "2027-03-31"),
    ("F-02", "save-rate", "Paisaleaf Save rate (old)", "Paisaleaf Save paid up to 7.25% p.a.", "7.25",
     "Earn up to 7.25% p.a. on Paisaleaf Save*", "interest, rate, returns, earn, p.a., Paisaleaf Save",
     "Treasury memo TM-07 (Jan 2026)", "Treasury", "2026-06-30"),
    ("F-03", "save-rate", "Paisaleaf Save rate", "Paisaleaf Save pays up to 7.10% p.a.", "7.10",
     "Earn up to 7.10% p.a. on Paisaleaf Save*", "interest, rate, returns, earn, p.a., Paisaleaf Save",
     "Treasury memo TM-11 (Jul 2026)", "Treasury", "2026-12-31"),
    ("F-04", "insurance", "Deposit insurance", "Deposits are held with our partner bank and insured up to ₹5 lakh per depositor by DICGC.",
     "500000", "Your deposits are insured up to ₹5 lakh by DICGC.", "insured, insurance, DICGC, safe, protected, ₹5 lakh, secure",
     "Partner bank agreement, clause 9", "Legal", "2027-12-31"),
    ("F-05", "withdrawal", "Withdrawal time", "Withdraw anytime; money reaches your bank within 1 working day.", "1",
     "Withdraw anytime. Money reaches your bank within 1 working day.", "withdraw, withdrawal, anytime, instant, working day, quick",
     "Ops SLA OPS-3", "Operations", "2027-03-31"),
    ("F-06", "fees", "Fees", "No account opening or maintenance fees.", "0",
     "No account opening or maintenance fees.", "fees, fee, free, charges, maintenance, zero",
     "Pricing sheet PR-2026-2", "Finance", "2027-03-31"),
    ("F-07", "payout", "Interest payout", "Interest is credited to your account every month.", "",
     "Your interest is added every month.", "monthly, credited, payout, every month",
     "Product spec PS-14 (Sep 2026)", "Product", "2027-03-31"),
    ("F-12", "rating", "App rating", "Rated 4.6★ on Google Play (October 2026).", "4.6",
     "Rated 4.6★ on Google Play", "rating, rated, stars, ★, Google Play, app, reviews, loved",
     "Play Console export, 1 Oct 2026", "Growth", "2026-12-31"),
    ("F-13", "savers", "Number of savers", "Over 1.2 lakh savers use Paisaleaf.", "120000",
     "Trusted by over 1.2 lakh savers", "savers, users, customers, people, lakh, trusted, community",
     "Analytics dashboard, 30 Sep 2026", "Growth", "2026-12-31"),
]

PALETTE = [
    ("Leaf Green", "#11734F"),
    ("Deep Ink", "#12263F"),
    ("Harvest Amber", "#B45309"),
    ("Mint", "#E8F3EE"),
    ("Cream", "#FBFAF6"),
    ("White", "#FFFFFF"),
    ("Slate", "#5F6A82"),
]

SAMPLE_POST = {
    "title": "Instagram: Paisaleaf Save launch post",
    "channel": "Instagram",
    "goal": "Get sign-ups by highlighting returns, for first-time savers.",
    "locked": ["Paisaleaf Save"],
    "body": "GUARANTEED 12% returns with Paisaleaf Save 🚀 The best savings app in India. Sign-up now!",
    "alt": "",
    "image": "samples/paisaleaf-banner.png",
}

SAMPLE_EMAIL = {
    "title": "Email: monthly savings tips",
    "channel": "Email",
    "goal": "Remind savers to keep their savings habit going.",
    "locked": [],
    "body": (
        "Hi everyone,\n\n"
        "Saving a little every month adds up. With Paisaleaf Save you can start saving with ₹100 and "
        "withdraw anytime. Money reaches your bank within 1 working day.\n\n"
        "Your deposits are insured up to ₹5 lakh by DICGC, and there are no account opening or maintenance fees.\n\n"
        "See your savings in the Paisaleaf app.\n\nThe Paisaleaf team"
    ),
    "alt": "",
    "image": None,
}


def seed_kit(db: Database) -> bool:
    """Load the demo brand kit once. Safe to call on every start and from several processes."""
    if not db.claim_once("kit_seeded"):
        return False
    ts = now_iso()
    for r in RULES:
        db.execute(
            """INSERT INTO rules (id, dimension, title, description, severity, method, pattern, fix_hint,
               do_example, dont_example, source, active, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT (id) DO NOTHING""",
            (*r, ts),
        )
    for g in GLOSSARY:
        db.execute("INSERT INTO glossary (preferred, avoid, note, rule_id, updated_at) VALUES (?, ?, ?, ?, ?)", (*g, ts))
    for f in FACTS:
        db.execute(
            """INSERT INTO facts (id, topic, label, statement, value, approved_wording, keywords, evidence, owner,
               valid_until, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING""",
            (*f, ts),
        )
    for p in PALETTE:
        db.execute("INSERT INTO palette (name, hex, updated_at) VALUES (?, ?, ?)", (*p, ts))
    db.set_meta("disclaimer", DISCLAIMER)
    db.audit("kit.seeded", "System", "system", None, {"rules": len(RULES), "facts": len(FACTS)})
    return True
